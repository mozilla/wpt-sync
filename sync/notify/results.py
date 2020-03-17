import json
import os
from collections import defaultdict

import newrelic
import requests
from six import iterkeys, iteritems, itervalues

from .. import log
from .. import tc
from .. import commit as sync_commit
from ..env import Environment
from ..errors import RetryableError
from ..meta import Metadata
from .. import wptfyi

logger = log.get_logger(__name__)
env = Environment()

passing_statuses = frozenset(["PASS", "OK"])
statuses = frozenset(["OK", "PASS", "CRASH", "FAIL", "TIMEOUT", "ERROR", "NOTRUN",
                      "PRECONDITION_FAILED"])
browsers = ["firefox", "chrome", "safari"]


class StatusResult(object):
    def __init__(self, base=None, head=None):
        self.base = None
        self.head = None
        self.head_expected = []
        self.base_expected = []

    def set(self, has_changes, status, expected):
        if has_changes:
            self.head = status
            self.head_expected = expected
        else:
            self.base = status
            self.base_expected = expected

    def is_crash(self):
        return self.head == "CRASH"

    def is_new_non_passing(self):
        return self.base is None and self.head not in passing_statuses

    def is_regression(self):
        # Regression if we go from a pass to a fail or a fail to a worse
        # failure and the result isn't marked as a known intermittent

        return ((self.base in passing_statuses and
                 self.head not in passing_statuses and
                 self.head not in self.head_expected) or
                (self.base == "FAIL" and
                 self.head in ("TIMEOUT", "ERROR", "CRASH", "NOTRUN") and
                 self.head not in self.head_expected))

    def is_disabled(self):
        return self.head == "SKIP"


class Result(object):
    def __init__(self):
        # Mapping {browser: {platform: StatusResult}}
        self.statuses = defaultdict(lambda: defaultdict(StatusResult))
        self.bug_links = []

    def iter_filter_status(self, fn):
        for browser, by_platform in iteritems(self.statuses):
            for platform, status in iteritems(by_platform):
                if fn(browser, platform, status):
                    yield browser, platform, status

    def set_status(self, browser, job_name, run_has_changes, status, expected):
        self.statuses[browser][job_name].set(run_has_changes, status, expected)

    def is_consistent(self, browser, target="head"):
        assert target in ["base", "head"]
        browser_results = self.statuses.get(browser, {})
        if not browser_results:
            return True
        first_result = getattr(itervalues(browser_results).next(), target)

        return all(getattr(result, target) == first_result
                   for result in itervalues(browser_results))

    def is_browser_only_failure(self, target_browser="firefox"):
        gh_target = self.statuses[target_browser].get("GitHub")
        gh_other = [self.statuses.get(browser, {}).get("GitHub")
                    for browser in browsers
                    if browser != target_browser]
        if gh_target is None or None in gh_other:
            # We don't have enough information to determine GH-only failures
            return False

        if gh_target.head in passing_statuses:
            return False

        if any(item.head not in passing_statuses for item in gh_other):
            return False

        # If it's passing on all internal platforms, assume a pref has to be
        # set or something. We could do better than this
        gecko_ci_statuses = [status
                             for job_name, status in iteritems(self.statuses[target_browser])
                             if job_name != "GitHub"]
        if (gecko_ci_statuses and
            all(status.head in passing_statuses for status in gecko_ci_statuses)):
            return False

        return True

    def has_crash(self, target_browser="firefox"):
        return any(self.iter_filter_status(
            lambda browser, _, status: (browser == target_browser and
                                        status.is_crash())))

    def has_new_non_passing(self, target_browser="firefox"):
        return any(self.iter_filter_status(
            lambda browser, _, status: (browser == target_browser and
                                        status.is_new_non_passing())))

    def has_regression(self, target_browser="firefox"):
        return any(self.iter_filter_status(
            lambda browser, _, status: (browser == target_browser and
                                        status.is_regression())))

    def has_disabled(self, target_browser="firefox"):
        return any(self.iter_filter_status(
            lambda browser, _, status: (browser == target_browser and
                                        status.is_disabled())))

    def has_link(self, status=None):
        if status is None:
            return len(self.bug_links) > 0
        return any(item for item in self.bug_links if item.status == status)


class TestResult(Result):
    def __init__(self):
        # Mapping {subtestname: SubtestResult}
        self.subtests = defaultdict(SubtestResult)
        super(TestResult, self).__init__()


class SubtestResult(Result):
    pass


