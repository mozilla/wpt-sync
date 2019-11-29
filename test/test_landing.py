import os

from mock import Mock, patch, ANY, DEFAULT

import pytest

from sync import landing, downstream, tc, tree, trypush, upstream
from sync import commit as sync_commit
from sync.errors import AbortError
from sync.gitutils import update_repositories
from sync.lock import SyncLock
from conftest import git_commit


def test_upstream_commit(env, git_gecko, git_wpt, git_wpt_upstream, pull_request):
    pr = pull_request([("Test commit", {"README": "example_change"})])
    head_rev = pr._commits[0]["sha"]
    git_wpt_upstream.head.commit = head_rev
    git_wpt.remotes.origin.fetch()
    landing.wpt_push(git_gecko, git_wpt, [head_rev], create_missing=False)
    assert sync_commit.WptCommit(git_wpt_upstream,
                                 git_wpt_upstream.head.commit).pr() == pr["number"]


def test_land_try(env, git_gecko, git_wpt, git_wpt_upstream, pull_request, set_pr_status,
                  hg_gecko_try, mock_mach):
    pr = pull_request([("Test commit", {"README": "example_change",
                                        "LICENSE": "Some change"})])
    head_rev = pr._commits[0]["sha"]

    trypush.Mach = mock_mach
    downstream.new_wpt_pr(git_gecko, git_wpt, pr)
    sync = set_pr_status(pr, "success")

    git_wpt_upstream.head.commit = head_rev
    git_wpt.remotes.origin.fetch()
    landing.wpt_push(git_gecko, git_wpt, [head_rev], create_missing=False)

    with SyncLock.for_process(sync.process_name) as downstream_lock:
        with sync.as_mut(downstream_lock):
            sync.data["force-metadata-ready"] = True

    tree.is_open = lambda x: True
    landing_sync = landing.update_landing(git_gecko, git_wpt)

    assert landing_sync is not None
    with SyncLock("landing", None) as lock:
        with landing_sync.as_mut(lock):
            worktree = landing_sync.gecko_worktree.get()
            # Check that files we shouldn't move aren't
            assert not os.path.exists(os.path.join(worktree.working_dir,
                                                   env.config["gecko"]["path"]["wpt"],
                                                   ".git"))
            with open(os.path.join(worktree.working_dir,
                                   env.config["gecko"]["path"]["wpt"],
                                   "LICENSE")) as f:
                assert f.read() == "Initial license\n"

    try_push = sync.latest_try_push
    assert try_push is None
    mach_command = mock_mach.get_log()[-1]
    assert mach_command["command"] == "mach"
    assert mach_command["args"] == ("try",
                                    "fuzzy",
                                    "--artifact",
                                    "-q",
                                    "web-platform-tests !devedition !ccov !fis",
                                    "-q",
                                    "web-platform-tests fis !devedition !ccov !asan !aarch64 "
                                    "windows10 | linux64",
                                    "--full")


