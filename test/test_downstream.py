from unittest.mock import Mock, patch

from sync import downstream, handlers, load, trypush
from sync.base import ProcessName
from sync.lock import SyncLock


def test_new_wpt_pr(env, git_gecko, git_wpt, pull_request, mock_mach, mock_wpt):
    pr = pull_request([(b"Test commit", {"README": b"Example change\n"})],
                      "Test PR")

    mock_mach.set_data("file-info", b"""Testing :: web-platform-tests
  testing/web-platform/tests/README
""")

    mock_wpt.set_data("files-changed", b"README\n")

    initial_data_commits = list(git_gecko.iter_commits(env.config["sync"]["ref"]))

    downstream.new_wpt_pr(git_gecko, git_wpt, pr)

    # We currently create the following commits:
    #   1. Initial sync creation
    #   2. Adding a bug
    #   3. Adding the GH checks status
    # This could be reduced to exactly one commit in the future
    data_commits = list(git_gecko.iter_commits(env.config["sync"]["ref"]))
    assert len(data_commits) == len(initial_data_commits) + 3

    sync = load.get_pr_sync(git_gecko, git_wpt, pr["number"])
    env.gh_wpt.set_status(pr["number"], "success", "http://test/", "description",
                          "continuous-integration/travis-ci/pr")
    assert sync is not None
    assert sync.status == "open"
    assert len(sync.gecko_commits) == 1
    assert len(sync.wpt_commits) == 1

    assert sync.gecko_commits[0].metadata == {
        "wpt-pr": str(pr["number"]),
        "wpt-commit": pr["head"]
    }
    assert sync.data["check"] == {"id": 0, "sha1": pr["head"]}
    assert len(env.gh_wpt.checks) == 1
    assert len(env.gh_wpt.checks[pr["head"]])
    assert "Creating a bug in component Testing :: web-platform" in env.bz.output.getvalue()


def test_new_pr_existing_branch(env, git_gecko, git_wpt, pull_request, mock_mach, mock_wpt):
    pr = pull_request([(b"Test commit", {"README": b"Example change\n"})],
                      "Test PR")

    mock_mach.set_data("file-info", b"""Testing :: web-platform-tests
  testing/web-platform/tests/README
""")

    mock_wpt.set_data("files-changed", b"README\n")

    initial_data_commits = list(git_gecko.iter_commits(env.config["sync"]["ref"]))
    process_name = ProcessName("sync", "downstream", pr["number"], "0")

    git_wpt.remotes.origin.fetch()

    # Pre-create some branches that will clash with the sync branches
    git_gecko.create_head(process_name.path(), env.config["gecko"]["refs"]["central"])
    git_wpt.create_head(process_name.path(), "origin/master")

    downstream.new_wpt_pr(git_gecko, git_wpt, pr)
    data_commits = list(git_gecko.iter_commits(env.config["sync"]["ref"]))
    assert len(data_commits) == len(initial_data_commits) + 3

    sync = load.get_pr_sync(git_gecko, git_wpt, pr["number"])

    assert sync is not None
    assert sync.process_name == process_name


def test_downstream_move(git_gecko, git_wpt, pull_request, set_pr_status,
                         hg_gecko_try, local_gecko_commit,
                         sample_gecko_metadata, initial_wpt_content, mock_mach):
    local_gecko_commit(message=b"Add wpt metadata", meta_changes=sample_gecko_metadata)
    pr = pull_request([(b"Test commit",
                        {"example/test.html": None,
                         "example/test1.html": initial_wpt_content["example/test.html"]})],
                      "Test PR")
    with patch("sync.tree.is_open", Mock(return_value=True)), patch("sync.trypush.Mach", mock_mach):
        downstream.new_wpt_pr(git_gecko, git_wpt, pr)
        sync = set_pr_status(pr.number, "success")
    assert sync.gecko_commits[-1].metadata["wpt-type"] == "metadata"


