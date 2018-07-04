import os

from mock import Mock, patch

from sync import landing, downstream, tc, tree, trypush, upstream
from sync import commit as sync_commit
from sync.gitutils import update_repositories


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

    sync.data["force-metadata-ready"] = True

    tree.is_open = lambda x: True
    landing_sync = landing.update_landing(git_gecko, git_wpt)

    assert landing_sync is not None
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
    assert try_push is not None
    assert try_push.status == "open"
    assert try_push.stability is False
    mach_command = mock_mach.get_log()[-1]
    assert mach_command["command"] == "mach"
    assert mach_command["args"] == ("try", "fuzzy", "-q", "web-platform-tests !pgo !ccov",
                                    "--artifact")


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

    downstream_sync.data["force-metadata-ready"] = True

    tree.is_open = lambda x: True
    sync = landing.update_landing(git_gecko, git_wpt)

    # Set the landing sync point to current central
    sync.last_sync_point(git_gecko, "mozilla-central",
                         env.config["gecko"]["refs"]["central"])

    try_push = sync.latest_try_push
    try_push.taskgroup_id = "abcdef"
    with patch.object(try_push, "download_raw_logs", Mock(return_value=[])):
        with patch.object(tc.TaskGroup, "tasks",
                          property(Mock(return_value=mock_tasks(completed=["foo"])))):
            landing.try_push_complete(git_gecko, git_wpt, try_push, sync)

    assert sync.status == "open"
    new_head = git_gecko.remotes.mozilla.refs["bookmarks/mozilla/inbound"].commit
    assert "Update web-platform-tests to %s" % head_rev in new_head.message
    assert new_head.tree["testing/web-platform/tests/README"].data_stream.read() == "example_change"
    sync_point = landing.load_sync_point(git_gecko, git_wpt)
    assert sync_point["local"] == new_head.parents[0].hexsha
    assert sync_point["upstream"] == head_rev
    # Update central to contain the landing
    git_gecko.refs["mozilla/bookmarks/mozilla/central"].commit = new_head
    with patch("sync.landing.tasks.land.apply_async") as mock_apply:
        landing.gecko_push(git_gecko, git_wpt, "mozilla-central",
                           git_gecko.cinnabar.git2hg(new_head))
        assert mock_apply.call_count == 1
    assert sync.status == "complete"


def test_try_push_exceeds_failure_threshold(git_gecko, git_wpt, try_push, mock_tasks):
    # 2/3 failure rate, too high
    with patch.object(tc.TaskGroup, "tasks",
                      property(Mock(return_value=mock_tasks(failed=["foo", "foobar"],
                                                            completed=["bar"])))):
        try_push.taskgroup_id = "abcdef"
        sync = try_push.sync(git_gecko, git_wpt)
        landing.try_push_complete(git_gecko, git_wpt, try_push, sync)
    assert try_push.status == "complete"
    assert "too many failures" in sync.error["message"]


def test_try_push_retriggers_failures(git_gecko, git_wpt, try_push, mock_tasks, env):
    # 20% failure rate, okay
    tasks = Mock(return_value=mock_tasks(
        failed=["foo"], completed=["bar", "baz", "boo", "faz"])
    )
    sync = try_push.sync(git_gecko, git_wpt)
    with patch.object(tc.TaskGroup, "tasks", property(tasks)):
        with patch('sync.trypush.auth_tc.retrigger',
                   return_value=["job"] * try_push._retrigger_count):
            landing.try_push_complete(git_gecko, git_wpt, try_push, sync)
            assert "Pushed to try (stability)" in env.bz.output.getvalue()
            assert try_push.status == "complete"
            assert sync.latest_try_push != try_push
            try_push = sync.latest_try_push
            # Give try push a fake taskgroup id
            try_push.taskgroup_id = "abcdef"
            assert try_push.stability
            landing.try_push_complete(git_gecko, git_wpt, try_push, sync)
            assert "Retriggered failing web-platform-test tasks" in env.bz.output.getvalue()
            assert try_push.status != "complete"


def test_download_logs_after_retriggers_complete(git_gecko, git_wpt, try_push, mock_tasks, env):
    # > 80% of retriggered "foo" tasks pass, so we consider the "foo" failure intermittent
    mock_tasks = Mock(return_value=mock_tasks(
        failed=["foo", "foo", "bar"],
        completed=["bar", "bar", "bar" "baz", "boo", "foo", "foo", "foo", "foo", "foo",
                   "foobar", "foobar", "foobar"])
    )
    with patch.object(tc.TaskGroup, "tasks", property(mock_tasks)):
        sync = landing.update_landing(git_gecko, git_wpt)
        try_push.download_raw_logs = Mock(return_value=[])
        try_push["stability"] = True
        landing.try_push_complete(git_gecko, git_wpt, try_push, sync)
        try_push.download_raw_logs.assert_called_with(exclude=["foo"])
        assert sync.status == "open"
        assert try_push.status == "complete"


def test_no_download_logs_after_all_try_tasks_success(git_gecko, git_wpt, try_push, mock_tasks,
                                                      env):
    tasks = Mock(return_value=mock_tasks(completed=["bar", "baz", "boo"]))
    with patch.object(tc.TaskGroup, "tasks", property(tasks)):
        sync = landing.update_landing(git_gecko, git_wpt)
        try_push.download_raw_logs = Mock(return_value=[])
        landing.try_push_complete(git_gecko, git_wpt, try_push, sync)
        # no intermittents in the try push
        try_push.download_raw_logs.assert_not_called()
        assert sync.status == "open"
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
    pushed, _, _ = upstream.gecko_push(git_gecko, git_wpt, "inbound", rev,
                                       raise_on_error=True)
    sync_1 = pushed.pop()

    # Update central
    hg_gecko_upstream.bookmark("mozilla/central", "-r", rev)

    # Merge the upstream change
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
    pushed, _, _ = upstream.gecko_push(git_gecko, git_wpt, "inbound", rev,
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
    pushed, _, _ = upstream.gecko_push(git_gecko, git_wpt, "inbound", rev,
                                       raise_on_error=True)

    # Now start a landing
    tree.is_open = lambda x: True
    sync = landing.update_landing(git_gecko, git_wpt)

    assert sync is not None

    try_push = sync.latest_try_push
    try_push.taskgroup_id = "abcde"
    try_push.download_raw_logs = Mock(return_value=[])
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
