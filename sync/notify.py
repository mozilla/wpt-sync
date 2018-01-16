import json
import os
from collections import defaultdict

import taskcluster
import commit as sync_commit
from env import Environment

env = Environment()


class Result(object):
    def __init__(self, new_result):
        self.new_result = new_result
        self.previous_result = None

    def is_crash(self):
        return self.new_result == "CRASH"

    def is_new_non_passing(self):
        return self.previous_result is None and self.new_result not in ("PASS", "OK")

    def is_regression(self):
        return ((self.previous_result in ("PASS", "OK") and
                 self.new_result not in ("PASS", "OK")) or
                (self.previous_result == "FAIL" and self.new_result in
                 ("TIMEOUT", "ERROR", "CRASH", "NOTRUN")))

    def is_disabled(self):
        return self.new_result == "SKIP"


def parse_logs(job_logs, log_data, new=True):
    for job_name, logs in job_logs.iteritems():

        if not new and job_name not in log_data:
            continue

        for log in logs:
            with open(log) as f:
                log_data = json.load(f)

            for test in log_data["results"]:
                if new:
                    subtest_results = {}
                    log_data[job_name][test["test"]] = (Result(test["status"]),
                                                        subtest_results)
                else:
                    if test["test"] in log_data[job_name]:
                        subtest_results = log_data[job_name][test["test"]][1]
                        log_data[job_name][test["test"]][0].previous_result = test["status"]
                    else:
                        continue
                for subtest in test["subtests"]:
                    if new:
                        subtest_results[subtest["name"]] = subtest["status"]
                    else:
                        if subtest["name"] in subtest_results:
                            subtest_results[subtest["name"]].previous_result = subtest["status"]


def get_central_tasks(git_gecko, sync):
    central_commit = sync_commit.GeckoCommit(
        git_gecko,
        git_gecko.merge_base(sync.gecko_commits.head.sha1,
                             env.config["gecko"]["refs"]["central"])[0])
    taskgroup_id = taskcluster.get_taskgroup_id("mozilla-central",
                                                central_commit.canonical_rev)
    if taskgroup_id is None:
        return False

    taskgroup_id = taskcluster.normalize_task_id(taskgroup_id)
    tasks = taskcluster.get_tasks_in_group(taskgroup_id)

    if not tasks:
        return False

    # Check if the job is complete
    for task in tasks:
        state = task.get("status", {}).get("state")
        if state in (None, "pending", "running"):
            return False

    wpt_tasks = taskcluster.filter_suite(tasks, "web-platform-tests")

    dest = os.path.join(env.config["root"], env.config["paths"]["try_logs"],
                        "central", central_commit.sha1)

    taskcluster.download_logs(wpt_tasks, dest, raw=False)

    return wpt_tasks


def get_logs(tasks):
    logs = defaultdict(list)
    for task in tasks:
        job_name = taskcluster.parse_job_name(
            task.get("task", {}).get("metadata", {}).get("name", "unknown"))
        runs = task.get("status", {}).get("runs", [])
        if not runs:
            continue
        run = runs[-1]
        log = run.get("_log_paths", []).get("wptreport.json")
        if log:
            logs[job_name].append(log)
    return logs


def get_msg(try_tasks, central_tasks):
    try_log_files = get_logs(try_tasks)
    central_log_files = get_logs(central_tasks)

    log_data = defaultdict(dict)

    parse_logs(try_log_files, log_data, True)
    parse_logs(central_log_files, log_data, False)

    summary = get_summary(log_data)
    details = get_details(log_data)

    return message(log_data.keys(), summary, details)


def get_summary(log_data):
    summary = defaultdict(lambda: defaultdict(int))
    # Work out how many tests ran, etc.
    for job_name, test_data in log_data.iteritems():
        for (result, subtest_results) in test_data.itervalues():
            summary["parent_tests"][job_name] += 1
            summary[result.new_result][job_name] += 1
            for subtest_result in subtest_results.itervalues():
                summary["subtests"][job_name] += 1
                summary[subtest_result.new_result][job_name] += 1
    return summary