class Results(object):
    def __init__(self):
        # Mapping of {test: TestResult}
        self.test_results = defaultdict(TestResult)
        self.errors = []
        self.wpt_sha = None
        self.treeherder_url = None

    def iter_results(self):
        for test_name, result in iteritems(self.test_results):
            yield test_name, None, result
            for subtest_name, subtest_result in iteritems(result.subtests):
                yield test_name, subtest_name, subtest_result

    def iter_filter(self, fn):
        for test_name, subtest_name, result in self.iter_results():
            if fn(test_name, subtest_name, result):
                yield test_name, subtest_name, result

    def add_jobs_from_log_files(self, logs_no_changes, logs_with_changes):
        for (browser_logs, run_has_changes) in [(logs_with_changes, True),
                                                (logs_no_changes, False)]:
            for browser, job_logs in iteritems(browser_logs):
                for job_name, job_logs in iteritems(job_logs):
                    if not run_has_changes and job_name not in self.test_results:
                        continue

                    for log_data in job_logs:
                        try:
                            json_data = log_data.json()
                        except ValueError:
                            self.errors.append(("Failed to parse data for %s %s" %
                                                (browser, job_name), False))
                            continue
                        self.add_log(json_data, browser, job_name, run_has_changes)

    def add_log(self, data, browser, job_name, run_has_changes):
        for test in data["results"]:
            use_result = run_has_changes or test["test"] in self.log_data
            if use_result:
                status = test["status"]
                expected = [test.get("expected", status)] + test.get("known_intermittent", [])
                self.test_results[test["test"]].set_status(browser,
                                                           job_name,
                                                           run_has_changes,
                                                           status,
                                                           expected)
                for subtest in test["subtests"]:
                    status = subtest["status"]
                    expected = ([subtest.get("expected", status)] +
                                subtest.get("known_intermittent", []))

                    (self.test_results[test["test"]]
                     .subtests[subtest["name"]]
                     .set_status(browser, job_name, run_has_changes, status, expected))

    def add_metadata(self, metadata):
        for test, result in iteritems(self.test_results):
            for meta_link in metadata.iterbugs(test, product="firefox"):
                if meta_link.subtest is None:
                    result.bug_links.append(meta_link)
                else:
                    if meta_link.subtest in result.subtests:
                        result.subtests[meta_link.subtest].bug_links.append(meta_link)

    def browsers(self):
        browsers = set()
        for result in itervalues(self.test_results):
            browsers |= set(item for item in iterkeys(result.statuses))
        return browsers

    def job_names(self, browser):
        job_names = set()
        for result in itervalues(self.test_results):
            if browser in result.statuses:
                for job_name in iterkeys(result.statuses[browser]):
                    job_names.add(job_name)
        return job_names

    def summary(self):
        summary = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
        # Work out how many tests ran, etc.

        summary["parent_tests"] = 0
        summary["subtests"] = 0

        def update_for_result(result):
            for browser, browser_result in iteritems(result.statuses):
                for job_name, job_result in iteritems(browser_result):
                    summary[job_result.head][browser][job_name] += 1

        for test_result in itervalues(self.test_results):
            summary["parent_tests"] += 1
            summary["subtests"] = len(test_result.subtests)
            update_for_result(test_result)
            for subtest_result in itervalues(test_result.subtests):
                update_for_result(subtest_result)
        return summary

    def iter_crashes(self, target_browser="firefox"):
        return self.iter_filter(lambda _test, _subtest, result:
                                result.has_crash(target_browser))

    def iter_new_non_passing(self, target_browser="firefox"):
        return self.iter_filter(lambda _test, _subtest, result:
                                result.has_new_non_passing(target_browser))

    def iter_regressions(self, target_browser="firefox"):
        return self.iter_filter(lambda _test, _subtest, result:
                                result.has_regression(target_browser))

    def iter_disabled(self, target_browser="firefox"):
        return self.iter_filter(lambda _test, _subtest, result:
                                result.has_disabled(target_browser))

    def iter_browser_only(self, target_browser="firefox"):
        def is_browser_only(_test, _subtest, result):
            return result.is_browser_only_failure(target_browser)
        return self.iter_filter(is_browser_only)


