import json
import os
from collections import defaultdict

import log
import tc
import commit as sync_commit
from env import Environment

logger = log.get_logger(__name__)

env = Environment()


class Result(object):
    def __init__(self, new_result):
        self.new_result = new_result
        self.previous_result = None

    def __str__(self):
        return "(%s, %s)" % (self.previous_result, self.new_result)

    def __repr__(self):
        return "Result<(%s, %s)>" % (self.previous_result, self.new_result)

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

        for log_path in logs:
            with open(log_path) as f:
                # Seems we sometimes get Access Denied messages; it's not clear what the
                # right thing to do in this case is
                try:
                    data = json.load(f)
                except ValueError:
                    logger.warning("Failed to parse %s as JSON" % f.name)
                    continue

            for test in data["results"]:
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
                        subtest_results[subtest["name"]] = Result(subtest["status"])
                    else:
                        if subtest["name"] in subtest_results:
                            subtest_results[subtest["name"]].previous_result = subtest["status"]


def get_central_tasks(git_gecko, sync):
    central_commit = sync_commit.GeckoCommit(
        git_gecko,
        git_gecko.merge_base(sync.gecko_commits.head.sha1,
                             env.config["gecko"]["refs"]["central"])[0])
    taskgroup_id, status = tc.get_taskgroup_id("mozilla-central",
                                               central_commit.canonical_rev)
    if taskgroup_id is None:
        return None

    if status != "success":
        logger.info("mozilla-central decision task has status %s" % status)
        return None

    taskgroup_id = tc.normalize_task_id(taskgroup_id)
    tasks = tc.TaskGroup(taskgroup_id)

    wpt_tasks = tasks.view(tc.is_suite_fn("web-platform-tests"))

    if not wpt_tasks:
        return None

    if not wpt_tasks.is_complete(allow_unscheduled=True):
        return None

    dest = os.path.join(env.config["root"], env.config["paths"]["try_logs"],
                        "central", central_commit.sha1)

    wpt_tasks.download_logs(dest, ["wptreport.json"])

    return wpt_tasks


def get_logs(tasks):
    logs = defaultdict(list)
    for task in tasks:
        job_name = tc.parse_job_name(
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
        elif result.is_new_non_passing():
            key = "new_not_pass"
        elif result.is_regression():
            key = "worse_result"
        elif result.is_disabled():
            key = "disabled"
        if not key:
            return

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


def consistent(results, job_names):
    if len(results) == len(job_names):
        target = results.itervalues().next()
        return all(value == target for value in results.itervalues())
    return False


def group(results):
    by_value = defaultdict(list)
    for key, value in results.iteritems():
        by_value[value].append(key)
    return by_value


def value_str(results, job_names, include_result=True):
    if consistent(results, job_names):
        if include_result:
            value = results.itervalues().next()
        else:
            value = ""
    else:
        by_value = group(results)
        if include_result:
            value = ", ".join("%s[%s]" % (result, ",".join(sorted(job_name)))
                              for result, job_name in by_value.iteritems())
        else:
            value = ", ".join("[%s]" % ",".join(sorted(job_name))
                              for job_name in by_value.itervalues())
    return value


def message(job_names, summary, details):
    # This is currently the number of codepoints, with the proviso that
    # only BMP characters are supported
    MAX_LENGTH = 65535

    suffix = "\n(truncated for maximum comment length)"

    message = summary_message(job_names, summary)
    for part in details_message(job_names, details):
        if len(message) + len(part) + 1 > MAX_LENGTH - len(suffix):
            message += suffix
            break
        message += "\n" + part

    return message


def summary_message(job_names, summary):
    parent_tests = summary["parent_tests"]
    parent_summary = "Ran %s tests" % value_str(parent_tests, job_names)

    subtests = summary["subtests"]
    if not subtests:
        subtest_summary = ""
    else:
        subtest_summary = " and %s subtests" % value_str(subtests, job_names)

    results_summary = []

    results = ["OK", "PASS", "CRASH", "FAIL", "TIMEOUT", "ERROR", "NOTRUN"]
    max_width = max(len(item) for item in results)
    for result in ["OK", "PASS", "CRASH", "FAIL", "TIMEOUT", "ERROR", "NOTRUN"]:
        if result in summary:
            result_data = summary[result]
            results_summary.append("%s: %s" % (result.ljust(max_width),
                                               value_str(result_data, job_names)))

    return """%s%s
%s
""" % (parent_summary, subtest_summary, "\n".join(results_summary))


def details_message(job_names, details):
    msg_parts = []

    def new_results(results):
        return {k: v.new_result for k, v in results.iteritems()}

    for key, intro, include_result in [("crash", "Tests that CRASH:", False),
                                       ("worse_result", "Existing tests that now have a worse "
                                        "result (e.g. they used to PASS and now FAIL):", True),
                                       ("new_not_pass", "New tests that have failures "
                                        "or other problems:", True),
                                       ("disabled", "Tests that are disabled for instability:",
                                        False)]:
        part_parts = [intro]
        data = details[key]
        if not data:
            continue
        for parent_test, (results, subtests) in sorted(data.iteritems()):
            test_str = []
            if results:
                value = value_str(new_results(results), job_names, include_result)
                if not value:
                    test_str.append(parent_test)
                else:
                    test_str.append("%s: %s" % (parent_test, value))
            else:
                test_str.append(parent_test)

            part_parts.append("".join(test_str))
            if subtests:
                for title, subtest_results in sorted(subtests.iteritems()):
                    subtest_str = ["    "]
                    value = value_str(new_results(subtest_results), job_names, include_result)
                    if value:
                        subtest_str.append("%s: %s" % (title, value))
                    else:
                        subtest_str.append(title)
                    part_parts.append("".join(subtest_str))
        msg_parts.append("\n".join(part_parts) + "\n")
    return msg_parts
