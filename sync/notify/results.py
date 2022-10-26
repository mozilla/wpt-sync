from __future__ import annotations
import json
import os
from collections import defaultdict

import newrelic
import requests

from .. import log
from .. import tc
from .. import commit as sync_commit
from ..env import Environment
from ..errors import RetryableError
from ..meta import Metadata
from ..repos import cinnabar
from .. import wptfyi

from typing import (Any,
                    Callable,
                    Dict,
                    Iterable,
                    Iterator,
                    List,
                    Mapping,
                    MutableMapping,
                    Optional,
                    Set,
                    Text,
                    Tuple,
                    Union,
                    TYPE_CHECKING)
from requests import Response
if TYPE_CHECKING:
    from sync.repos import Repo
    from sync.downstream import DownstreamSync
    from sync.meta import MetaLink
    from sync.tc import TaskGroupView

Logs = Mapping[str, Mapping[str, List[Any]]]  # Any is really "anything with a json method"
ResultsEntry = Tuple[str, Optional[str], "Result"]
JobResultsSummary = MutableMapping[str, MutableMapping[str, MutableMapping[str, int]]]

logger = log.get_logger(__name__)
env = Environment()

passing_statuses = frozenset(["PASS", "OK"])
statuses = frozenset(["OK", "PASS", "CRASH", "FAIL", "TIMEOUT", "ERROR", "NOTRUN",
                      "PRECONDITION_FAILED"])
browsers = ["firefox", "chrome", "safari"]


class StatusResult:
    def __init__(self, base: Optional[Text] = None, head: Optional[Text] = None) -> None:
        self.base: Optional[Text] = None
        self.head: Optional[Text] = None
        self.head_expected: List[Text] = []
        self.base_expected: List[Text] = []

    def set(self, has_changes: bool, status: Text, expected: List[Text]) -> None:
        if has_changes:
            self.head = status
            self.head_expected = expected
        else:
            self.base = status
            self.base_expected = expected

    def is_crash(self) -> bool:
        return self.head == "CRASH"

    def is_new_non_passing(self) -> bool:
        return self.base is None and self.head not in passing_statuses

    def is_regression(self) -> bool:
        # Regression if we go from a pass to a fail or a fail to a worse
        # failure and the result isn't marked as a known intermittent

        return ((self.base in passing_statuses and
                 self.head not in passing_statuses and
                 self.head not in self.head_expected) or
                (self.base == "FAIL" and
                 self.head in ("TIMEOUT", "ERROR", "CRASH", "NOTRUN") and
                 self.head not in self.head_expected))

    def is_disabled(self) -> bool:
        return self.head == "SKIP"


class Result:
    def __init__(self) -> None:
        # Mapping {browser: {platform: StatusResult}}
        self.statuses: MutableMapping[Text, MutableMapping[Text, StatusResult]] = defaultdict(
            lambda: defaultdict(
                StatusResult))
        self.bug_links: List[MetaLink] = []

    def iter_filter_status(self,
                           fn: Callable,
                           ) -> Iterator[Tuple[Text, Text, StatusResult]]:
        for browser, by_platform in self.statuses.items():
            for platform, status in by_platform.items():
                if fn(browser, platform, status):
                    yield browser, platform, status

    def set_status(self, browser: Text, job_name: Text, run_has_changes: bool, status: Text,
                   expected: List[Text]) -> None:
        self.statuses[browser][job_name].set(run_has_changes, status, expected)

    def is_consistent(self, browser: Text, target: Text = "head") -> bool:
        assert target in ["base", "head"]
        browser_results: Optional[Mapping[Text, StatusResult]] = self.statuses.get(
            browser)
        if not browser_results:
            return True
        first_result = getattr(next(iter(browser_results.values())), target)

        return all(getattr(result, target) == first_result
                   for result in browser_results.values())

    def is_browser_only_failure(self, target_browser: Text = "firefox") -> bool:
        gh_target = self.statuses[target_browser].get("GitHub")
        gh_other = [self.statuses.get(browser, {}).get("GitHub")
                    for browser in browsers
                    if browser != target_browser]
        if gh_target is None:
            # We don't have enough information to determine GH-only failures
            return False

        if gh_target.head in passing_statuses:
            return False

        if any(item is None or item.head not in passing_statuses for item in gh_other):
            return False

        # If it's passing on all internal platforms, assume a pref has to be
        # set or something. We could do better than this
        gecko_ci_statuses = [status
                             for job_name, status in self.statuses[target_browser].items()
                             if job_name != "GitHub"]
        if (gecko_ci_statuses and
            all(status.head in passing_statuses for status in gecko_ci_statuses)):
            return False

        return True

    def is_github_only_failure(self, target_browser: Text = "firefox") -> bool:
        gh_status = self.statuses[target_browser].get("GitHub")
        if not gh_status:
            return False

        if gh_status.head in passing_statuses:
            return False

        # Check if any non-GitHub status is a pass
        if any(self.iter_filter_status(
                lambda browser, platform, status: (browser == target_browser and
                                                   platform != "GitHub" and
                                                   status.head in passing_statuses))):
            return False

        return True

    def has_crash(self, target_browser: Text = "firefox") -> bool:
        return any(self.iter_filter_status(
            lambda browser, _, status: (browser == target_browser and
                                        status.is_crash())))

    def has_new_non_passing(self, target_browser: Text = "firefox") -> bool:
        return any(self.iter_filter_status(
            lambda browser, _, status: (browser == target_browser and
                                        status.is_new_non_passing())))

    def has_regression(self, target_browser: Text = "firefox") -> bool:
        return any(self.iter_filter_status(
            lambda browser, _, status: (browser == target_browser and
                                        status.is_regression())))

    def has_disabled(self, target_browser: Text = "firefox") -> bool:
        return any(self.iter_filter_status(
            lambda browser, _, status: (browser == target_browser and
                                        status.is_disabled())))

    def has_non_disabled(self, target_browser: Text = "firefox") -> bool:
        return any(self.iter_filter_status(
            lambda browser, platform, status: (browser == target_browser and
                                               platform != "GitHub" and
                                               not status.is_disabled())))

    def has_passing(self) -> bool:
        return any(self.iter_filter_status(
            lambda _browser, _platform, status: status.head in passing_statuses))

    def has_link(self, status: Optional[Text] = None) -> bool:
        if status is None:
            return len(self.bug_links) > 0
        return any(item for item in self.bug_links if item.status == status)