def test_wpt_pr_approved(env, git_gecko, git_wpt, pull_request, set_pr_status,
                         hg_gecko_try, mock_wpt, mock_tasks, mock_mach):
    mock_wpt.set_data("tests-affected", "")

    pr = pull_request([(b"Test commit", {"README": b"Example change\n"})],
                      "Test PR")
    pr._approved = False
    with patch("sync.tree.is_open", Mock(return_value=True)), patch("sync.trypush.Mach", mock_mach):
        downstream.new_wpt_pr(git_gecko, git_wpt, pr)
        sync = set_pr_status(pr.number, "success")

        with SyncLock.for_process(sync.process_name) as lock:
            with sync.as_mut(lock):
                sync.data["affected-tests"] = {"testharness": ["example"]}

            assert sync.latest_try_push is None

        pr._approved = True
        # A Try push is not run after approval.
        assert sync.latest_try_push is None

        # If we 'merge' the PR, then we will see a stability try push
        with patch.object(trypush.TryCommit, 'read_treeherder', autospec=True) as mock_read:
            mock_read.return_value = "0000000000000000"
            handlers.handle_pr(git_gecko, git_wpt,
                               {"action": "closed",
                                "number": pr.number,
                                "pull_request": {
                                    "number": pr.number,
                                    "merge_commit_sha": "a" * 25,
                                    "base": {"sha": "b" * 25},
                                    "merged": True,
                                    "state": "closed",
                                    "merged_by": {"login": "test_user"}}})
        try_push = sync.latest_try_push
        assert try_push.stability


def test_revert_pr(env, git_gecko, git_wpt, git_wpt_upstream, pull_request, pull_request_fn,
                   set_pr_status, wpt_worktree, mock_mach):
    pr = pull_request([(b"Test commit", {"README": b"Example change\n"})],
                      "Test PR")

    downstream.new_wpt_pr(git_gecko, git_wpt, pr)
    sync = load.get_pr_sync(git_gecko, git_wpt, pr["number"])

    with SyncLock.for_process(sync.process_name) as lock:
        with sync.as_mut(lock):
            commit = sync.wpt_commits[0]
            sync.wpt_commits.base = sync.data["wpt-base"] = git_wpt_upstream.head.commit.hexsha
            git_wpt_upstream.git.merge(commit.sha1)

    def revert_fn():
        git_wpt.remotes["origin"].fetch()
        wpt_work = wpt_worktree()
        wpt_work.git.revert(commit.sha1, no_edit=True)
        wpt_work.git.push("origin", "HEAD:refs/heads/revert")
        git_wpt_upstream.commit("revert")
        return "revert"

    pr_revert = pull_request_fn(revert_fn, title="Revert Test PR")

    downstream.new_wpt_pr(git_gecko, git_wpt, pr_revert)
    sync_revert = load.get_pr_sync(git_gecko, git_wpt, pr_revert["number"])

    # Refresh the instance data
    sync.data._load()
    assert sync.skip
    assert sync_revert.skip


def test_next_try_push(git_gecko, git_wpt, pull_request, set_pr_status, MockTryCls,
                       hg_gecko_try, pull_request_commit, mock_mach):
    pr = pull_request([(b"Test commit", {"README": b"Example change\n"})],
                      "Test PR")
    downstream.new_wpt_pr(git_gecko, git_wpt, pr)
    with patch("sync.tree.is_open", Mock(return_value=True)), patch("sync.trypush.Mach", mock_mach):
        sync = set_pr_status(pr.number, "success")

        with SyncLock.for_process(sync.process_name) as lock:
            with sync.as_mut(lock):
                assert sync.next_try_push() is None
                assert sync.metadata_ready is False

                # No affected tests and one try push, means we should be ready
                sync.data["affected-tests"] = {}

                assert sync.requires_try
                assert not sync.requires_stability_try

                sync.data["affected-tests"] = {"testharness": ["example"]}

                assert sync.requires_stability_try
                assert not sync.metadata_ready

                # The PR has not yet been merged, so no Try push should happen
                assert sync.next_try_push() is None

                pr.merged = True

                new_try_push = sync.next_try_push(try_cls=MockTryCls)
                assert new_try_push is not None
                assert new_try_push.stability
                assert not sync.metadata_ready

                with new_try_push.as_mut(lock):
                    new_try_push.status = "complete"
                assert sync.metadata_ready
                assert not sync.next_try_push()


