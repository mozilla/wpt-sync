from mock import Mock, patch
from datetime import datetime

import taskcluster

from sync import downstream, handlers, load, tc, trypush
from sync.lock import SyncLock


def test_new_wpt_pr(env, git_gecko, git_wpt, pull_request, mock_mach, mock_wpt):
    pr = pull_request([("Test commit", {"README": "Example change\n"})],
                      "Test PR")

    mock_mach.set_data("file-info", """Testing :: web-platform-tests
  testing/web-platform/tests/README
""")

    mock_wpt.set_data("files-changed", "README\n")

    downstream.new_wpt_pr(git_gecko, git_wpt, pr)
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


def test_wpt_pr_status_success(git_gecko, git_wpt, pull_request, set_pr_status,
                               hg_gecko_try, mock_wpt, mock_mach):
    with patch("sync.trypush.Mach", mock_mach):
        mock_wpt.set_data("tests-affected", "")

        pr = pull_request([("Test commit", {"README": "Example change\n"})],
                          "Test PR")
        downstream.new_wpt_pr(git_gecko, git_wpt, pr)
        with patch("sync.tree.is_open", Mock(return_value=True)):
            sync = set_pr_status(pr, "success")
        try_push = sync.latest_try_push
        assert sync.last_pr_check == {"state": "success", "sha": pr.head}
        assert try_push is not None
        assert try_push.status == "open"
        assert try_push.stability is False


def test_downstream_move(git_gecko, git_wpt, pull_request, set_pr_status,
                         hg_gecko_try, local_gecko_commit,
                         sample_gecko_metadata, initial_wpt_content, mock_mach):
    local_gecko_commit(message="Add wpt metadata", meta_changes=sample_gecko_metadata)
    pr = pull_request([("Test commit",
                        {"example/test.html": None,
                         "example/test1.html": initial_wpt_content["example/test.html"]})],
                      "Test PR")
    with patch("sync.tree.is_open", Mock(return_value=True)), patch("sync.trypush.Mach", mock_mach):
        downstream.new_wpt_pr(git_gecko, git_wpt, pr)
        sync = set_pr_status(pr, "success")
    assert sync.gecko_commits[-1].metadata["wpt-type"] == "metadata"


def test_wpt_pr_approved(git_gecko, git_wpt, pull_request, set_pr_status,
                         hg_gecko_try, mock_wpt, mock_tasks, mock_mach):
    mock_wpt.set_data("tests-affected", "")

    pr = pull_request([("Test commit", {"README": "Example change\n"})],
                      "Test PR")
    pr._approved = False
    with patch("sync.tree.is_open", Mock(return_value=True)), patch("sync.trypush.Mach", mock_mach):
        downstream.new_wpt_pr(git_gecko, git_wpt, pr)
        sync = set_pr_status(pr, "success")

        with SyncLock.for_process(sync.process_name) as lock:
            with sync.as_mut(lock):
                sync.data["affected-tests"] = {"testharness": ["example"]}

            try_push = sync.latest_try_push
            with try_push.as_mut(lock):
                try_push.taskgroup_id = "abcdef"

            assert sync.last_pr_check == {"state": "success", "sha": pr.head}
            try_push.success = lambda: True

            tasks = Mock(return_value=mock_tasks(completed=["foo", "bar"] * 5))
            with patch.object(tc.TaskGroup, 'tasks', property(tasks)):
                with sync.as_mut(lock), try_push.as_mut(lock):
                    downstream.try_push_complete(git_gecko, git_wpt, try_push, sync)
            assert try_push.status == "complete"
            assert sync.latest_try_push == try_push

        pr._approved = True
        handlers.handle_pull_request_review(git_gecko, git_wpt,
                                            {"action": "submitted",
                                             "review": {"state": "approved"},
                                             "pull_request": {"number": pr.number}})
        assert sync.latest_try_push != try_push
        assert sync.latest_try_push.stability