def test_land_commit(env, git_gecko, git_wpt, git_wpt_upstream, pull_request, set_pr_status,
                     hg_gecko_try, mock_mach, mock_tasks):
    pr = pull_request([("Test commit", {"README": "example_change"})])
    head_rev = pr._commits[0]["sha"]

    trypush.Mach = mock_mach

    downstream.new_wpt_pr(git_gecko, git_wpt, pr)
    downstream_sync = set_pr_status(pr, "success")

    git_wpt_upstream.head.commit = head_rev
    git_wpt.remotes.origin.fetch()
    landing.wpt_push(git_gecko, git_wpt, [head_rev], create_missing=False)

    with SyncLock.for_process(downstream_sync.process_name) as downstream_lock:
        with downstream_sync.as_mut(downstream_lock):
            downstream_sync.data["force-metadata-ready"] = True

    tree.is_open = lambda x: True
    sync = landing.update_landing(git_gecko, git_wpt)

    try_push = sync.latest_try_push
    with SyncLock.for_process(sync.process_name) as lock:
        with sync.as_mut(lock), try_push.as_mut(lock):
            try_push.taskgroup_id = "abcdef"
            with patch.object(try_push, "download_logs", Mock(return_value=[])):
                with patch.object(tc.TaskGroup, "tasks",
                                  property(Mock(return_value=mock_tasks(completed=["foo"])))):
                    landing.try_push_complete(git_gecko, git_wpt, try_push, sync)

    assert "Pushed to try (stability)" in env.bz.output.getvalue()
    assert try_push.status == "complete"
    assert sync.status == "open"

    try_push = sync.latest_try_push
    with SyncLock.for_process(sync.process_name) as lock:
        with sync.as_mut(lock), try_push.as_mut(lock):
            try_push.taskgroup_id = "abcdef2"
            with patch.object(try_push, "download_logs", Mock(return_value=[])):
                with patch.object(tc.TaskGroup, "tasks",
                                  property(Mock(return_value=mock_tasks(completed=["foo"])))):
                    landing.try_push_complete(git_gecko, git_wpt, try_push, sync)

    new_head = git_gecko.remotes.mozilla.refs["bookmarks/mozilla/autoland"].commit
    assert "Update web-platform-tests to %s" % head_rev in new_head.message
    assert new_head.tree["testing/web-platform/tests/README"].data_stream.read() == "example_change"
    sync_point = landing.load_sync_point(git_gecko, git_wpt)
    assert sync_point["upstream"] == head_rev
    # Update central to contain the landing
    git_gecko.refs["mozilla/bookmarks/mozilla/central"].commit = new_head
    with patch("sync.landing.tasks.land.apply_async") as mock_apply:
        landing.gecko_push(git_gecko, git_wpt, "central",
                           git_gecko.cinnabar.git2hg(new_head))
        assert mock_apply.call_count == 1
    assert sync.status == "complete"


def test_landable_skipped(env, git_gecko, git_wpt, git_wpt_upstream, pull_request, set_pr_status,
                          mock_mach):
    prev_wpt_head = git_wpt_upstream.head.commit
    pr = pull_request([("Test commit", {"README": "example_change"})])
    head_rev = pr._commits[0]["sha"]

    trypush.Mach = mock_mach

    downstream.new_wpt_pr(git_gecko, git_wpt, pr)
    downstream_sync = set_pr_status(pr, "success")

    with SyncLock.for_process(downstream_sync.process_name) as downstream_lock:
        with downstream_sync.as_mut(downstream_lock):
            downstream_sync.skip = True

    git_wpt_upstream.head.commit = head_rev
    git_wpt.remotes.origin.fetch()
    landing.wpt_push(git_gecko, git_wpt, [head_rev], create_missing=False)

    wpt_head, landable_commits = landing.landable_commits(git_gecko, git_wpt, prev_wpt_head.hexsha)
    assert len(landable_commits) == 1
    assert landable_commits[0][0] == str(pr.number)
    assert landable_commits[0][1] == downstream_sync


def test_try_push_exceeds_failure_threshold(git_gecko, git_wpt, landing_with_try_push, mock_tasks):
    # 2/3 failure rate, too high
    sync = landing_with_try_push
    try_push = sync.latest_try_push
    with patch.object(tc.TaskGroup, "tasks",
                      property(Mock(return_value=mock_tasks(failed=["foo", "foobar"],
                                                            completed=["bar"])))):
        with SyncLock.for_process(sync.process_name) as lock:
            with sync.as_mut(lock), try_push.as_mut(lock):
                with pytest.raises(AbortError):
                    landing.try_push_complete(git_gecko, git_wpt, try_push, sync)
    assert try_push.status == "open"
    assert "too many test failures" in sync.error["message"]


def test_try_push_retriggers_failures(git_gecko, git_wpt, landing_with_try_push, mock_tasks, env,
                                      mock_mach):
    # 20% failure rate, okay
    tasks = Mock(return_value=mock_tasks(
        failed=["foo"], completed=["bar", "baz", "boo", "faz"])
    )
    sync = landing_with_try_push
    try_push = sync.latest_try_push
    with SyncLock("landing", None) as lock:
        with sync.as_mut(lock), try_push.as_mut(lock):
            with patch.object(tc.TaskGroup, "tasks", property(tasks)):
                with patch('sync.trypush.auth_tc.retrigger',
                           return_value=["job"] * trypush.TryPushTasks._retrigger_count):
                    try_push.Mach = mock_mach
                    with try_push.as_mut(lock):
                        try_push["stability"] = True
                        try_push.taskgroup_id = "12345678"
                        landing.try_push_complete(git_gecko, git_wpt, try_push, sync)
                    assert "Retriggered failing web-platform-test tasks" in env.bz.output.getvalue()
                    assert try_push.status != "complete"


