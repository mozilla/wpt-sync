from collections import defaultdict

import requests

from .. import log
from .. import wptfyi
from ..bug import bug_number_from_url
from ..env import Environment

logger = log.get_logger(__name__)

env = Environment()

browsers = ["firefox", "chrome", "safari"]
passing_status = ("PASS", "OK")
statuses = ["OK", "PASS", "CRASH", "FAIL", "TIMEOUT", "ERROR", "NOTRUN"]


class TestResult(object):
    def __init__(self, name):
        self.name = name
        self.subtests = {}
        self.results = {target: {item: None for item in browsers}
                        for target in ("head", "base")}

    def result_str(self, target, browser):
        result = self.results[target].get(browser)
        if result is None:
            result = "MISSING"
        return result

    def add(self, target, browser, status):
        self.results[target][browser] = status

    def is_browser_only(self, browser):
        browser_result = self.results["head"].get(browser)

        if browser_result in passing_status:
            return False

        return all(result in passing_status
                   for (other_browser, result) in self.results["head"].iteritems()
                   if other_browser != browser)

    def is_regression(self, browser):
        return ((self.results["head"].get(browser) not in passing_status and
                 self.results["base"].get(browser) in passing_status) or
                (self.results["base"].get(browser) == "FAIL" and
                 self.results["head"].get(browser) in ("TIMEOUT", "ERROR", "CRASH", "NOTRUN")))

    def is_crash(self, browser):
        return self.results["head"].get(browser) == "CRASH"

    def is_new_non_passing(self, browser):
        new_non_passing_status = passing_status + (None,)
        return (self.results["base"].get(browser) is None and
                self.results["head"].get(browser) not in new_non_passing_status)


def results_by_test(results_by_browser):
    results = {}
    for target, browser_results in results_by_browser.iteritems():
        for browser, results_data in browser_results.iteritems():
            for test in results_data["results"]:
                name = test["test"]
                if name not in results:
                    results[name] = TestResult(name)
                results[name].add(target, browser, test["status"])
                for subtest in test["subtests"]:
                    subtest_name = subtest["name"]
                    if subtest_name not in results[name].subtests:
                        results[name].subtests[subtest_name] = TestResult(subtest_name)
                    results[name].subtests[subtest_name].add(target, browser, subtest["status"])
    return results


def get_results(head_sha1):
    results = {"base": {},
               "head": {}}

    for target, results_by_browser in results.iteritems():
        runs = wptfyi.get_runs(sha=head_sha1, labels=["pr_%s" % target])
        for run in runs:
            if run["browser_name"] in browsers:
                browser = run["browser_name"]
                results_by_browser[browser] = requests.get(run["raw_results_url"]).json()

    return results


def get_summary(browser, results):
    summary = defaultdict(int)
    for test_result in results.itervalues():
        status = test_result.result_str("head", browser)
        summary[status] += 1
        for subtest_result in test_result.subtests.itervalues():
            status = subtest_result.result_str("head", browser)
            summary[status] += 1
    return summary


def summary_message(sha, results):
    heading = ("## GitHub CI Results\nwpt.fyi "
               "[PR Results](https://wpt.fyi/results/?sha=%s&label=pr_head) "
               "[Base Results](https://wpt.fyi/results/?sha=%s&label=pr_base)\n" % (sha, sha))

    summary = "Ran %s tests" % len(results)
    num_subtests = sum(len(item.subtests) for item in results.itervalues())
    if num_subtests:
        summary += " and %i subtests\n" % num_subtests
    else:
        summary += "\n"

    results_values = []
    for browser in browsers:
        results_values.append("### %s" % browser.title())
        browser_summary = get_summary(browser, results)
        max_width = max(len(item) for item in browser_summary)
        for status in statuses:
            if status in browser_summary:
                count = browser_summary[status]
                results_values.append("  %s: %s" % (status.ljust(max_width), count))
        results_values.append("")

    return "\n".join([heading, summary] + results_values)


