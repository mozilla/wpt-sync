import mock
from collections import defaultdict

from sync.notify import geckomsg, wptfyimsg


def test_parse_logs_gecko(open_wptreport_path):
    with mock.patch("sync.notify.geckomsg.open", open_wptreport_path):
        log_data = defaultdict(dict)
        geckomsg.parse_logs({"test-1": ["try1.json"]}, log_data, True)
        geckomsg.parse_logs({"test-1": ["central.json"]}, log_data, False)

        summary = geckomsg.get_summary(log_data)
        assert summary == {
            "parent_tests": {
                "test-1": 5
            },
            "subtests": {
                "test-1": 4
            },
            "OK": {
                "test-1": 1
            },
            "PASS": {
                "test-1": 2
            },
            "FAIL": {
                "test-1": 5
            },
            "CRASH": {
                "test-1": 1
            }
        }

        details = geckomsg.get_details(log_data)
        assert len(details["crash"]) == 1
        assert len(details["disabled"]) == 0
        assert len(details["new_not_pass"]) == 2
        assert len(details["worse_result"]) == 2

        assert (geckomsg.summary_message(["test-1"], summary) ==
                """## Gecko CI Results

Ran 5 tests and 4 subtests
  OK     : 1
  PASS   : 2
  CRASH  : 1
  FAIL   : 5
""")

        assert (geckomsg.details_message(["test-1"], details) ==
                ["""### Tests that CRASH
/test3.html
""",

                 """### Existing tests that now have a worse result
/test1.html
  Subtest 2: FAIL
/test2.html: FAIL
""",
                 """### New tests that don't pass
/test1.html
  Subtest 4: FAIL
/test4.html: FAIL
"""])


def test_parse_logs_multi_gecko(open_wptreport_path):
    with mock.patch("sync.notify.geckomsg.open", open_wptreport_path):

        log_data = defaultdict(dict)
        geckomsg.parse_logs({"test-1": ["try1.json"],
                             "test-2": ["try2.json"]}, log_data, True)
        geckomsg.parse_logs({"test-1": ["central.json"],
                             "test-2": ["central.json"]}, log_data, False)

        summary = geckomsg.get_summary(log_data)
        details = geckomsg.get_details(log_data)

        assert (geckomsg.summary_message(["test-1", "test-2"], summary) ==
                """## Gecko CI Results

Ran 5 tests and 4 subtests
  OK     : 1
  PASS   : 2[test-1], 4[test-2]
  CRASH  : 1[test-1]
  FAIL   : 4[test-2], 5[test-1]
""")
    assert (geckomsg.details_message(["test-1", "test-2"], details) ==
            ["""### Tests that CRASH
/test3.html: [test-1]
""",
             """### Existing tests that now have a worse result
/test1.html
  Subtest 2: FAIL[test-1]
/test2.html: FAIL
""",

             """### New tests that don't pass
/test1.html
  Subtest 4: FAIL
/test4.html: FAIL[test-1]
"""])


def test_notify_wptfyi(wptfyi_pr_results):
    head_sha1, results_by_browser = wptfyi_pr_results
    results = wptfyimsg.results_by_test(results_by_browser)
    assert (wptfyimsg.summary_message(head_sha1, results) ==
            """## GitHub CI Results
wpt.fyi [PR Results](https://wpt.fyi/results/?sha=fcf424c168778e2eaf2a6ca31d19339e3e36beac&label=pr_head) [Base Results](https://wpt.fyi/results/?sha=fcf424c168778e2eaf2a6ca31d19339e3e36beac&label=pr_base)

Ran 40 tests and 273 subtests

### Firefox
  OK     : 38
  PASS   : 227
  FAIL   : 40
  ERROR  : 2

### Chrome
  OK  : 40
  PASS: 244
  FAIL: 29

### Safari
  OK  : 40
  PASS: 172
  FAIL: 101
""")  # noqa: E501

    details = wptfyimsg.details_message(results)
    assert len(details) == 2
    assert (details[0] ==
            """### Firefox-only failures

/cookies/samesite/form-get-blank.https.html?legacy-samesite
   Cross-site redirecting to subdomain top-level form GETs are strictly same-site: Firefox: FAIL
   Cross-site redirecting to same-host top-level form GETs are strictly same-site: Firefox: FAIL

/cookies/samesite/form-post-blank-reload.https.html?legacy-samesite: Firefox: ERROR
   Reloaded same-host top-level form POSTs are strictly same-site: Firefox: MISSING
   Reloaded subdomain top-level form POSTs are strictly same-site: Firefox: MISSING

/cookies/samesite/iframe.https.html
   Cross-site redirecting to same-host fetches are strictly same-site: Firefox: FAIL
   Cross-site redirecting to subdomain fetches are strictly same-site: Firefox: FAIL

/cookies/samesite/form-post-blank.https.html
   Cross-site redirecting to same-host top-level form POSTs are strictly same-site: Firefox: FAIL
   Cross-site redirecting to subdomain top-level form POSTs are strictly same-site: Firefox: FAIL

/cookies/samesite/form-post-blank-reload.https.html: Firefox: ERROR
   Reloaded same-host top-level form POSTs are strictly same-site: Firefox: MISSING
   Reloaded subdomain top-level form POSTs are strictly same-site: Firefox: MISSING

/cookies/samesite/form-get-blank.https.html
   Cross-site redirecting to subdomain top-level form GETs are strictly same-site: Firefox: FAIL
   Cross-site redirecting to same-host top-level form GETs are strictly same-site: Firefox: FAIL

/cookies/samesite/img.https.html?legacy-samesite
   Cross-site redirecting to same-host images are strictly same-site: Firefox: FAIL
   Cross-site redirecting to subdomain images are strictly same-site: Firefox: FAIL

/cookies/samesite/fetch.https.html?legacy-samesite
   Cross-site redirecting to same-host fetches are strictly same-site: Firefox: FAIL
   Cross-site redirecting to subdomain fetches are strictly same-site: Firefox: FAIL

/cookies/samesite/iframe.https.html?legacy-samesite
   Cross-site redirecting to same-host fetches are strictly same-site: Firefox: FAIL
   Cross-site redirecting to subdomain fetches are strictly same-site: Firefox: FAIL

/cookies/samesite/form-post-blank.https.html?legacy-samesite
   Cross-site redirecting to same-host top-level form POSTs are strictly same-site: Firefox: FAIL
   Cross-site redirecting to subdomain top-level form POSTs are strictly same-site: Firefox: FAIL

/cookies/samesite/img.https.html
   Cross-site redirecting to same-host images are strictly same-site: Firefox: FAIL
   Cross-site redirecting to subdomain images are strictly same-site: Firefox: FAIL

/cookies/samesite/fetch.https.html
   Cross-site redirecting to same-host fetches are strictly same-site: Firefox: FAIL
   Cross-site redirecting to subdomain fetches are strictly same-site: Firefox: FAIL
""")
    assert (details[1] == """### Other new tests that's don't pass

/cookies/samesite/blob-toplevel.https.html
  SameSite cookies with blob child window: Firefox: FAIL, Chrome: FAIL, Safari: FAIL
""")  # noqa: E501


def test_notify_wptfyi_value_str():
    result = wptfyimsg.TestResult("test")
    result.results = {
        "head": {"firefox": "FAIL"},
        "base": {"firefox": "PASS"}
    }

    assert wptfyimsg.value_str(result, ["firefox"], ["firefox"]) == "Firefox: PASS->FAIL"
