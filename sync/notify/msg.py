from collections import defaultdict

from six import itervalues
from six.moves import urllib

from ..bug import bug_number_from_url, max_comment_length
from ..env import Environment

from .results import statuses, browsers

MYPY = False
if MYPY:
    from sync.notify.results import Result, Results, SubtestResult, TestResult
    from sync.notify.results import ResultsEntry
    from typing import List, Iterable, Mapping, Optional, Text, Tuple, Union

env = Environment()


def status_str(result: Union[Result, SubtestResult, TestResult],
               browser: Text = "firefox",
               include_status: Text = "head",
               include_other_browser: bool = False,
               ) -> Optional[Text]:
    """Construct a string containing the statuses for a results.

    :param result: The Result object for which to construct the string.
    :param browser: The primary browser for which to construct the result
    :param include_status: Either "head" to just include the updated status or
                           "both" to include both the head and base statuses.
    :param include_other_browser: Boolean indicating whether to include results from
                                  non-primary browsers.
    """
    targets = {"head": ["head"],
               "both": ["base", "head"]}[include_status]

    if all(result.is_consistent(browser, target) for target in targets):
        value = "->".join(f"`{getattr(next(itervalues(result.statuses[browser])), target)}`"
                          for target in targets)
    else:
        by_value = defaultdict(list)
        results = result.statuses[browser]
        for job_name, status in results.items():
            key = tuple(getattr(status, target) for target in targets)
            by_value[key].append(job_name)

        value = ", ".join("{} [{}]".format("->".join(f"`{status}`" for status in statuses),
                                           ", ".join("`%s`" % item for item in sorted(job_names)))
                          for statuses, job_names in sorted(by_value.items()))

    if include_other_browser:
        other_browser_values = []
        for other_browser, job_results in sorted(result.statuses.items()):
            if other_browser == browser:
                continue
            browser_status = job_results.get("GitHub")
            if not browser_status:
                return None
            other_browser_values.append("{}: {}".format(other_browser.title(),
                                                        "->".join(
                                                        f"`{getattr(browser_status, target)}`"
                                                        for target in targets)))
        if other_browser_values:
            value += " (%s)" % ", ".join(other_browser_values)
    return value


def summary_value(result_data: Mapping[Text, int]) -> Text:
    by_result = defaultdict(list)
    for job_name, value in result_data.items():
        by_result[value].append(job_name)

    if len(by_result) == 1:
        return str(next(iter(by_result.keys())))

    return " ".join("{}[{}]".format(count, ", ".join(sorted(jobs)))
                    for count, jobs in sorted(by_result.items()))


def bug_str(url: Text) -> Text:
    """Create a bug string for a given bug url"""
    if url.startswith(env.bz.bz_url):
        return "Bug %s" % bug_number_from_url(url)
    elif url.startswith("https://github.com"):
        return "[Issue {}]({})".format(urllib.parse.urlsplit(url).path.split("/")[-1],
                                       url)
    return "[%s]()" % url


def list_join(items_iter: Iterable) -> Text:
    """Join a list of strings using commands, with "and" before the final item."""
    items = list(items_iter)
    if len(items) == 0:
        return ""
    if len(items) == 1:
        return items[0]
    rv = ", ".join(items[:-1])
    rv += ", and %s" % items[-1]
    return rv


def summary_message(results: Results) -> Text:
    """Generate a summary message for results indicating how many tests ran"""
    summary = results.summary()

    job_names = {browser: results.job_names(browser) for browser in browsers}

    github_browsers = list_join(browser.title() for browser in browsers
                                if "GitHub" in job_names[browser])

    gecko_configs = len([item for item in job_names["firefox"] if item != "GitHub"])
    data = ["Ran {} Firefox configurations based on mozilla-central".format(gecko_configs)]
    if github_browsers:
        data[-1] += ", and {} on GitHub CI".format(github_browsers)
    data.append("")
    data.append("Total %s tests" % summary.parent_tests)

    subtests = summary.subtests
    if subtests:
        data[-1] += " and %s subtests" % subtests
    data[-1] += "\n"

    result_statuses = [status for status in statuses if status in summary.job_results]
    if not result_statuses:
        return "\n".join(data)

    data.append("## Status Summary\n")

    max_width = len(max(result_statuses, key=len))
    for browser in browsers:
        if not job_names[browser]:
            continue

        data.append("### %s" % browser.title())
        for result in ["OK", "PASS", "CRASH", "FAIL", "PRECONDITION_FAILED", "TIMEOUT",
                       "ERROR", "NOTRUN"]:
            if browser in summary.job_results[result]:
                result_data = summary.job_results[result][browser]
                data.append("{}: {}".format(f"`{result}`".ljust(max_width + 2),
                                            summary_value(result_data)))
        data.append("")

    return "\n".join(data)


