import base64
import re
from unittest.mock import Mock, patch

from sync import tc, trypush
from sync.lock import SyncLock


def test_read_try_rev(env, git_gecko):
    try_commit = trypush.TryFuzzyCommit(git_gecko, git_gecko, None, 0, hacks=False)
    job_id = env.lando.try_push(["patch"], "0" * 40)

    assert try_commit.read_try_rev(job_id) == "%040x" % job_id


def test_try_push_patches(env, try_push):
    assert len(env.lando.try_pushes) == 1
    lando_push = env.lando.try_pushes[0]

    assert lando_push["repo_name"] == "try"
    assert lando_push["patch_format"] == "git-format-patch"
    assert lando_push["base_commit_vcs"] == "hg"
    assert re.match("^[0-9a-f]{40}$", lando_push["base_commit"])

    patches = [base64.b64decode(item).decode("utf8") for item in lando_push["patches"]]
    assert patches
    # The try commit contains the try_task_config.json written by mach try
    assert "try_task_config.json" in patches[-1]
    assert "test-linux2404-64/opt-web-platform-tests-1" in patches[-1]


def test_try_push_for_task(git_gecko, try_push):
    task = {"payload": {"env": {"WPTSYNC_TRY_PUSH_TOKEN": try_push.token}}}

    assert trypush.TryPush.for_task(git_gecko, task) == try_push


def test_try_push_for_task_selects_exact_push(git_gecko, git_wpt, try_push, MockTryCls):
    sync = try_push.sync(git_gecko, git_wpt)
    assert sync is not None
    with SyncLock.for_process(sync.process_name) as lock:
        with sync.as_mut(lock):
            second_try_push = trypush.TryPush.create(
                lock, sync, hacks=False, try_cls=MockTryCls, check_open=False
            )

    task = {"payload": {"env": {"WPTSYNC_TRY_PUSH_TOKEN": second_try_push.token}}}

    assert second_try_push.token != try_push.token
    assert trypush.TryPush.for_task(git_gecko, task) == second_try_push


def test_try_task_states(mock_tasks, try_push):
    tasks = Mock(
        return_value=mock_tasks(completed=["foo", "bar"] * 5, failed=["foo", "foo", "bar", "baz"])
    )
    with SyncLock.for_process(try_push.process_name) as lock:
        with try_push.as_mut(lock):
            with patch.object(tc.TaskGroup, "tasks", property(tasks)):
                tasks = try_push.tasks()
                states = tasks.wpt_states()
                assert not tasks.success()
                assert set(states.keys()) == {"baz", "foo", "bar"}
                assert states["foo"]["states"][tc.SUCCESS] == 5
                assert states["foo"]["states"][tc.FAIL] == 2
                assert states["bar"]["states"][tc.SUCCESS] == 5
                assert states["bar"]["states"][tc.FAIL] == 1
                assert states["baz"]["states"][tc.FAIL] == 1
                retriggered_states = tasks.retriggered_wpt_states()
                assert tasks.success_rate() == float(10) / len(tasks)
                # baz is not retriggered, only occurs once
                assert list(retriggered_states.keys()) == ["foo", "bar"]


def test_try_task_states_all_success(mock_tasks, try_push):
    tasks = Mock(return_value=mock_tasks(completed=["foo", "bar"] * 5))
    with SyncLock.for_process(try_push.process_name) as lock:
        with try_push.as_mut(lock):
            with patch.object(tc.TaskGroup, "tasks", property(tasks)):
                tasks = try_push.tasks()
                assert tasks.success()
                assert tasks.success_rate() == 1.0


def test_retrigger_failures(mock_tasks, try_push):
    failed = ["foo", "foo", "bar", "baz", "foo-aarch64"]
    ex = ["bar", "boo", "bork"]
    tasks = Mock(return_value=mock_tasks(completed=["foo", "bar"] * 5, failed=failed, exception=ex))
    retrigger_count = 5
    with SyncLock.for_process(try_push.process_name) as lock:
        with try_push.as_mut(lock):
            with patch.object(tc.TaskGroup, "tasks", property(tasks)):
                with patch(
                    "sync.trypush.auth_tc.retrigger", return_value=["job"] * retrigger_count
                ):
                    tasks = try_push.tasks()
                    jobs = tasks.retrigger_failures(count=retrigger_count)
    assert jobs == retrigger_count * (len(set(failed + ex)) - 1)


def test_download_logs(mock_tasks, try_push):
    failed = ["foo", "foo", "bar", "baz"]
    ex = ["bar", "boo"]
    tasks = Mock(
        return_value=mock_tasks(completed=["foo", "bar", "woo"] * 5, failed=failed, exception=ex)
    )
    with SyncLock.for_process(try_push.process_name) as lock:
        with try_push.as_mut(lock):
            try_push.try_rev = "1" * 40
            with patch.object(tc.TaskGroup, "tasks", property(tasks)):
                with patch.object(tc.TaskGroupView, "download_logs", Mock()):
                    tasks = try_push.tasks()
                    download_tasks = try_push.download_logs(tasks)
                    task_names = [t["task"]["metadata"]["name"] for t in download_tasks]
                    assert task_names.count("foo") == 7
                    assert task_names.count("bar") == 7
                    assert task_names.count("woo") == 5
                    assert task_names.count("boo") == 1
                    assert task_names.count("baz") == 1
                    assert len(task_names) == 21
