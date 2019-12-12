from sync.notify import results, msg
from sync.wptmeta import MetaLink


def test_results_wptfyi(env, pr_19900_github):
    results_data = results.Results()
    results_data.add_jobs_from_log_files(*pr_19900_github)
    assert results_data.summary() == {
        "parent_tests": 1,
        "subtests": 1,
        "OK": {"chrome": {"GitHub": 1},
               "safari": {"GitHub": 1}},
        "FAIL": {"chrome": {"GitHub": 1},
                 "safari": {"GitHub": 1}},
        "ERROR": {"firefox": {"GitHub": 1}},
        "NOTRUN": {"firefox": {"GitHub": 1}},
    }
    assert list(results_data.iter_browser_only()) == [(
        "/html/browsers/sandboxing/sandbox-new-execution-context.html",
        None,
        results_data.test_results[
            "/html/browsers/sandboxing/sandbox-new-execution-context.html"])]
    assert list(results_data.iter_crashes()) == []
    assert list(results_data.iter_new_non_passing()) == [
        ("/html/browsers/sandboxing/sandbox-new-execution-context.html",
         None,
         results_data.test_results[
             "/html/browsers/sandboxing/sandbox-new-execution-context.html"]),
        ("/html/browsers/sandboxing/sandbox-new-execution-context.html",
         "iframe with sandbox should load with new execution context",
         results_data.test_results[
             "/html/browsers/sandboxing/sandbox-new-execution-context.html"]
         .subtests["iframe with sandbox should load with new execution context"])]

    assert list(results_data.iter_regressions()) == []
    assert list(results_data.iter_disabled()) == []


def test_msg_wptfyi(env, pr_19900_github):
    results_data = results.Results()
    results_data.add_jobs_from_log_files(*pr_19900_github)
    results_data.wpt_sha = "6146f4a506c1b7efaac68c9e8d552597212eabca"
    message = msg.for_results(results_data)
    assert message[0] == """# CI Results

Ran 0 Firefox configurations based on mozilla-central, and Firefox, Chrome, and Safari on GitHub CI

Total 1 tests and 1 subtests

## Status Summary

### Firefox
ERROR : 1
NOTRUN: 1

### Chrome
OK    : 1
FAIL  : 1

### Safari
OK    : 1
FAIL  : 1

## Links
[GitHub PR Head](https://wpt.fyi/results/?sha=6146f4a506c1b7efaac68c9e8d552597212eabca&label=pr_head)
[GitHub PR Base](https://wpt.fyi/results/?sha=6146f4a506c1b7efaac68c9e8d552597212eabca&label=pr_base)

## Details

### Firefox-only Failures
/html/browsers/sandboxing/sandbox-new-execution-context.html: ERROR

### New Tests That Don't Pass
/html/browsers/sandboxing/sandbox-new-execution-context.html: ERROR (Chrome: OK, Safari: OK)
  iframe with sandbox should load with new execution context: NOTRUN (Chrome: FAIL, Safari: FAIL)
"""  # noqa: E501
    assert message[1] is None


def test_results_gecko(env, pr_19900_gecko_ci):
    results_data = results.Results()
    results_data.add_jobs_from_log_files(*pr_19900_gecko_ci)
    results_data.summary() == {
        "subtests": 1,
        "parent_tests": 1,
        "NOTRUN": {
            "firefox": {
                "Gecko-windows10-64-qr-debug": 1,
                "Gecko-linux64-asan-opt": 1,
                "Gecko-windows7-32-debug": 1,
                "Gecko-windows10-64-qr-opt": 1,
                "Gecko-android-em-7.0-x86_64-debug-geckoview": 1,
                "Gecko-windows7-32-opt": 1,
                "Gecko-linux64-qr-debug": 1,
                "Gecko-linux64-opt": 1,
                "Gecko-android-em-7.0-x86_64-opt-geckoview": 1,
                "Gecko-windows10-64-debug": 1,
                "Gecko-linux64-qr-opt": 1,
                "Gecko-linux64-debug": 1,
                "Gecko-windows10-64-opt": 1
            }
        },
        "ERROR": {
            "firefox": {
                "Gecko-windows10-64-qr-debug": 1,
                "Gecko-linux64-asan-opt": 1,
                "Gecko-windows7-32-debug": 1,
                "Gecko-windows10-64-qr-opt": 1,
                "Gecko-android-em-7.0-x86_64-debug-geckoview": 1,
                "Gecko-windows7-32-opt": 1,
                "Gecko-linux64-qr-debug": 1,
                "Gecko-linux64-opt": 1,
                "Gecko-android-em-7.0-x86_64-opt-geckoview": 1,
                "Gecko-windows10-64-debug": 1,
                "Gecko-linux64-qr-opt": 1,
                "Gecko-linux64-debug": 1,
                "Gecko-windows10-64-opt": 1
            }
        }
    }

    assert list(results_data.iter_browser_only()) == []
    assert list(results_data.iter_crashes()) == []
    assert list(results_data.iter_new_non_passing()) == [
        ("/html/browsers/sandboxing/sandbox-new-execution-context.html",
         None,
         results_data.test_results[
             "/html/browsers/sandboxing/sandbox-new-execution-context.html"]),
        ("/html/browsers/sandboxing/sandbox-new-execution-context.html",
         "iframe with sandbox should load with new execution context",
         results_data.test_results[
             "/html/browsers/sandboxing/sandbox-new-execution-context.html"]
         .subtests["iframe with sandbox should load with new execution context"])]

    assert list(results_data.iter_regressions()) == []
    assert list(results_data.iter_disabled()) == []


