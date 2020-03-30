import json
from mock import Mock, patch

from sync import downstream, load, meta
from sync.base import FrozenDict
from sync.lock import SyncLock
from sync.notify import results, bugs
from sync.wptmeta import MetaLink


def test_fallback_test_ids_to_paths(env):
    test_ids = ["/IndexedDB/key-generators/reading-autoincrement-indexes.any.html",
                "/IndexedDB/key-generators/reading-autoincrement-indexes.any.serviceworker.html",
                "/cookie-store/cookieStore_event_arguments.tentative.https.window.html",
                "/css/geometry/DOMMatrix-css-string.worker.html",
                "/css/geometry/DOMMatrix-003.html",
                "/_mozilla/tests/example.html"]
    assert bugs.fallback_test_ids_to_paths(test_ids) == {
        "testing/web-platform/tests/IndexedDB/key-generators/reading-autoincrement-indexes.any.js":
        ["/IndexedDB/key-generators/reading-autoincrement-indexes.any.html",
         "/IndexedDB/key-generators/reading-autoincrement-indexes.any.serviceworker.html"],
        "testing/web-platform/tests/cookie-store/cookieStore_event_arguments."
        "tentative.https.window.js":
        ["/cookie-store/cookieStore_event_arguments.tentative.https.window.html"],
        "testing/web-platform/tests/css/geometry/DOMMatrix-css-string.worker.js":
        ["/css/geometry/DOMMatrix-css-string.worker.html"],
        "testing/web-platform/tests/css/geometry/DOMMatrix-003.html":
        ["/css/geometry/DOMMatrix-003.html"],
        "testing/web-platform/mozilla/tests/tests/example.html":
        ["/_mozilla/tests/example.html"]
    }


def fx_only_failure():
    result = results.TestResult()
    result.set_status("firefox", "GitHub", False, "PASS", ["PASS"])
    result.set_status("firefox", "GitHub", True, "FAIL", ["PASS"])
    result.set_status("chrome", "GitHub", False, "PASS", ["PASS"])
    result.set_status("chrome", "GitHub", True, "PASS", ["PASS"])

    results_obj = results.Results()
    results_obj.test_results = {"/test/test.html": result}
    results_obj.wpt_sha = "abcdef"
    results_obj.treeherder_url = "https://treeherder.mozilla.org"

    return results_obj


def fx_crash():
    result = results.TestResult()
    result.set_status("firefox", "GitHub", False, "PASS", ["PASS"])
    result.set_status("firefox", "GitHub", True, "CRASH", ["PASS"])

    results_obj = results.Results()
    results_obj.test_results = {"/test/test.html": result}
    results_obj.wpt_sha = "abcdef"
    results_obj.treeherder_url = "https://treeherder.mozilla.org"

    return results_obj


def test_msg_failure():
    results_obj = fx_only_failure()

    class Sync(object):
        pr = "1234"
        bug = "100000"

    output = bugs.bug_data_failure(Sync(),
                                   [("/test/test.html",
                                     None,
                                     results_obj.test_results["/test/test.html"])],
                                   "https://treeherder.mozilla.org/#/jobs?repo=try&revision=1234",
                                   "12345")
    assert output == (
        'New wpt failures in /test/test.html',
        """Syncing wpt [PR 1234](https://github.com/web-platform-tests/wpt/pull/1234)\
 found new untriaged test failures in CI

# Tests Affected

### Tests with a Worse Result After Changes
/test/test.html: FAIL (Chrome: PASS)


# CI Results

[Gecko CI (Treeherder)](https://treeherder.mozilla.org/#/jobs?repo=try&revision=1234)
[GitHub PR Head](https://wpt.fyi/results/?sha=12345&label=pr_head)

# Notes

These updates will be on mozilla-central once bug 100000 lands.

Note: this bug is for tracking fixing the issues and is not
owned by the wpt sync bot.

This bug is linked to the relevant tests by an annotation in
https://github.com/web-platform-tests/wpt-metadata. These annotations
can be edited using the wpt interop dashboard
https://jgraham.github.io/wptdash/

If this bug is split into multiple bugs, please also update the
annotations, otherwise we are unable to track which wpt issues are
already triaged. Resolving as duplicate or closing this issue should
be cause the bot to automatically update or remove the annotation.
""")