def test_revert_pr(env, git_gecko, git_wpt, git_wpt_upstream, pull_request, pull_request_fn,
                   set_pr_status, wpt_worktree, mock_mach):
    pr = pull_request([("Test commit", {"README": "Example change\n"})],
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
    pr = pull_request([("Test commit", {"README": "Example change\n"})],
                      "Test PR")
    downstream.new_wpt_pr(git_gecko, git_wpt, pr)
    with patch("sync.tree.is_open", Mock(return_value=True)), patch("sync.trypush.Mach", mock_mach):
        sync = set_pr_status(pr, "success")

        with SyncLock.for_process(sync.process_name) as lock:
            with sync.as_mut(lock):
                assert sync.next_try_push() is None
                assert sync.metadata_ready is False

                # No affected tests and one try push, means we should be ready
                sync.data["affected-tests"] = {}

                assert sync.requires_try
                assert not sync.requires_stability_try

                try_push = trypush.TryPush.create(lock, sync, try_cls=MockTryCls)
                with try_push.as_mut(lock):
                    try_push.status = "complete"

                assert try_push.wpt_head == sync.wpt_commits.head.sha1

                assert sync.metadata_ready
                assert sync.next_try_push() is None

                sync.data["affected-tests"] = {"testharness": ["example"]}

                assert sync.requires_stability_try
                assert not sync.metadata_ready

                new_try_push = sync.next_try_push(try_cls=MockTryCls)
                assert new_try_push is not None
                assert new_try_push.stability
                assert not sync.metadata_ready

                with new_try_push.as_mut(lock):
                    new_try_push.status = "complete"
                assert sync.metadata_ready
                assert not sync.next_try_push()

                pull_request_commit(pr.number, [("Second test commit",
                                                 {"README": "Another change\n"})])
                git_wpt.remotes.origin.fetch()

                sync.update_commits()
                assert sync.latest_try_push is not None
                assert sync.latest_valid_try_push is None
                assert not sync.metadata_ready

                updated_try_push = sync.next_try_push(try_cls=MockTryCls)
                assert not updated_try_push.stability


def test_next_try_push_infra_fail(git_gecko, git_wpt, pull_request,
                                  set_pr_status, MockTryCls, hg_gecko_try,
                                  mock_mach):
    pr = pull_request([("Test commit", {"README": "Example change\n"})],
                      "Test PR")
    downstream.new_wpt_pr(git_gecko, git_wpt, pr)
    with patch("sync.tree.is_open", Mock(return_value=True)), patch("sync.trypush.Mach", mock_mach):
        sync = set_pr_status(pr, "success")
    with SyncLock.for_process(sync.process_name) as lock:
        with sync.as_mut(lock):
            assert len(sync.try_pushes()) == 1

            # no stability try push needed
            sync.data["affected-tests"] = {}

            try_push = sync.latest_valid_try_push
            with try_push.as_mut(lock):
                try_push.status = "complete"
                try_push.infra_fail = True

            for i in range(4):
                another_try_push = sync.next_try_push(try_cls=MockTryCls)
                assert not sync.metadata_ready
                assert another_try_push is not None
                with another_try_push.as_mut(lock):
                    another_try_push.infra_fail = True
                    another_try_push.status = "complete"

            assert len(sync.latest_busted_try_pushes()) == 5

            with another_try_push.as_mut(lock):
                another_try_push.infra_fail = False
                # Now most recent try push isn't busted, so count goes back to 0
                assert len(sync.latest_busted_try_pushes()) == 0
                # Reset back to 5
                another_try_push.infra_fail = True
            # After sixth consecutive infra_failure, we should get sync error
            another_try_push = sync.next_try_push(try_cls=MockTryCls)
            with another_try_push.as_mut(lock):
                another_try_push.infra_fail = True
                another_try_push.status = "complete"
                another_try_push = sync.next_try_push(try_cls=MockTryCls)
                assert another_try_push is None
                assert sync.next_action == downstream.DownstreamAction.manual_fix


def test_try_push_expiration(git_gecko, git_wpt, pull_request,
                             set_pr_status, MockTryCls, hg_gecko_try,
                             mock_mach):
    pr = pull_request([("Test commit", {"README": "Example change\n"})],
                      "Test PR")
    today = datetime.today().date()
    with patch("sync.tree.is_open", Mock(return_value=True)), patch("sync.trypush.Mach", mock_mach):
        downstream.new_wpt_pr(git_gecko, git_wpt, pr)
        sync = set_pr_status(pr, "success")
    with SyncLock.for_process(sync.process_name) as lock:
        try_push = sync.latest_valid_try_push
        created_date = datetime.strptime(try_push.created, tc._DATE_FMT)
        assert today == created_date.date()
        assert not try_push.expired()
        with try_push.as_mut(lock):
            try_push.created = taskcluster.fromNowJSON("-15 days")
            assert try_push.expired()
            try_push.created = None
            with patch("sync.trypush.tc.get_task",
                       return_value={"created": taskcluster.fromNowJSON("-5 days")}):
                assert not try_push.expired()


def test_dependent_commit(env, git_gecko, git_wpt, pull_request, upstream_wpt_commit,
                          pull_request_commit):
    upstream_wpt_commit("First change", {"README": "Example change\n"})

    pr = pull_request([("Test change", {"README": "Example change 1\n"})],
                      "Test PR")

    downstream.new_wpt_pr(git_gecko, git_wpt, pr)
    sync = load.get_pr_sync(git_gecko, git_wpt, pr["number"])

    with SyncLock.for_process(sync.process_name) as lock:
        with sync.as_mut(lock):
            assert len(sync.gecko_commits) == 2
            assert sync.gecko_commits[0].msg.splitlines()[0] == "First change"
            assert sync.gecko_commits[0].metadata["wpt-type"] == "dependency"
            assert sync.gecko_commits[1].metadata.get("wpt-type") is None
            assert "Test change" in sync.gecko_commits[1].msg.splitlines()[0]
            old_gecko_commits = sync.gecko_commits[:]
            # Check that rerunning doesn't affect anything
            sync.update_commits()
            assert ([item.sha1 for item in sync.gecko_commits] ==
                    [item.sha1 for item in old_gecko_commits])

            head_sha = pull_request_commit(pr.number,
                                           [("fixup! Test change",
                                             {"README": "Example change 2\n"})])
            downstream.update_repositories(git_gecko, git_wpt)
            sync.update_commits()
            assert len(sync.gecko_commits) == 3
            assert ([item.sha1 for item in sync.gecko_commits[:2]] ==
                    [item.sha1 for item in old_gecko_commits])
            assert sync.gecko_commits[-1].metadata["wpt-commit"] == head_sha


def test_metadata_update(env, git_gecko, git_wpt, pull_request, pull_request_commit):
    from conftest import gecko_changes, git_commit
    pr = pull_request([("Test commit", {"README": "Example change\n"})],
                      "Test PR")

    downstream.new_wpt_pr(git_gecko, git_wpt, pr)
    sync = load.get_pr_sync(git_gecko, git_wpt, pr["number"])

    assert len(sync.gecko_commits) == 1

    with SyncLock.for_process(sync.process_name) as lock:
        with sync.as_mut(lock):
            gecko_work = sync.gecko_worktree.get()
            changes = gecko_changes(env, meta_changes={"example.ini": "Example change"})
            git_commit(gecko_work, """Update metadata

        wpt-pr: %s
        wpt-type: metadata
        """ % pr.number, changes)

            assert len(sync.gecko_commits) == 2
            assert sync.gecko_commits[-1].metadata.get("wpt-type") == "metadata"
            metadata_commit = sync.gecko_commits[-1]

            head_sha = pull_request_commit(pr.number,
                                           [("fixup! Test commit",
                                             {"README": "Example change 1\n"})])

            downstream.update_repositories(git_gecko, git_wpt)
            sync.update_commits()
            assert len(sync.gecko_commits) == 3
            assert sync.gecko_commits[-1].metadata.get("wpt-type") == "metadata"
            assert sync.gecko_commits[-1].msg == metadata_commit.msg
            assert sync.gecko_commits[-2].metadata.get("wpt-commit") == head_sha


def test_message_filter():
    sync = Mock()
    sync.configure_mock(bug=1234, pr=7)
    msg, _ = downstream.DownstreamSync.message_filter.__func__(
        sync,
        """Upstream summary

Upstream message

Cq-Include-Trybots: luci.chromium.try:android_optional_gpu_tests_rel;"""
        "luci.chromium.try:mac_optional_gpu_tests_rel;"
        "master.tryserver.chromium.linux:linux_mojo;"
        "master.tryserver.chromium.mac:ios-simulator-cronet;"
        "master.tryserver.chromium.mac:ios-simulator-full-configs")

    assert msg == (u"""Bug 1234 [wpt PR 7] - Upstream summary, a=testonly

Upstream message

Cq-Include-Trybots: luci.chromium.try\u200B:android_optional_gpu_tests_rel;"""
                   u"luci.chromium.try\u200B:mac_optional_gpu_tests_rel;"
                   u"master.tryserver.chromium.linux:linux_mojo;"
                   u"master.tryserver.chromium.mac:ios-simulator-cronet;"
                   u"master.tryserver.chromium.mac:ios-simulator-full-configs")


def test_github_label_on_error(env, git_gecko, git_wpt, pull_request):
    pr = pull_request([("Testing", {"README": "Example change\n"})],
                      "Test PR")

    downstream.new_wpt_pr(git_gecko, git_wpt, pr)
    sync = load.get_pr_sync(git_gecko, git_wpt, pr["number"])
    with SyncLock.for_process(sync.process_name) as lock:
        with sync.as_mut(lock):
            sync.error = "Infrastructure Failed"

    assert env.gh_wpt.get_pull(pr["number"])['labels'] == ['mozilla:gecko-blocked']

    with SyncLock.for_process(sync.process_name) as lock:
        with sync.as_mut(lock):
            sync.update_commits()

    assert env.gh_wpt.get_pull(pr["number"])['labels'] == []