def get_push_changeset(commit):
    url = ("https://hg.mozilla.org/mozilla-central/json-pushes?changeset=%s&version=2&tipsonly=1" %
           commit.canonical_rev)
    headers = {"Accept": "application/json",
               "User-Agent": "wpt-sync"}
    resp = requests.get(url, headers=headers)
    try:
        resp.raise_for_status()
    except requests.exceptions.RequestException:
        if resp.status_code != 404:
            newrelic.agent.record_exception()
        return None

    result = resp.json()
    pushes = result["pushes"]
    [changeset] = pushes.values()[0]["changesets"]
    return changeset


def get_central_tasks(git_gecko, sync):
    merge_base_commit = sync_commit.GeckoCommit(
        git_gecko,
        git_gecko.merge_base(sync.gecko_commits.head.sha1,
                             env.config["gecko"]["refs"]["central"])[0])

    push_commit = sync_commit.GeckoCommit(git_gecko,
                                          get_push_changeset(merge_base_commit))
    if push_commit is None:
        return False

    taskgroup_id, state, _ = tc.get_taskgroup_id("mozilla-central",
                                                 push_commit.canonical_rev)
    if taskgroup_id is None:
        return

    if state != "completed":
        logger.info("mozilla-central decision task has state %s" % state)
        return

    taskgroup_id = tc.normalize_task_id(taskgroup_id)
    tasks = tc.TaskGroup(taskgroup_id)

    wpt_tasks = tasks.view(tc.is_suite_fn("web-platform-tests"))

    if not wpt_tasks:
        return None

    if not wpt_tasks.is_complete(allow_unscheduled=True):
        return None

    dest = os.path.join(env.config["root"], env.config["paths"]["try_logs"],
                        "central", push_commit.sha1)

    wpt_tasks.download_logs(dest, ["wptreport.json"])

    return wpt_tasks


class LogFile(object):
    def __init__(self, path):
        self.path = path

    def json(self):
        with open(self.path) as f:
            return json.load(f)


def get_logs(tasks, job_prefix="Gecko-"):
    logs = defaultdict(list)
    for task in tasks:
        job_name = job_prefix + tc.parse_job_name(
            task.get("task", {}).get("metadata", {}).get("name", "unknown"))
        runs = task.get("status", {}).get("runs", [])
        if not runs:
            continue
        run = runs[-1]
        log = run.get("_log_paths", {}).get("wptreport.json")
        if log:
            logs[job_name].append(LogFile(log))
    return {"firefox": logs}


def add_gecko_data(sync, results):
    complete_try_push = None

    for try_push in sorted(sync.try_pushes(), key=lambda x: -x.process_name.seq_id):
        if try_push.status == "complete":
            complete_try_push = try_push
            break

    if not complete_try_push:
        results.e("No complete try push available for PR %s" % sync.pr)
        return False

    # Get the list of central tasks and download the wptreport logs
    central_tasks = get_central_tasks(sync.git_gecko, sync)
    if not central_tasks:
        logger.info("Not all mozilla-central results available for PR %s" % sync.pr)
        return False

    with try_push.as_mut(sync._lock):
        try_tasks = complete_try_push.tasks()
        try:
            complete_try_push.download_logs(try_tasks.wpt_tasks, first_only=True)
        except RetryableError:
            logger.warning("Downloading logs failed")
            return False

    try_log_files = get_logs(try_tasks.wpt_tasks)
    central_log_files = get_logs(central_tasks)

    results.add_jobs_from_log_files(central_log_files, try_log_files)
    results.treeherder_url = complete_try_push.treeherder_url
    return True


def add_wpt_fyi_data(sync, results):
    head_sha1 = sync.wpt_commits.head.sha1

    logs = []
    for target, run_has_changes in [("base", False),
                                    ("head", True)]:
        target_results = defaultdict(dict)
        try:
            runs = wptfyi.get_runs(sha=head_sha1, labels=["pr_%s" % target])
            for run in runs:
                if run["browser_name"] in browsers:
                    browser = run["browser_name"]
                    target_results[browser]["GitHub"] = [requests.get(run["raw_results_url"])]
        except requests.HTTPError as e:
            logger.error("Unable to fetch results from wpt.fyi: %s" % e)
            return False

        logs.append(target_results)
    results.add_jobs_from_log_files(*logs)
    results.wpt_sha = head_sha1
    return True


def for_sync(sync):
    results = Results()
    if not add_gecko_data(sync, results):
        results.errors.append(("Failed to get Gecko data", True))

    if not add_wpt_fyi_data(sync, results):
        results.errors.append(("Failed to get wptfyi data", True))

    results.add_metadata(Metadata(sync.process_name))

    return results