def test_download_logs_after_retriggers_complete(git_gecko, git_wpt, landing_with_try_push,
                                                 mock_tasks, env):
    # > 80% of retriggered "foo" tasks pass, so we consider the "foo" failure intermittent
    tasks = Mock(return_value=mock_tasks(
        failed=["foo", "foo", "bar"],
        completed=["bar", "bar", "bar" "baz", "boo", "foo", "foo", "foo", "foo", "foo",
                   "foobar", "foobar", "foobar"])
    )
    sync = landing_with_try_push
    try_push = sync.latest_try_push
    with SyncLock.for_process(sync.process_name) as lock:
        with try_push.as_mut(lock), sync.as_mut(lock):
            with patch.object(tc.TaskGroup,
                              "tasks",
                              property(tasks)), patch(
                                  'sync.landing.push_to_gecko'):
                try_push.download_logs = Mock(return_value=[])
                try_push["stability"] = True
                landing.try_push_complete(git_gecko, git_wpt, try_push, sync)
        try_push.download_logs.assert_called_with(ANY)
        assert try_push.status == "complete"


def test_no_download_logs_after_all_try_tasks_success(git_gecko, git_wpt, landing_with_try_push,
                                                      mock_tasks, env):
    tasks = Mock(return_value=mock_tasks(completed=["bar", "baz", "boo"]))
    sync = landing_with_try_push
    try_push = sync.latest_try_push
    with SyncLock.for_process(sync.process_name) as lock:
        with try_push.as_mut(lock), sync.as_mut(lock):
            with patch.object(tc.TaskGroup,
                              "tasks",
                              property(tasks)), patch(
                                  'sync.landing.push_to_gecko'):
                try_push.download_logs = Mock(return_value=[])
                landing.try_push_complete(git_gecko, git_wpt, try_push, sync)
                # no intermittents in the try push
                try_push.download_logs.assert_not_called()
                assert try_push.status == "complete"