class TestResult(Result):
    def __init__(self) -> None:
        # Mapping {subtestname: SubtestResult}
        self.subtests: MutableMapping[Text, "SubtestResult"] = defaultdict(SubtestResult)
        super().__init__()


class SubtestResult(Result):
    pass


class ResultsSummary:
    def __init__(self):
        self.parent_tests = 0
        self.subtests = 0
        self.job_results: JobResultsSummary = defaultdict(
            lambda: defaultdict(
                lambda: defaultdict(int)))


class Results:
    def __init__(self) -> None:
        # Mapping of {test: TestResult}
        self.test_results: MutableMapping[Text, TestResult] = defaultdict(TestResult)
        self.errors: List[Tuple[Text, bool]] = []
        self.wpt_sha: Optional[Text] = None
        self.treeherder_url: Optional[Text] = None

    def iter_results(self) -> Iterator[ResultsEntry]:
        for test_name, result in self.test_results.items():
            yield test_name, None, result
            for subtest_name, subtest_result in result.subtests.items():
                yield test_name, subtest_name, subtest_result

    def iter_filter(self, fn: Callable) -> Iterator[ResultsEntry]:
        for test_name, subtest_name, result in self.iter_results():
            if fn(test_name, subtest_name, result):
                yield test_name, subtest_name, result

    def add_jobs_from_log_files(self, logs_no_changes: Logs, logs_with_changes: Logs) -> None:
        for (browser_logs, run_has_changes) in [(logs_with_changes, True),
                                                (logs_no_changes, False)]:
            for browser, browser_job_logs in browser_logs.items():
                for job_name, job_logs in browser_job_logs.items():
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

    def add_log(self, data: Dict[Text, Any], browser: Text, job_name: Text,
                run_has_changes: bool) -> None:
        for test in data["results"]:
            use_result = run_has_changes or test["test"] in self.test_results
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

    def add_metadata(self, metadata: Metadata) -> None:
        for test, result in self.test_results.items():
            for meta_link in metadata.iterbugs(test, product="firefox"):
                if meta_link.subtest is None:
                    result.bug_links.append(meta_link)
                else:
                    if meta_link.subtest in result.subtests:
                        result.subtests[meta_link.subtest].bug_links.append(meta_link)

    def browsers(self) -> Set[Text]:
        browsers = set()
        for result in self.test_results.values():
            browsers |= {item for item in result.statuses.keys()}
        return browsers

    def job_names(self, browser: Text) -> Set[Text]:
        job_names = set()
        for result in self.test_results.values():
            if browser in result.statuses:
                for job_name in result.statuses[browser].keys():
                    job_names.add(job_name)
        return job_names

    def summary(self) -> ResultsSummary:
        summary = ResultsSummary()
        # Work out how many tests ran, etc.

        summary.parent_tests = 0
        summary.subtests = 0

        def update_for_result(result: Union[SubtestResult, TestResult]) -> None:
            for browser, browser_result in result.statuses.items():
                for job_name, job_result in browser_result.items():
                    if job_result.head:
                        summary.job_results[job_result.head][browser][job_name] += 1

        for test_result in self.test_results.values():
            summary.parent_tests += 1  # type: ignore
            summary.subtests = len(test_result.subtests)
            update_for_result(test_result)
            for subtest_result in test_result.subtests.values():
                update_for_result(subtest_result)
        return summary

    def iter_crashes(self, target_browser: Text = "firefox") -> Iterator[ResultsEntry]:
        return self.iter_filter(lambda _test, _subtest, result:
                                result.has_crash(target_browser))

    def iter_new_non_passing(self, target_browser: str = "firefox") -> Iterator[ResultsEntry]:
        return self.iter_filter(lambda _test, _subtest, result:
                                result.has_new_non_passing(target_browser))

    def iter_regressions(self, target_browser: str = "firefox") -> Iterator[ResultsEntry]:
        return self.iter_filter(lambda _test, _subtest, result:
                                result.has_regression(target_browser))

    def iter_disabled(self, target_browser: str = "firefox") -> Iterator[ResultsEntry]:
        return self.iter_filter(lambda _test, _subtest, result:
                                result.has_disabled(target_browser))

    def iter_browser_only(self, target_browser: str = "firefox") -> Iterator[ResultsEntry]:
        def is_browser_only(_test: Text, _subtest: Optional[Text], result: Result) -> bool:
            return result.is_browser_only_failure(target_browser)
        return self.iter_filter(is_browser_only)


