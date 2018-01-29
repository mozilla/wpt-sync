from collections import defaultdict

from sync import notify


def test_parse_logs(open_wptreport_path):
    notify.open = open_wptreport_path

    log_data = defaultdict(dict)
    notify.parse_logs({"test-1": ["try1.json"]}, log_data, True)
    notify.parse_logs({"test-1": ["central.json"]}, log_data, False)

    summary = notify.get_summary(log_data)
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

    details = notify.get_details(log_data)
    assert len(details["crash"]) == 1
    assert len(details["disabled"]) == 0
    assert len(details["new_not_pass"]) == 2
    assert len(details["worse_result"]) == 2

    assert (notify.message(["test-1"], summary, details) ==
            """Ran 5 tests and 4 subtests
OK     : 1
PASS   : 2
CRASH  : 1
FAIL   : 5

These tests crash:
/test3.html

These tests have a worse result after import (e.g. they used to PASS and now FAIL)
/test1.html
    Subtest 2: FAIL
/test2.html: FAIL

These new tests don't PASS on all platforms
/test1.html
    Subtest 4: FAIL
/test4.html: FAIL
""")


def test_parse_logs_multi(open_wptreport_path):
    notify.open = open_wptreport_path

    log_data = defaultdict(dict)
    notify.parse_logs({"test-1": ["try1.json"], "test-2": ["try2.json"]}, log_data, True)
    notify.parse_logs({"test-1": ["central.json"], "test-2": ["central.json"]}, log_data, False)

    summary = notify.get_summary(log_data)
    details = notify.get_details(log_data)

    assert (notify.message(["test-1", "test-2"], summary, details) ==
            """Ran 5 tests and 4 subtests
OK     : 1
PASS   : 2[test-1], 4[test-2]
CRASH  : 1[test-1]
FAIL   : 4[test-2], 5[test-1]

These tests crash:
/test3.html: [test-1]

These tests have a worse result after import (e.g. they used to PASS and now FAIL)
/test1.html
    Subtest 2: FAIL[test-1]
/test2.html: FAIL

These new tests don't PASS on all platforms
/test1.html
    Subtest 4: FAIL
/test4.html: FAIL[test-1]
""")