def test_landing_reapply(env, git_gecko, git_wpt, git_wpt_upstream, pull_request, set_pr_status,
                         hg_gecko_upstream, upstream_gecko_commit, upstream_wpt_commit,
                         hg_gecko_try, mock_mach, mock_tasks):
    # Test that we reapply the correct commits when landing patches on upstream
    # First we need to create 3 gecko commits:
    # Two that are landed
    # One that is still a PR
    # Then we create a landing that points at the first gecko commit that is landed
    # upstream. Locally we expect the code to reapply the two other gecko commits, so
    # we should end up with no overall change.

    trypush.Mach = mock_mach

    # Add first gecko change
    test_changes = {"change1": "CHANGE1\n"}
    rev = upstream_gecko_commit(test_changes=test_changes, bug="1111",
                                message="Add change1 file")

    update_repositories(git_gecko, git_wpt, wait_gecko_commit=rev)
    pushed, _, _ = upstream.gecko_push(git_gecko, git_wpt, "autoland", rev,
                                       raise_on_error=True)
    sync_1 = pushed.pop()

    # Update central
    hg_gecko_upstream.bookmark("mozilla/central", "-r", rev)

    # Merge the upstream change
    with SyncLock.for_process(sync_1.process_name) as lock:
        with sync_1.as_mut(lock):
            remote_branch = sync_1.remote_branch
            git_wpt_upstream.git.checkout(remote_branch)
            git_wpt_upstream.git.rebase("master")
            git_wpt_upstream.git.checkout("master")
            git_wpt_upstream.git.merge(remote_branch, ff_only=True)

            sync_1.finish()

    # Add second gecko change
    test_changes = {"change2": "CHANGE2\n"}
    rev = upstream_gecko_commit(test_changes=test_changes, bug="1112",
                                message="Add change2 file")

    update_repositories(git_gecko, git_wpt, wait_gecko_commit=rev)
    pushed, _, _ = upstream.gecko_push(git_gecko, git_wpt, "autoland", rev,
                                       raise_on_error=True)
    sync_2 = pushed.pop()

    hg_gecko_upstream.bookmark("mozilla/central", "-r", rev)

    # Merge the gecko change
    remote_branch = sync_2.remote_branch
    git_wpt_upstream.git.checkout(remote_branch)
    git_wpt_upstream.git.rebase("master")
    git_wpt_upstream.git.checkout("master")
    git_wpt_upstream.git.merge(remote_branch, ff_only=True)

    # Add an upstream commit that has metadata
    pr = pull_request([("Upstream change 1", {"upstream1": "UPSTREAM1\n"})])
    head_rev = pr._commits[0]["sha"]
    downstream.new_wpt_pr(git_gecko, git_wpt, pr)
    downstream_sync = set_pr_status(pr, "success")
    git_wpt_upstream.head.commit = head_rev
    git_wpt_upstream.git.reset(hard=True)
    with SyncLock.for_process(downstream_sync.process_name) as downstream_lock:
        with downstream_sync.as_mut(downstream_lock):
            downstream_sync.data["force-metadata-ready"] = True

    # This is the commit we should land to
    landing_rev = git_wpt_upstream.git.rev_parse("HEAD")

    # Add an upstream commit that doesn't have metadata
    pr = pull_request([("Upstream change 2", {"upstream2": "UPSTREAM2\n"})])
    head_rev = pr._commits[0]["sha"]
    downstream.new_wpt_pr(git_gecko, git_wpt, pr)
    downstream_sync = set_pr_status(pr, "success")
    git_wpt_upstream.head.commit = head_rev
    git_wpt_upstream.git.reset(hard=True)

    # Add third gecko change
    test_changes = {"change3": "CHANGE3\n"}
    rev = upstream_gecko_commit(test_changes=test_changes, bug="1113",
                                message="Add change3 file")

    update_repositories(git_gecko, git_wpt, wait_gecko_commit=rev)
    pushed, _, _ = upstream.gecko_push(git_gecko, git_wpt, "autoland", rev,
                                       raise_on_error=True)

    # Now start a landing
    tree.is_open = lambda x: True
    sync = landing.update_landing(git_gecko, git_wpt)

    assert sync is not None

    for i in xrange(2):
        with SyncLock.for_process(sync.process_name) as lock:
            try_push = sync.latest_try_push
            with sync.as_mut(lock), try_push.as_mut(lock):
                try_push.taskgroup_id = "abcde" + str(i)
                try_push.download_logs = Mock(return_value=[])
                with patch.object(tc.TaskGroup, "tasks",
                                  property(Mock(return_value=mock_tasks(completed=["foo"])))):
                    landing.try_push_complete(git_gecko, git_wpt, try_push, sync)

    hg_gecko_upstream.update()
    gecko_root = hg_gecko_upstream.root().strip()
    assert (hg_gecko_upstream
            .log("-l1", "--template={desc|firstline}")
            .strip()
            .endswith("[wpt-sync] Update web-platform-tests to %s, a=testonly" % landing_rev))
    for file in ["change1", "change2", "change3", "upstream1"]:
        path = os.path.join(gecko_root,
                            env.config["gecko"]["path"]["wpt"],
                            file)
        assert os.path.exists(path)
        with open(path) as f:
            assert f.read() == file.upper() + "\n"
    assert not os.path.exists(os.path.join(gecko_root,
                                           env.config["gecko"]["path"]["wpt"],
                                           "upstream2"))
    sync_point = landing.load_sync_point(git_gecko, git_wpt)
    assert sync_point["upstream"] == landing_rev