def get_push_changeset(commit: sync_commit.GeckoCommit) -> Optional[Text]:
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
    [changeset] = list(pushes.values())[0]["changesets"]
    return changeset


def get_central_tasks(git_gecko: Repo, sync: DownstreamSync) -> Optional[TaskGroupView]:
    merge_base_commit = sync_commit.GeckoCommit(
        git_gecko,
        git_gecko.merge_base(sync.gecko_commits.head.sha1,
                             env.config["gecko"]["refs"]["central"])[0])

    hg_push_sha = get_push_changeset(merge_base_commit)
    if hg_push_sha is None:
        return None
    try:
        git_push_sha = cinnabar(git_gecko).hg2git(hg_push_sha)
    except ValueError:
        newrelic.agent.record_exception()
        return None
    push_commit = sync_commit.GeckoCommit(git_gecko, git_push_sha)
    if push_commit is None:
        return None

    taskgroup_id, state, _ = tc.get_taskgroup_id("mozilla-central",
                                                 push_commit.canonical_rev)
    if taskgroup_id is None:
        return None

    if state != "completed":
        logger.info("mozilla-central decision task has state %s" % state)
        return None

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


class LogFile:
    def __init__(self, path):
        self.path = path

    def json(self) -> Dict[Text, Any]:
        with open(self.path) as f:
            return json.load(f)


def get_logs(tasks: Iterable[Dict[Text, Any]],
             job_prefix: Text = "Gecko-") -> Mapping[Text, Mapping[Text, List[LogFile]]]:
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


def add_gecko_data(sync: DownstreamSync, results: Results) -> bool:
    complete_try_push = None

    for try_push in sorted(sync.try_pushes(), key=lambda x: -x.process_name.seq_id):
        if try_push.status == "complete":
            complete_try_push = try_push
            break

    if not complete_try_push:
        logger.info("No complete try push available for PR %s" % sync.pr)
        return False

    # Get the list of central tasks and download the wptreport logs
    central_tasks = get_central_tasks(sync.git_gecko, sync)
    if not central_tasks:
        logger.info("Not all mozilla-central results available for PR %s" % sync.pr)
        return False

    # TODO: this method should be @mut('sync')
    assert sync._lock is not None
    with try_push.as_mut(sync._lock):
        try_tasks = complete_try_push.tasks()
        if try_tasks is None:
            logger.error("Trypush didn't have a taskgroup id")
            return False
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


def add_wpt_fyi_data(sync: DownstreamSync, results: Results) -> bool:
    head_sha1 = sync.wpt_commits.head.sha1

    logs = []
    for target, run_has_changes in [("base", False),
                                    ("head", True)]:
        target_results: MutableMapping[Text, Dict[Text, List[Response]]] = defaultdict(
            dict)
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


def for_sync(sync: DownstreamSync) -> Results:
    results = Results()
    if not add_gecko_data(sync, results):
        results.errors.append(("Failed to get Gecko data", True))

    if not add_wpt_fyi_data(sync, results):
        results.errors.append(("Failed to get wptfyi data", True))

    results.add_metadata(Metadata(sync.process_name))

    return results