def test_next_try_push_infra_fail(env, git_gecko, git_wpt, pull_request,
                                  set_pr_status, MockTryCls, hg_gecko_try,
                                  mock_mach, mock_taskgroup):
    taskgroup = mock_taskgroup("taskgroup-complete-build-failed.json")
    try_tasks = trypush.TryPushTasks(taskgroup)

    pr = pull_request([(b"Test commit", {"README": b"Example change\n"})],
                      "Test PR")
    downstream.new_wpt_pr(git_gecko, git_wpt, pr)

    try_patch = patch("sync.trypush.TryPush.tasks", Mock(return_value=try_tasks))
    tree_open_patch = patch("sync.tree.is_open", Mock(return_value=True))
    taskgroup_patch = patch("sync.tc.TaskGroup", Mock(return_value=taskgroup))
    mach_patch = patch("sync.trypush.Mach", mock_mach)

    with tree_open_patch, try_patch, taskgroup_patch, mach_patch:
        sync = set_pr_status(pr.number, "success")
        env.gh_wpt.get_pull(sync.pr).merged = True

        with SyncLock.for_process(sync.process_name) as lock:
            with sync.as_mut(lock):
                assert len(sync.try_pushes()) == 0

                sync.data["affected-tests"] = {"testharness": ["example"]}

                try_push = sync.next_try_push(try_cls=MockTryCls)
                with try_push.as_mut(lock):
                    try_push["taskgroup-id"] = taskgroup.taskgroup_id
                    try_push.status = "complete"
                    try_push.infra_fail = True

                # This try push still has completed builds and tests, so we say metadata is ready.
                assert sync.next_action == downstream.DownstreamAction.ready
                assert sync.next_try_push(try_cls=MockTryCls) is None

                # There should be a comment to flag failed builds
                msg = "There were infrastructure failures for the Try push (%s):\nbuild-win32/opt\nbuild-win32/debug\nbuild-win64/opt\nbuild-win64/debug\n" % try_push.treeherder_url  # noqa: E501
                assert msg in env.bz.output.getvalue()

                # Replace the taskgroup with one where there were no completed tests
                taskgroup = mock_taskgroup("taskgroup-no-tests-build-failed.json")
                try_tasks = trypush.TryPushTasks(taskgroup)

                # The next action should flag for manual fix now
                assert sync.next_action == downstream.DownstreamAction.manual_fix


def test_next_try_push_infra_fail_try_rebase(env, git_gecko, git_wpt, pull_request,
                                             set_pr_status, MockTryCls, hg_gecko_try,
                                             mock_mach, mock_taskgroup):
    taskgroup = mock_taskgroup("taskgroup-complete-build-failed.json")
    try_tasks = trypush.TryPushTasks(taskgroup)

    pr = pull_request([(b"Test commit", {"README": b"Example change\n"})],
                      "Test PR")
    downstream.new_wpt_pr(git_gecko, git_wpt, pr)

    try_patch = patch("sync.trypush.TryPush.tasks", Mock(return_value=try_tasks))
    tree_open_patch = patch("sync.tree.is_open", Mock(return_value=True))
    taskgroup_patch = patch("sync.tc.TaskGroup", Mock(return_value=taskgroup))
    mach_patch = patch("sync.trypush.Mach", mock_mach)

    with tree_open_patch, try_patch, taskgroup_patch, mach_patch:
        sync = set_pr_status(pr.number, "success")
        env.gh_wpt.get_pull(sync.pr).merged = True

        with SyncLock.for_process(sync.process_name) as lock:
            with sync.as_mut(lock):
                assert len(sync.try_pushes()) == 0

                sync.data["affected-tests"] = {"testharness": ["example"]}

                try_push = sync.next_try_push(try_cls=MockTryCls)
                with try_push.as_mut(lock):
                    try_push["taskgroup-id"] = None
                    try_push.status = "complete"
                    try_push.infra_fail = True

                assert sync.next_action == downstream.DownstreamAction.ready


def test_dependent_commit(env, git_gecko, git_wpt, pull_request, upstream_wpt_commit,
                          pull_request_commit):
    upstream_wpt_commit(b"First change", {"README": b"Example change\n"})

    pr = pull_request([(b"Test change", {"README": b"Example change 1\n"})],
                      "Test PR")

    downstream.new_wpt_pr(git_gecko, git_wpt, pr)
    sync = load.get_pr_sync(git_gecko, git_wpt, pr["number"])

    with SyncLock.for_process(sync.process_name) as lock:
        with sync.as_mut(lock):
            assert len(sync.gecko_commits) == 2
            assert sync.gecko_commits[0].msg.splitlines()[0] == b"First change"
            assert sync.gecko_commits[0].metadata["wpt-type"] == "dependency"
            assert sync.gecko_commits[1].metadata.get("wpt-type") is None
            assert b"Test change" in sync.gecko_commits[1].msg.splitlines()[0]
            old_gecko_commits = sync.gecko_commits[:]
            # Check that rerunning doesn't affect anything
            sync.update_commits()
            assert ([item.sha1 for item in sync.gecko_commits] ==
                    [item.sha1 for item in old_gecko_commits])

            head_sha = pull_request_commit(pr.number,
                                           [(b"fixup! Test change",
                                             {"README": b"Example change 2\n"})])
            downstream.update_repositories(git_gecko, git_wpt)
            sync.update_commits()
            assert len(sync.gecko_commits) == 3
            assert ([item.sha1 for item in sync.gecko_commits[:2]] ==
                    [item.sha1 for item in old_gecko_commits])
            assert sync.gecko_commits[-1].metadata["wpt-commit"] == head_sha