def test_fx_only(env):
    results_obj = fx_only_failure()
    sync = Mock()
    sync.lock_key = ("downstream", None)
    sync.notify_bugs = FrozenDict()
    env.config["notify"]["components"] = "Foo :: Bar, Testing :: web-platform-tests"
    with patch("sync.notify.bugs.components_for_wpt_paths",
               return_value={"Testing :: web-platform-tests": ["test/test.html"]}):
        with patch("sync.notify.bugs.test_ids_to_paths",
                   return_value={"testing/web-platform/tests/test/test.html":
                                 ["/test/test.html"]}):
            bug_data = bugs.for_sync(sync, results_obj)

    assert list(bug_data.values()) == [[("/test/test.html",
                                         None,
                                         results_obj.test_results["/test/test.html"],
                                         None)]]

    assert "Creating a bug in component Testing :: web-platform-tests" in env.bz.output.getvalue()
    assert "Type: defect" in env.bz.output.getvalue()


def test_crash(env):
    results_obj = fx_crash()
    sync = Mock()
    sync.lock_key = ("downstream", None)
    sync.notify_bugs = FrozenDict()
    env.config["notify"]["components"] = "Foo :: Bar, Testing :: web-platform-tests"
    with patch("sync.notify.bugs.components_for_wpt_paths",
               return_value={"Testing :: web-platform-tests": ["test/test.html"]}):
        with patch("sync.notify.bugs.test_ids_to_paths",
                   return_value={"testing/web-platform/tests/test/test.html":
                                 ["/test/test.html"]}):
            bug_data = bugs.for_sync(sync, results_obj)

    assert list(bug_data.values()) == [[("/test/test.html",
                                         None,
                                         results_obj.test_results["/test/test.html"],
                                         "CRASH")]]

    assert "Creating a bug in component Testing :: web-platform-tests" in env.bz.output.getvalue()


def test_update_metadata(env, git_gecko, git_wpt, pull_request, git_wpt_metadata, mock_mach):
    results_obj = fx_only_failure()

    pr = pull_request([("Test commit", {"README": "Example change\n"})],
                      "Test PR")

    downstream.new_wpt_pr(git_gecko, git_wpt, pr)
    sync = load.get_pr_sync(git_gecko, git_wpt, pr["number"])

    mock_mach.set_data("wpt-test-paths",
                       json.dumps(bugs.fallback_test_ids_to_paths(["/test/test.html"])))

    with patch("sync.notify.bugs.components_for_wpt_paths",
               return_value={"Testing :: web-platform-tests": ["test/test.html"]}):
        with patch("sync.notify.bugs.Mach", return_value=mock_mach(None)):
            with patch("sync.meta.Metadata.github") as mock_github:
                with SyncLock.for_process(sync.process_name) as lock:
                    with sync.as_mut(lock):
                        bug_data = bugs.for_sync(sync, results_obj)
                        bugs.update_metadata(sync, bug_data)

                assert mock_github.create_pull.called
                head = mock_github.method_calls[0].args[-1]

    bugs_filed = bug_data.keys()
    assert len(bugs_filed) == 1
    bug = bugs_filed[0]

    metadata = meta.Metadata(sync.process_name, branch=head)
    links = list(metadata.iterbugs("/test/test.html"))
    assert len(links) == 1
    link = links[0]
    assert link.url == "%s/show_bug.cgi?id=%s" % (env.bz.bz_url, bug)
    assert link.test_id == "/test/test.html"
    assert link.product == "firefox"
    assert link.subtest is None
    assert link.status is None


