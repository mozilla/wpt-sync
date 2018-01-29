import os

from sync import landing, downstream, tree, trypush, upstream
from sync import commit as sync_commit


def test_upstream_commit(env, git_gecko, git_wpt, git_wpt_upstream, pull_request):
    pr = pull_request([("Test commit", {"README": "example_change"})])
    head_rev = pr._commits[0]["sha"]
    git_wpt_upstream.head.commit = head_rev
    landing.wpt_push(git_wpt, [head_rev])
    assert sync_commit.WptCommit(git_wpt_upstream,
                                 git_wpt_upstream.head.commit).pr() == pr["number"]


def test_land_try(env, git_gecko, git_wpt, git_wpt_upstream, pull_request, set_pr_status,
                  hg_gecko_try, mock_mach):
    pr = pull_request([("Test commit", {"README": "example_change"})])
    head_rev = pr._commits[0]["sha"]

    trypush.Mach = mock_mach
    downstream.new_wpt_pr(git_gecko, git_wpt, pr)
    sync = set_pr_status(pr, "success")

    git_wpt_upstream.head.commit = head_rev
    landing.wpt_push(git_wpt, [head_rev])

    sync.metadata_ready = True

    tree.is_open = lambda x: True
    landing.land_to_gecko(git_gecko, git_wpt)

    try_push = sync.latest_try_push
    assert try_push is not None
    assert try_push.status == "open"
    assert try_push.stability is False
    mach_command = mock_mach.get_log()[-1]
    assert mach_command["command"] == "mach"
    assert mach_command["args"] == ("try", "fuzzy", "-q", "web-platform-tests !pgo !ccov",
                                    "--artifact")


def test_land_commit(env, git_gecko, git_wpt, git_wpt_upstream, pull_request, set_pr_status,
                     hg_gecko_try):
    pr = pull_request([("Test commit", {"README": "example_change"})])
    head_rev = pr._commits[0]["sha"]

    downstream.new_wpt_pr(git_gecko, git_wpt, pr)
    downstream_sync = set_pr_status(pr, "success")

    git_wpt_upstream.head.commit = head_rev
    landing.wpt_push(git_wpt, [head_rev])

    downstream_sync.metadata_ready = True

    tree.is_open = lambda x: True
    sync = landing.land_to_gecko(git_gecko, git_wpt)

    try_push = sync.latest_try_push

    try_push.download_raw_logs = lambda: []
    landing.try_push_complete(git_gecko, git_wpt, try_push, sync)

    assert sync.status == "complete"
    new_head = git_gecko.remotes.mozilla.refs["bookmarks/mozilla/inbound"].commit
    assert "Update web-platform-tests to %s" % head_rev in new_head.message
    assert new_head.tree["testing/web-platform/tests/README"].data_stream.read() == "example_change"
    sync_point = landing.load_sync_point(git_gecko, git_wpt)
    assert sync_point["local"] == new_head.parents[0].hexsha
    assert sync_point["upstream"] == head_rev


def test_landing_reapply(env, git_gecko, git_wpt, git_wpt_upstream, pull_request, set_pr_status,
                         hg_gecko_upstream, upstream_gecko_commit, upstream_wpt_commit,
                         hg_gecko_try, mock_mach):
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

    pushed, _, _ = upstream.push(git_gecko, git_wpt, "inbound", rev,
                                 raise_on_error=True)
    sync_1 = pushed.pop()

    # Update central
    hg_gecko_upstream.bookmark("mozilla/central", "-r", rev)

    # Merge the upstream change
    remote_branch, _ = sync_1.remote_branch()
    git_wpt_upstream.git.checkout(remote_branch)
    git_wpt_upstream.git.rebase("master")
    git_wpt_upstream.git.checkout("master")
    git_wpt_upstream.git.merge(remote_branch, ff_only=True)

    # Add second gecko change
    test_changes = {"change2": "CHANGE2\n"}
    rev = upstream_gecko_commit(test_changes=test_changes, bug="1112",
                                message="Add change2 file")

    pushed, _, _ = upstream.push(git_gecko, git_wpt, "inbound", rev,
                                 raise_on_error=True)
    sync_2 = pushed.pop()

    hg_gecko_upstream.bookmark("mozilla/central", "-r", rev)

    # Merge the gecko change
    remote_branch, _ = sync_2.remote_branch()
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
    downstream_sync.metadata_ready = True

    # This is the commit we should land to
    landing_rev = git_wpt_upstream.git.rev_parse("HEAD")

    # Add an upstream commit that doesn't have metadata
    pr = pull_request([("Upstream change 2", {"upstream2": "UPSTREAM2\n"})])
    head_rev = pr._commits[0]["sha"]
    downstream.new_wpt_pr(git_gecko, git_wpt, pr)
    downstream_sync = set_pr_status(pr, "success")
    git_wpt_upstream.head.commit = head_rev
    git_wpt_upstream.git.reset(hard=True)
    downstream_sync.metadata_ready = False

    # Add third gecko change
    test_changes = {"change3": "CHANGE3\n"}
    rev = upstream_gecko_commit(test_changes=test_changes, bug="1113",
                                message="Add change3 file")
    pushed, _, _ = upstream.push(git_gecko, git_wpt, "inbound", rev,
                                 raise_on_error=True)

    # Now start a landing
    tree.is_open = lambda x: True
    sync = landing.land_to_gecko(git_gecko, git_wpt)

    assert sync is not None

    try_push = sync.latest_try_push
    try_push.download_raw_logs = lambda: []
    landing.try_push_complete(git_gecko, git_wpt, try_push, sync)

    hg_gecko_upstream.update()
    gecko_root = hg_gecko_upstream.root().strip()
    assert (hg_gecko_upstream
            .log("-l1", "--template={desc|firstline}")
            .strip()
            .endswith("[wpt-sync] Update web-platform-tests to %s" % landing_rev))
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