def test_landing_metadata(env, git_gecko, git_wpt, git_wpt_upstream, pull_request, set_pr_status,
                          hg_gecko_try, mock_mach):
    from conftest import create_file_data, gecko_changes

    trypush.Mach = mock_mach

    pr = pull_request([("Test commit", {"example/test1.html": "example_change"})])
    head_rev = pr._commits[0]["sha"]

    downstream.new_wpt_pr(git_gecko, git_wpt, pr)
    downstream_sync = set_pr_status(pr, "success")

    # Create a metadata commit
    with SyncLock.for_process(downstream_sync.process_name) as downstream_lock:
        with downstream_sync.as_mut(downstream_lock):
            git_work = downstream_sync.gecko_worktree.get()

            changes = gecko_changes(env, meta_changes={"example/test1.html":
                                                       "[test1.html]\n  expected: FAIL"})
            file_data, _ = create_file_data(changes, git_work.working_dir)
            downstream_sync.ensure_metadata_commit()
            git_work.index.add(file_data)
            downstream_sync._commit_metadata()

            assert downstream_sync.metadata_commit is not None
            downstream_sync.data["force-metadata-ready"] = True

    git_wpt_upstream.head.commit = head_rev
    git_wpt.remotes.origin.fetch()

    landing.wpt_push(git_gecko, git_wpt, [head_rev], create_missing=False)

    tree.is_open = lambda x: True
    landing_sync = landing.update_landing(git_gecko, git_wpt)

    assert len(landing_sync.gecko_commits) == 3
    assert landing_sync.gecko_commits[-1].metadata["wpt-type"] == "landing"
    assert landing_sync.gecko_commits[-2].metadata["wpt-type"] == "metadata"
    for item in file_data:
        assert item in landing_sync.gecko_commits[-2].commit.stats.files


def create_and_upstream_gecko_bug(env, git_gecko, git_wpt, hg_gecko_upstream,
                                  upstream_gecko_commit):
    # Create gecko bug and upstream it
    bug = "1234"
    test_changes = {"README": "Change README\n"}
    upstream_gecko_commit(test_changes=test_changes, bug=bug,
                          message="Change README")

    test_changes = {"CONFIG": "Change CONFIG\n"}
    rev = upstream_gecko_commit(test_changes=test_changes, bug=bug,
                                message="Change CONFIG")

    update_repositories(git_gecko, git_wpt, wait_gecko_commit=rev)
    upstream.gecko_push(git_gecko, git_wpt, "autoland", rev, raise_on_error=True)

    syncs = upstream.UpstreamSync.for_bug(git_gecko, git_wpt, bug)
    sync = syncs["open"].pop()
    env.gh_wpt.get_pull(sync.pr).mergeable = True

    hg_gecko_upstream.bookmark("mozilla/central", "-r", rev)

    update_repositories(git_gecko, git_wpt, wait_gecko_commit=rev)
    upstream.gecko_push(git_gecko, git_wpt, "central", rev, raise_on_error=True)

    return sync