def get_details(results, metadata):
    details = {
        "browser_only": defaultdict(list),
        "worse_result": defaultdict(list),
        "crash": defaultdict(list),
        "new_not_pass": defaultdict(list),
    }

    def add_result(result, test_name, subtest_name):
        keys = []
        if result.is_crash("firefox"):
            keys.append("crash")
        if result.is_browser_only("firefox"):
            keys.append("browser_only")
        elif result.is_new_non_passing("firefox"):
            keys.append("new_not_pass")
        elif result.is_regression("firefox"):
            keys.append("worse_result")

        if not keys:
            return

        for key in keys:
            meta = get_meta(metadata, test_name, subtest_name,
                            result.result_str("head", "firefox"))
            details[key][test_name].append((subtest_name, result, meta))

    for test_name, test_result in results.iteritems():
        add_result(test_result, test_name, None)
        for subtest_name, subtest_result in test_result.subtests.iteritems():
            add_result(subtest_result, test_name, subtest_name)

    return details


def get_meta(metadata, test, subtest, status):
    test_metadata = metadata.get(test)
    if not test_metadata:
        return []

    specific_metadata = []
    for item in test_metadata:
        if "results" not in item:
            specific_metadata.append(item)
        else:
            specific_results = [result for result in item["results"]
                                if (item.get("subtest", subtest) == subtest and
                                    item.get("status", status) == status)]
            if specific_results:
                item["results"] = specific_results
                specific_metadata.append(item)

    return specific_metadata


def status_str(result, head_browsers, base_browsers):
    if base_browsers is None:
        base_browsers = set([])
    else:
        base_browsers = set(base_browsers)

    value_parts = []
    for browser in head_browsers:
        part = "%s: " % browser.title()
        if browser in base_browsers:
            base_result = result.result_str("base", browser)
            part += "%s->" % base_result
        head_result = result.result_str("head", browser)
        part += head_result
        value_parts.append(part)
    return ", ".join(value_parts)


def bug_str(meta):
    bugs = []
    for item in meta:
        url = item.get("url", "")
        if "bugzilla.mozilla.org" in url:
            bugs.append(bug_number_from_url(url))
    if bugs:
        return "bugs: %s" % ", ".join("Bug %s" % bug for bug in bugs)


def details_message(results, metadata):
    parts = []
    details = get_details(results, metadata)

    # TODO: Check if failures are associated with a bug
    for key, title, head_browsers, base_browsers, other_prefix, add_bug in [
            ("browser_only", "Firefox-only failures", ["firefox"], None, False, True),
            ("worse_result",
             "Existing tests that now have a worse result",
             browsers,
             browsers,
             True,
             False),
            ("new_not_pass", "New tests that's don't pass", browsers, None, True, False),
            ("crash", "### Tests that CRASH", ["firefox"], ["firefox"], False, True)]:

        if len(details[key]) == 0:
            continue

        if parts and other_prefix:
            title = "Other %s%s" % (title[0].lower(), title[1:])
        part_parts = ["### %s" % title, ""]
        for test_name, test_results in details[key].iteritems():
            if test_results[0][0] is not None:
                part_parts.append("%s" % test_name)
            for subtest_name, subtest_result, metadata in test_results:
                indent = "" if subtest_name is None else "  "
                name = subtest_name if subtest_name is not None else test_name
                status = status_str(subtest_result,
                                    head_browsers,
                                    base_browsers)
                if add_bug and metadata:
                    bug = " %s" % bug_str(metadata)
                else:
                    bug = ""
                part_parts.append("%s%s: %s%s" % (indent, name, status, bug))

            part_parts.append("")
        parts.append("\n".join(part_parts))
    return parts


def for_sync(sync):
    head_sha1 = sync.wpt_commits.head.sha1

    try:
        results_by_browser = get_results(head_sha1)
    except requests.HTTPError as e:
        logger.error("Unable to fetch results from wpt.fyi: %s" % e)
        return
    results = results_by_test(results_by_browser)
    try:
        metadata = wptfyi.get_metadata(["firefox"], "https://bugzilla.mozilla.org")
    except requests.HTTPError:
        metadata = {}
    return [summary_message(head_sha1, results)] + details_message(results, metadata)