def test_msg_gecko(env, pr_19900_gecko_ci):
    results_data = results.Results()
    results_data.add_jobs_from_log_files(*pr_19900_gecko_ci)
    results_data.treeherder_url = ("https://treeherder.mozilla.org/#/jobs?"
                                   "repo=try&"
                                   "revision=b0337497587b2bac7d2baeecea0d873df8bcb4f4")
    message = msg.for_results(results_data)
    assert message[0] == """# CI Results

Ran 13 Firefox configurations based on mozilla-central

Total 1 tests and 1 subtests

## Status Summary

### Firefox
ERROR : 1
NOTRUN: 1

## Links
[Gecko CI (Treeherder)](https://treeherder.mozilla.org/#/jobs?repo=try&revision=b0337497587b2bac7d2baeecea0d873df8bcb4f4)

## Details

### New Tests That Don't Pass
/html/browsers/sandboxing/sandbox-new-execution-context.html: ERROR
  iframe with sandbox should load with new execution context: NOTRUN
"""  # noqa: E501


def test_msg_both(env, pr_19900_gecko_ci, pr_19900_github):
    results_data = results.Results()
    results_data.add_jobs_from_log_files(*pr_19900_gecko_ci)
    results_data.treeherder_url = ("https://treeherder.mozilla.org/#/jobs?"
                                   "repo=try&"
                                   "revision=b0337497587b2bac7d2baeecea0d873df8bcb4f4")
    results_data.add_jobs_from_log_files(*pr_19900_github)
    results_data.wpt_sha = "6146f4a506c1b7efaac68c9e8d552597212eabca"
    message = msg.for_results(results_data)
    assert message[0] == """# CI Results

Ran 13 Firefox configurations based on mozilla-central, and Firefox, Chrome, and Safari on GitHub CI

Total 1 tests and 1 subtests

## Status Summary

### Firefox
ERROR : 1
NOTRUN: 1

### Chrome
OK    : 1
FAIL  : 1

### Safari
OK    : 1
FAIL  : 1

## Links
[Gecko CI (Treeherder)](https://treeherder.mozilla.org/#/jobs?repo=try&revision=b0337497587b2bac7d2baeecea0d873df8bcb4f4)
[GitHub PR Head](https://wpt.fyi/results/?sha=6146f4a506c1b7efaac68c9e8d552597212eabca&label=pr_head)
[GitHub PR Base](https://wpt.fyi/results/?sha=6146f4a506c1b7efaac68c9e8d552597212eabca&label=pr_base)

## Details

### Firefox-only Failures
/html/browsers/sandboxing/sandbox-new-execution-context.html: ERROR

### New Tests That Don't Pass
/html/browsers/sandboxing/sandbox-new-execution-context.html: ERROR (Chrome: OK, Safari: OK)
  iframe with sandbox should load with new execution context: NOTRUN (Chrome: FAIL, Safari: FAIL)
"""  # noqa: E501


def test_status_str(env):
    result = results.Result()
    result.set_status("firefox", "GitHub", False, "PASS", ["PASS"])
    result.set_status("firefox", "GitHub", True, "FAIL", ["PASS"])
    result.set_status("chrome", "GitHub", False, "PASS", ["PASS"])
    result.set_status("chrome", "GitHub", True, "PASS", ["PASS"])

    with_both_statuses = msg.status_str(result,
                                        include_status="both",
                                        include_other_browser=False)
    assert with_both_statuses == "PASS->FAIL"

    with_other_browser = msg.status_str(result,
                                        include_status="both",
                                        include_other_browser=True)
    assert with_other_browser == "PASS->FAIL (Chrome: PASS->PASS)"

    result = results.Result()
    result.set_status("firefox", "platform1", False, "PASS", ["PASS"])
    result.set_status("firefox", "platform1", True, "FAIL", ["PASS"])
    result.set_status("firefox", "platform2", False, "PASS", ["PASS"])
    result.set_status("firefox", "platform2", True, "PASS", ["PASS"])
    result.set_status("firefox", "platform3", False, "PASS", ["PASS"])
    result.set_status("firefox", "platform3", True, "PASS", ["PASS"])

    with_platform_difference = msg.status_str(result,
                                              include_status="both",
                                              include_other_browser=False)
    assert with_platform_difference == ("PASS->FAIL [`platform1`], "
                                        "PASS->PASS [`platform2`, `platform3`]")

    with_platform_difference_head = msg.status_str(result,
                                                   include_status="head",
                                                   include_other_browser=False)
    assert with_platform_difference_head == "FAIL [`platform1`], PASS [`platform2`, `platform3`]"


def test_link(env):
    result0 = results.Result()
    result0.set_status("firefox", "GitHub", False, "PASS", ["PASS"])
    result0.set_status("firefox", "GitHub", True, "FAIL", ["PASS"])
    result0.bug_links.append(MetaLink(None,
                                      "%s/show_bug.cgi?id=1234" % env.bz.bz_url,
                                      "firefox",
                                      "/test/test0.html"))
    result1 = results.Result()
    result1.set_status("firefox", "GitHub", False, "PASS", ["PASS"])
    result1.set_status("firefox", "GitHub", True, "FAIL", ["PASS"])
    result1.bug_links.append(MetaLink(None,
                                      "https://github.com/web-platform-tests/wpt/issues/123",
                                      "firefox",
                                      "/test/test1.html"))

    results_iter = [("/test/test0.html", None, result0),
                    ("/test/test1.html", None, result1)]
    data = msg.detail_part("Test", results_iter, include_bugs=("bugzilla", "github"),
                           include_status="head", include_other_browser=True)
    assert data == """### Test
/test/test0.html: FAIL linked bug:Bug 1234
/test/test1.html: FAIL linked bug:[Issue 123](https://github.com/web-platform-tests/wpt/issues/123)
"""  # noqa: E501