def test_already_linked(env):
    results_obj = fx_only_failure()
    results_obj.test_results["/test/test.html"].bug_links.append(
        MetaLink(None,
                 "%s/show_bug.cgi?id=10000" % (env.bz.bz_url,),
                 "firefox",
                 "/test/test.html",
                 None,
                 None))
    sync = Mock()
    sync.lock_key = ("downstream", None)
    sync.notify_bugs = FrozenDict()
    env.config["notify"]["components"] = "Foo :: Bar, Testing :: web-platform-tests"
    with patch("sync.notify.bugs.components_for_wpt_paths",
               return_value={"Testing :: web-platform-tests": ["test/test.html"]}):
        with patch("sync.notify.bugs.test_ids_to_paths",
                   return_value={"testing/web-platform/tests/test/test.html":
                                 ["/test/test.html"]}):
            bug_data = bugs.for_sync(sync, results_obj)

    assert len(bug_data) == 0

    assert ("Creating a bug in component Testing :: web-platform-tests"
            not in env.bz.output.getvalue())


def test_get_common_prefix():
    assert (bugs.get_common_prefix(["/a/b/c.html", "/a/b/c.html?1=/2"]) ==
            ([("", "a", "b", "c.html"), ("", "a", "b", "c.html?1=/2")],
             ("", "a", "b")))
    assert (bugs.get_common_prefix(["/a/b/c.html", "/a/c/d.html"]) ==
            ([("", "a", "b", "c.html"), ("", "a", "c", "d.html")],
             ("", "a")))


def test_make_summary():
    test_results = ["/a/b/c.html", "/a/b/d.html", "/a/b/e.html", "/a/b/f.html",
                    "/a/b/g.html"]
    test_results = [(item, None, None) for item in test_results]
    prefix = "Test failures"

    assert (bugs.make_summary(test_results, prefix) ==
            "Test failures in /a/b/ [c.html, d.html, e.html, and 2 others]")
    assert (bugs.make_summary(test_results, prefix, max_length=55) ==
            "Test failures in /a/b/ [c.html, d.html, and 3 others]")
    assert (bugs.make_summary(test_results, prefix, max_length=50) ==
            "Test failures in /a/b/ [c.html, and 4 others]")
    assert (bugs.make_summary(test_results, prefix, max_length=35) ==
            "Test failures in /a/b/ [5 tests]")
    assert (bugs.make_summary(test_results, prefix, max_length=25) ==
            "Test failures in /a/b/")
    assert (bugs.make_summary(test_results, prefix, max_length=15) ==
            "Test failures")

    test_results = ["/a/b/c.html", "/a/b/d.html", "/a/b/e.html"]
    test_results = [(item, None, None) for item in test_results]
    assert (bugs.make_summary(test_results, prefix) ==
            "Test failures in /a/b/ [c.html, d.html, e.html]")


def test_split_id():
    assert bugs.split_id("/a/b/c.html") == ("", "a", "b", "c.html")
    assert bugs.split_id("/a/b/c.html?foo=bar/baz") == ("", "a", "b", "c.html?foo=bar/baz")
    assert bugs.split_id("/a/b/c.html#bar/baz") == ("", "a", "b", "c.html#bar/baz")
    assert (bugs.split_id("/a/b/c.html?foo=bar/baz#bar/baz") ==
            ("", "a", "b", "c.html?foo=bar/baz#bar/baz"))


def test_already_filed(env):
    results_obj = fx_only_failure()
    sync = Mock()
    sync.lock_key = ("downstream", None)
    sync.notify_bugs = FrozenDict(**{"failure :: Testing :: web-platform-tests": "1234"})

    env.config["notify"]["components"] = "Foo :: Bar, Testing :: web-platform-tests"
    with patch("sync.notify.bugs.components_for_wpt_paths",
               return_value={"Testing :: web-platform-tests": ["test/test.html"]}):
        with patch("sync.notify.bugs.test_ids_to_paths",
                   return_value={"testing/web-platform/tests/test/test.html":
                                 ["/test/test.html"]}):
            bug_data = bugs.for_sync(sync, results_obj)

    assert len(bug_data) == 1
    assert list(bug_data.keys()) == ["1234"]
    assert ("Creating a bug in component Testing :: web-platform-tests"
            not in env.bz.output.getvalue())