def test_metadata_update(env, git_gecko, git_wpt, pull_request, pull_request_commit):
    from conftest import gecko_changes, git_commit
    pr = pull_request([(b"Test commit", {"README": b"Example change\n"})],
                      "Test PR")

    downstream.new_wpt_pr(git_gecko, git_wpt, pr)
    sync = load.get_pr_sync(git_gecko, git_wpt, pr["number"])

    assert len(sync.gecko_commits) == 1

    with SyncLock.for_process(sync.process_name) as lock:
        with sync.as_mut(lock):
            gecko_work = sync.gecko_worktree.get()
            changes = gecko_changes(env, meta_changes={"example.ini": b"Example change"})
            git_commit(gecko_work, b"""Update metadata

        wpt-pr: %s
        wpt-type: metadata
        """ % str(pr.number).encode("utf8"), changes)

            assert len(sync.gecko_commits) == 2
            assert sync.gecko_commits[-1].metadata.get("wpt-type") == "metadata"
            metadata_commit = sync.gecko_commits[-1]

            head_sha = pull_request_commit(pr.number,
                                           [(b"fixup! Test commit",
                                             {"README": b"Example change 1\n"})])

            downstream.update_repositories(git_gecko, git_wpt)
            sync.update_commits()
            assert len(sync.gecko_commits) == 3
            assert sync.gecko_commits[-1].metadata.get("wpt-type") == "metadata"
            assert sync.gecko_commits[-1].msg == metadata_commit.msg
            assert sync.gecko_commits[-2].metadata.get("wpt-commit") == head_sha


def test_gecko_rebase(env, git_gecko, git_wpt, pull_request):
    pr = pull_request([(b"Test commit", {"README": b"Example change\n"})],
                      b"Test PR")

    downstream.new_wpt_pr(git_gecko, git_wpt, pr)
    sync = load.get_pr_sync(git_gecko, git_wpt, pr["number"])

    assert len(sync.gecko_commits) == 1

    with SyncLock.for_process(sync.process_name) as lock:
        with sync.as_mut(lock):
            assert sync.data["gecko-base"] == downstream.DownstreamSync.gecko_landing_branch()
            sync.gecko_rebase(downstream.DownstreamSync.gecko_integration_branch())
            assert sync.data["gecko-base"] == downstream.DownstreamSync.gecko_integration_branch()


def test_message_filter():
    sync = Mock()
    sync.configure_mock(bug=1234, pr=7)
    func = downstream.DownstreamSync.message_filter
    msg, _ = func(
        sync,
        b"""Upstream summary

Upstream message

Cq-Include-Trybots: luci.chromium.try:android_optional_gpu_tests_rel;"""
        b"luci.chromium.try:mac_optional_gpu_tests_rel;"
        b"master.tryserver.chromium.linux:linux_mojo;"
        b"master.tryserver.chromium.mac:ios-simulator-cronet;"
        b"master.tryserver.chromium.mac:ios-simulator-full-configs")

    assert msg == ("""Bug 1234 [wpt PR 7] - Upstream summary, a=testonly

SKIP_BMO_CHECK

Upstream message

Cq-Include-Trybots: luci.chromium.try\u200B:android_optional_gpu_tests_rel;"""
                   "luci.chromium.try\u200B:mac_optional_gpu_tests_rel;"
                   "master.tryserver.chromium.linux:linux_mojo;"
                   "master.tryserver.chromium.mac:ios-simulator-cronet;"
                   "master.tryserver.chromium.mac:ios-simulator-full-configs").encode()


def test_github_label_on_error(env, git_gecko, git_wpt, pull_request):
    pr = pull_request([(b"Testing", {"README": b"Example change\n"})],
                      "Test PR")

    downstream.new_wpt_pr(git_gecko, git_wpt, pr)
    sync = load.get_pr_sync(git_gecko, git_wpt, pr["number"])
    with SyncLock.for_process(sync.process_name) as lock:
        with sync.as_mut(lock):
            sync.error = "Infrastructure Failed"

    assert env.gh_wpt.get_pull(pr["number"])["labels"] == ["mozilla:gecko-blocked"]

    with SyncLock.for_process(sync.process_name) as lock:
        with sync.as_mut(lock):
            sync.update_commits()

    assert env.gh_wpt.get_pull(pr["number"])["labels"] == []