def get_details(log_data):
    def test_result():
        return {}, defaultdict(dict)

    details = {
        "crash": defaultdict(test_result),
        "new_not_pass": defaultdict(test_result),
        "worse_result": defaultdict(test_result),
        "disabled": defaultdict(test_result)
    }

    def add_result(job_name, result, keys):
        key = None
        if result.is_crash():
            key = "crash"
        if result.is_new_non_passing():
            key = "new_not_pass"
        elif result.is_regression():
            key = "worse_result"
        elif result.is_disabled():
            key = "disabled"

        target = details[key]
        for extra in keys:
            target = target[extra]

        target[job_name] = result

    for job_name, test_data in log_data.iteritems():
        for test, (result, subtest_results) in test_data.iteritems():
            add_result(job_name, result, (test, 0))
            for title, subtest_result in subtest_results.iteritems():
                add_result(job_name, subtest_result, (test, 1, title))

    return details


def consistent(results):
    target = results.itervalues().next()
    return all(value == target for value in results.itervalues())


def group(results):
    by_value = defaultdict(list)
    for key, value in results.iteritems():
        by_value[value].append(key)
    return by_value


def value_str(results, merge_consistent):
    if merge_consistent and consistent(results):
        value = "%s" % parent_tests.itervalues().next()
    else:
        by_value = group(results)
        value = "%s" % (", ".join("%s[%s]" % ((count, ",".join(sorted(job_names)))
                                              for count, job_name in by_value.iteritems())))
    return value


def message(job_names, summary, details):
    # This is currently the number of codepoints, with the proviso that
    # only BMP characters are supported
    MAX_LENGTH = 65535

    suffix = "\n(truncated for maximum comment length)"

    message = summary_message(summary)
    for part in details_message(job_names, details):
        if len(message) + len(part) + 1 > MAX_LENGTH - len(suffix):
            message += suffix
            break
        message += "\n" + part

    return message


def summary_message(summary):
    parent_tests = summary["parent_tests"]
    parent_summary = "Ran %s tests" % value_str(parent_tests, True)

    subtests = summary["subtests"]
    if not subtests:
        subtest_summary = ""
    else:
        subtest_summary = " and %s subtests" % value_str(subtests, True)

    results_summary = []

    results = ["OK", "PASS", "CRASH", "FAIL", "TIMEOUT", "ERROR", "NOTRUN"]
    max_width = max(len(item) for item in results)
    for result in ["OK", "PASS", "CRASH", "FAIL", "TIMEOUT", "ERROR", "NOTRUN"]:
        if result in summary:
            result_data = summary[result]
            results_summary.append("%s: %s" % (result.ljust(max_width),
                                               value_str(result_data, True)))

    return """%s%s

%s""" % (parent_summary, subtest_summary, "\n".join(results_summary))


def details_message(job_names, details):
    msg_parts = []

    for key, intro, include_result in [("crash", "The following tests crash:", False),
                                       ("worse_result", "The following tests have a worse result "
                                        "after import (e.g. they used to PASS and now FAIL)", True),
                                       ("new_not_pass", "The following new tests don't PASS "
                                        "on all platforms", True),
                                       ("disabled", "The following tests are disabled", False)]:
        part_parts = [intro, ""]
        data = details[key]
        if not data:
            continue
        for parent_test, (results, subtests) in sorted(data.iteritems()):
            test_str = []
            if results:
                if len(results) == len(job_names) or not include_result:
                    test_str.append(parent_test)
                else:
                    test_str.append("%s: " % parent_test)
                    test_str.append(value_str(group(results), False))
            else:
                test_str.append(parent_test)

            part_parts.append("".join(test_str))
            if subtests:
                for title, subtest_results in sorted(subtests.iteritems()):
                    subtest_str = ["    "]
                    if len(subtest_results) == len(job_names) or not include_result:
                        subtest_str.append(title)
                    else:
                        subtest_str.append("%s: " % title)
                        subtest_str.append(value_str(group(subtest_results), False))
                    part_parts.append("".join(subtest_str))
            msg_parts.append("\n".join(part_parts))
    return msg_parts