def links_message(results: Results) -> Text:
    """Generate a list of relevant links for the results"""
    data = []

    if results.treeherder_url is not None:
        data.append("[Gecko CI (Treeherder)](%s)" % results.treeherder_url)

    if results.wpt_sha is not None:
        data.append("[GitHub PR Head](https://wpt.fyi/results/?sha=%s&label=pr_head)" %
                    results.wpt_sha)
        data.append("[GitHub PR Base](https://wpt.fyi/results/?sha=%s&label=pr_base)" %
                    results.wpt_sha)

    if data:
        data.insert(0, "## Links")
        data.append("")

    return "\n".join(data)


def detail_message(results: Results) -> List[Text]:
    """Generate a message for results highlighting specific noteworthy test outcomes"""
    data = []

    for (details_type, iterator, include_bugs, include_status, include_other_browser) in [
            ("Crashes", results.iter_crashes("firefox"), ("bugzilla",), "head", False),
            ("Firefox-only Failures", results.iter_browser_only("firefox"),
             ("bugzilla", "github"),
             "head",
             False),
            ("Tests With a Worse Result After Changes",
             results.iter_regressions("firefox"), (), "both", True),
            ("New Tests That Don't Pass",
             results.iter_new_non_passing("firefox"), (), "head", True),
            ("Tests Disabled in Gecko Infrastructure",
             results.iter_disabled(),
             ("bugzilla", "github"),
             "head",
             True)]:

        part = detail_part(details_type, iterator, include_bugs, include_status,
                           include_other_browser)
        if part:
            data.append(part)

    if data:
        data.insert(0, "## Details\n")

    return data


def detail_part(details_type: Optional[Text],
                iterator: Iterable[ResultsEntry],
                include_bugs: Optional[Tuple[Text, ...]],
                include_status: Text,
                include_other_browser: bool,
                ) -> Optional[Text]:
    """Generate a message for a specific class of notable results.

    :param details_type: The name of the results class
    :param iterator: An iterator over all results that belong to the class
    :param include bugs: A list of bug systems' whose bug links should go in the status string
                         (currently can be "bugzilla" and/or "github"
    :param include_status: "head" or "both" indicating whether only the status with changes or
                           both the statuses before and after changes should be included
    :param include_other_browser: A boolean indicating whether to include statuses from
                                  non-Firefox browsers

    :returns: A text string containing the message
    """
    bug_prefixes = {"bugzilla": env.bz.bz_url,
                    "github": "https://github.com/"}

    item_data = []

    results = list(iterator)

    if not results:
        return None

    if details_type:
        item_data.append("### %s" % details_type)

    prev_test = None
    for test, subtest, result in results:
        msg_line = ""
        if prev_test != test:
            msg_line = (f"* [{test}](https://wpt.live{test}) " +
                        f"[[wpt.fyi](https://wpt.fyi/results{test})]")
            prev_test = test
        status = status_str(result,
                            include_status=include_status,
                            include_other_browser=include_other_browser)
        if not subtest:
            msg_line += ": %s" % status
        else:
            if msg_line:
                msg_line += "\n"
            msg_line += "  * {}: {}".format(subtest, status)

        if include_bugs:
            prefixes = [bug_prefixes[item] for item in include_bugs]

            bug_links = [bug_link for bug_link in result.bug_links
                         if any(bug_link.url.startswith(prefix) for prefix in prefixes)]
            if bug_links:
                msg_line += " linked bug{}:{}".format("s" if len(bug_links) > 1 else "",
                                                      ", ". join(bug_str(link.url)
                                                                 for link in bug_links))
        item_data.append(msg_line)
    return "\n".join(item_data) + "\n"


def for_results(results: Results) -> Tuple[Text, Optional[Text]]:
    """Generate a notification message for results

    :param results: a Results object
    :returns: A string message, and truncated_message which is None
              if the message all fits in a bugzilla comment, or the
              length-truncated version if it doesn't."""

    msg_parts = ["# CI Results\n",
                 summary_message(results),
                 links_message(results)]
    msg_parts += detail_message(results)
    msg_parts = [item for item in msg_parts if item]
    truncated, message = truncate_message(msg_parts)
    if truncated:
        truncated_message: Optional[Text] = message
        message = "\n".join(msg_parts)
    else:
        truncated_message = None

    return message, truncated_message


def truncate_message(parts: Iterable[Text]) -> Tuple[bool, Text]:
    """Take an iterator of message parts and return a string consisting of
    all the parts starting from the first that will fit into a
    bugzilla comment, seperated by new lines.

    :param parts: Iterator returning strings consisting of message parts
    :returns: a boolean indicating whether the message was truncated, and the
              truncated message.

    """
    suffix = "(See attachment for full changes)"
    message = ""
    truncated = False

    padding = len(suffix) + 1
    for part in parts:
        if len(message) + len(part) + 1 > max_comment_length - padding:
            truncated = True
            part = suffix
        if message:
            message += "\n"
        message += part
        if truncated:
            break

    return truncated, message