def test_relanding_unchanged_upstreamed_pr(env, git_gecko, git_wpt, hg_gecko_upstream,
                                           pull_request, upstream_gecko_commit, mock_mach,
                                           set_pr_status, git_wpt_upstream):
    trypush.Mach = mock_mach

    # Create an unrelated PR that didn't come from Gecko
    pr0 = pull_request([("Non Gecko PR", {"SOMEFILE": "Made changes"})])
    unrelated_rev = pr0._commits[0]["sha"]
    downstream.new_wpt_pr(git_gecko, git_wpt, pr0)
    downstream_sync = set_pr_status(pr0, 'success')
    git_wpt_upstream.head.commit = unrelated_rev
    git_wpt.remotes.origin.fetch()

    sync = create_and_upstream_gecko_bug(env, git_gecko, git_wpt, hg_gecko_upstream,
                                         upstream_gecko_commit)

    # 'Merge' this upstream PR
    pr = env.gh_wpt.get_pull(sync.pr)
    pr["user"]["login"] = "not_bot"
    with SyncLock.for_process(sync.process_name) as upstream_sync_lock:
        with sync.as_mut(upstream_sync_lock):
            sync.push_commits()

    assert str(git_wpt_upstream.active_branch) == "master"
    git_wpt_upstream.git.merge('gecko/1234')  # TODO avoid hardcoding?

    # Create a ref on the upstream to simulate the pr than GH would setup
    git_wpt_upstream.create_head(
        'pr/%d' % pr['number'],
        commit=git_wpt_upstream.refs['gecko/1234'].commit.hexsha
    )
    git_wpt.remotes.origin.fetch()
    pr['merge_commit_sha'] = str(git_wpt_upstream.active_branch.commit.hexsha)
    pr['base'] = {'sha': unrelated_rev}
    env.gh_wpt.commit_prs[pr['merge_commit_sha']] = pr['number']

    # Update landing, the Non Gecko PR should be applied but not the Gecko one we upstreamed
    def mock_create(repo, msg, metadata, author=None, amend=False):
        # This commit should not be making it this far, should've been dropped earlier
        assert 'Bug 1234 [wpt PR 2] - [Gecko Bug 1234]' not in msg
        return DEFAULT

    m = Mock(side_effect=mock_create, wraps=sync_commit.Commit.create)
    with patch('sync.commit.Commit.create', m):
        landing_sync = landing.update_landing(git_gecko, git_wpt, include_incomplete=True)

    commits = landing_sync.gecko_commits._commits

    # Check that the upstreamed gecko PR didnt get pushed to try
    assert not any([c.bug == sync.bug for c in commits])
    # Check that our unrelated PR got pushed to try
    assert any([c.bug == downstream_sync.bug for c in commits])


def test_relanding_changed_upstreamed_pr(env, git_gecko, git_wpt, hg_gecko_upstream,
                                         upstream_gecko_commit, mock_mach, git_wpt_upstream):
    trypush.Mach = mock_mach

    sync = create_and_upstream_gecko_bug(env, git_gecko, git_wpt, hg_gecko_upstream,
                                         upstream_gecko_commit)

    # 'Merge' this upstream PR
    pr = env.gh_wpt.get_pull(sync.pr)
    pr["base"] = {"sha": git_wpt_upstream.head.commit.hexsha}
    pr["user"]["login"] = "not_bot"
    with SyncLock.for_process(sync.process_name) as upstream_sync_lock:
        with sync.as_mut(upstream_sync_lock):
            sync.push_commits()

    git_wpt_upstream.branches['gecko/1234'].checkout()
    extra_commit = git_commit(git_wpt_upstream, "Fixed pr before merge", {"EXTRA": "This fixes it"})
    git_wpt_upstream.branches.master.checkout()
    assert str(git_wpt_upstream.active_branch) == "master"
    git_wpt_upstream.git.merge('gecko/1234')  # TODO avoid hardcoding?

    # Create a ref on the upstream to simulate the pr than GH would setup
    git_wpt_upstream.create_head(
        'pr/%d' % pr['number'],
        commit=git_wpt_upstream.refs['gecko/1234'].commit.hexsha
    )
    git_wpt.remotes.origin.fetch()
    pr['merge_commit_sha'] = str(git_wpt_upstream.active_branch.commit.hexsha)
    env.gh_wpt.commit_prs[pr['merge_commit_sha']] = pr['number']

    landing_sync = landing.update_landing(git_gecko, git_wpt, include_incomplete=True)
    commits = landing_sync.gecko_commits._commits

    assert len(commits) == 2
    # Check that the first commit is our "fix commit" which didn't come from Gecko
    assert commits[0].metadata['wpt-commits'] == extra_commit.hexsha
    # Check that the other commit is the bot's push commit
    assert commits[1].metadata['MANUAL PUSH'] == "wpt sync bot"


def test_accept_failures(landing_with_try_push, mock_tasks):
    sync = landing_with_try_push
    with patch.object(tc.TaskGroup, "tasks",
                      property(Mock(return_value=mock_tasks(failed=["foo", "foobar"],
                                                            completed=["bar"])))):
        assert sync.try_result() == landing.TryPushResult.too_many_failures
        with SyncLock.for_process(sync.process_name) as lock:
            with sync.latest_try_push.as_mut(lock):
                sync.latest_try_push.accept_failures = True
        assert sync.try_result() == landing.TryPushResult.acceptable_failures
