from mock import Mock, patch

from sync import tc, trypush


def test_try_message_no_tests():
    assert trypush.TrySyntaxCommit.try_message() == (
        "try: -b do -p win32,win64,linux64,linux -u web-platform-tests"
        "[linux64-stylo,Ubuntu,10.10,Windows 10] -t none "
        "--artifact")


def test_try_message_no_tests_rebuild():
    assert trypush.TrySyntaxCommit.try_message(rebuild=10) == (
        "try: -b do -p win32,win64,linux64,linux -u web-platform-tests"
        "[linux64-stylo,Ubuntu,10.10,Windows 10] -t none "
        "--artifact --rebuild 10")


def test_try_fuzzy():
    push = trypush.TryFuzzyCommit(None, None, [], 0, include=["web-platform-tests"],
                                  exclude=["pgo", "ccov"])
    assert push.query == "web-platform-tests !pgo !ccov"


def test_try_task_states(mock_tasks, try_push):
    tc.get_wpt_tasks = Mock(return_value=mock_tasks(
        completed=["foo", "bar"] * 5, failed=["foo", "foo", "bar", "baz"]
    ))
    states = try_push.wpt_states()
    assert not try_push.success()
    assert states.keys() == ["baz", "foo", "bar"]
    assert states["foo"]["states"][tc.SUCCESS] == 5
    assert states["foo"]["states"][tc.FAIL] == 2
    assert states["bar"]["states"][tc.SUCCESS] == 5
    assert states["bar"]["states"][tc.FAIL] == 1
    assert states["baz"]["states"][tc.FAIL] == 1
    retriggered_states = try_push.retriggered_wpt_states()
    assert try_push.success_rate() == float(10) / len(tc.get_wpt_tasks())
    # baz is not retriggered, only occurs once
    assert retriggered_states.keys() == ["foo", "bar"]


def test_try_task_states_all_success(mock_tasks, try_push):
    tc.get_wpt_tasks = Mock(return_value=mock_tasks(completed=["foo", "bar"] * 5))
    assert try_push.success()
    assert try_push.success_rate() == 1.0


def test_retrigger_failures(mock_tasks, try_push):
    failed = ["foo", "foo", "bar", "baz"]
    ex = ["bar", "boo", "bork"]
    tc.get_wpt_tasks = Mock(return_value=mock_tasks(
        completed=["foo", "bar"] * 5, failed=failed, exception=ex
    ))
    retrigger_count = 5
    with patch('sync.trypush.auth_tc.retrigger', return_value=["job"] * retrigger_count):
        jobs = try_push.retrigger_failures(count=retrigger_count)
    assert jobs == retrigger_count * len(set(failed + ex))


def test_download_logs_excluded(mock_tasks, try_push):
    failed = ["foo", "foo", "bar", "baz"]
    ex = ["bar", "boo"]
    tc.get_wpt_tasks = Mock(return_value=mock_tasks(
        completed=["foo", "bar", "woo"] * 5, failed=failed, exception=ex
    ))
    with patch('sync.trypush.tc.download_logs'):
        tasks = try_push.download_logs(exclude=["foo"])
        tasks = [t["task"]["metadata"]["name"] for t in tasks]
        assert tasks.count("foo") == 5
        assert tasks.count("bar") == 7
        assert tasks.count("woo") == 5
        assert tasks.count("boo") == 1
        assert tasks.count("baz") == 1
        assert len(tasks) == 19
        assert tasks.count("foo") == 5
