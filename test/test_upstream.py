from sync import upstream
from sync.gitutils import update_repositories


def test_create_pr(env, git_gecko, git_wpt, upstream_gecko_commit):
    bug = "1234"
    test_changes = {"README": "Change README\n"}
    rev = upstream_gecko_commit(test_changes=test_changes, bug=bug,
                                message="Change README")

    update_repositories(git_gecko, git_wpt, wait_gecko_commit=rev)
    pushed, landed, failed = upstream.push(git_gecko, git_wpt, "inbound", rev,
                                           raise_on_error=True)
    assert len(pushed) == 1
    assert len(landed) == 0
    assert len(failed) == 0

    sync = upstream.UpstreamSync.for_bug(git_gecko, git_wpt, bug)
    assert sync is not None
    assert sync.bug == "1234"
    assert sync.status == "open"
    assert len(sync.gecko_commits) == 1
    assert len(sync.wpt_commits) == 1
    assert sync.gecko_commits.head.sha1 == git_gecko.cinnabar.hg2git(rev)

    wpt_commit = sync.wpt_commits[0]

    assert wpt_commit.msg.split("\n")[0] == "Change README"
    assert "README" in wpt_commit.commit.tree
    assert wpt_commit.metadata == {
        'gecko-integration-branch': 'mozilla-inbound',
        'bugzilla-url': 'https://bugzilla-dev.allizom.org/show_bug.cgi?id=1234',
        'gecko-commit': rev
    }
    assert sync.pr
    assert "Posting to bug %s" % bug in env.bz.output.getvalue()
    assert "Created PR with id %s" % sync.pr in env.gh_wpt.output.getvalue()


def test_create_pr_backout(git_gecko, git_wpt, upstream_gecko_commit,
                           upstream_gecko_backout):
    bug = "1234"
    test_changes = {"README": "Change README\n"}
    rev = upstream_gecko_commit(test_changes=test_changes, bug=bug,
                                message="Change README")

    update_repositories(git_gecko, git_wpt, wait_gecko_commit=rev)
    upstream.push(git_gecko, git_wpt, "inbound", rev,
                  raise_on_error=True)

    sync = upstream.UpstreamSync.for_bug(git_gecko, git_wpt, bug)
    assert sync.bug == "1234"
    assert sync.status == "open"
    assert len(sync.gecko_commits) == 1
    assert len(sync.wpt_commits) == 1
    assert sync.pr

    backout_rev = upstream_gecko_backout(rev, bug)

    update_repositories(git_gecko, git_wpt, wait_gecko_commit=rev)
    upstream.push(git_gecko, git_wpt, "inbound", backout_rev, raise_on_error=True)
    sync = upstream.UpstreamSync.for_bug(git_gecko, git_wpt, bug)
    assert sync.bug == "1234"
    assert len(sync.gecko_commits) == 0
    assert len(sync.wpt_commits) == 0
    assert sync.status == "complete"


def test_create_pr_backout_reland(git_gecko, git_wpt, upstream_gecko_commit,
                                  upstream_gecko_backout):
    bug = "1234"
    test_changes = {"README": "Change README\n"}
    rev = upstream_gecko_commit(test_changes=test_changes, bug=bug,
                                message="Change README")

    update_repositories(git_gecko, git_wpt, wait_gecko_commit=rev)
    upstream.push(git_gecko, git_wpt, "inbound", rev,
                  raise_on_error=True)

    backout_rev = upstream_gecko_backout(rev, bug)

    update_repositories(git_gecko, git_wpt, wait_gecko_commit=rev)
    upstream.push(git_gecko, git_wpt, "inbound", backout_rev, raise_on_error=True)

    # Make some unrelated commit in the root
    upstream_gecko_commit(other_changes=test_changes, bug="1235",
                          message="Change other file")

    relanding_rev = upstream_gecko_commit(test_changes=test_changes, bug=bug,
                                          message="Reland: Change README")

    update_repositories(git_gecko, git_wpt, wait_gecko_commit=rev)
    upstream.push(git_gecko, git_wpt, "inbound", relanding_rev, raise_on_error=True)

    sync = upstream.UpstreamSync.for_bug(git_gecko, git_wpt, bug)
    assert sync.bug == "1234"
    assert len(sync.gecko_commits) == 1
    assert len(sync.wpt_commits) == 1
    assert sync.status == "open"
    sync.wpt_commits[0].metadata["gecko-commit"] == relanding_rev


def test_create_partial_backout_reland(git_gecko, git_wpt, upstream_gecko_commit,
                                       upstream_gecko_backout):
    bug = "1234"
    test_changes = {"README": "Change README\n"}
    rev0 = upstream_gecko_commit(test_changes=test_changes, bug=bug,
                                 message="Change README")
    rev1 = upstream_gecko_commit(test_changes={"README": "Change README again\n"}, bug=bug,
                                 message="Change README again")

    update_repositories(git_gecko, git_wpt)
    upstream.push(git_gecko, git_wpt, "inbound", rev1,
                  raise_on_error=True)

    upstream_gecko_backout(rev1, bug)

    # Make some unrelated commit in the root
    upstream_gecko_commit(other_changes=test_changes, bug="1235",
                          message="Change other file")

    relanding_rev = upstream_gecko_commit(test_changes={"README": "Change README once more\n"},
                                          bug=bug,
                                          message="Change README once more")

    update_repositories(git_gecko, git_wpt, wait_gecko_commit=relanding_rev)
    upstream.push(git_gecko, git_wpt, "inbound", relanding_rev, raise_on_error=True)

    sync = upstream.UpstreamSync.for_bug(git_gecko, git_wpt, bug)
    assert sync.bug == "1234"
    assert len(sync.gecko_commits) == 2
    assert len(sync.wpt_commits) == 2
    assert sync.status == "open"
    sync.wpt_commits[0].metadata["gecko-commit"] == rev0
    sync.wpt_commits[1].metadata["gecko-commit"] == relanding_rev


def test_land_pr(env, git_gecko, git_wpt, hg_gecko_upstream, upstream_gecko_commit):
    bug = "1234"
    test_changes = {"README": "Change README\n"}
    rev = upstream_gecko_commit(test_changes=test_changes, bug=bug,
                                message="Change README")

    update_repositories(git_gecko, git_wpt, wait_gecko_commit=rev)
    pushed, landed, failed = upstream.push(git_gecko, git_wpt, "inbound", rev,
                                           raise_on_error=True)

    sync = upstream.UpstreamSync.for_bug(git_gecko, git_wpt, bug)
    env.gh_wpt.get_pull(sync.pr).mergeable = True

    hg_gecko_upstream.bookmark("mozilla/central", "-r", rev)

    update_repositories(git_gecko, git_wpt, wait_gecko_commit=rev)
    pushed, landed, failed = upstream.push(git_gecko, git_wpt, "central", rev,
                                           raise_on_error=True)

    sync = upstream.UpstreamSync.for_bug(git_gecko, git_wpt, bug)
    assert sync.gecko_landed()
    assert sync.status == "complete"
    pr = env.gh_wpt.get_pull(sync.pr)
    assert pr.merged


def test_land_pr_after_status_change(env, git_gecko, git_wpt, hg_gecko_upstream,
                                     upstream_gecko_commit):
    bug = "1234"
    test_changes = {"README": "Change README\n"}
    rev = upstream_gecko_commit(test_changes=test_changes, bug=bug,
                                message="Change README")

    update_repositories(git_gecko, git_wpt, wait_gecko_commit=rev)
    pushed, landed, failed = upstream.push(git_gecko, git_wpt, "inbound", rev,
                                           raise_on_error=True)
    sync = upstream.UpstreamSync.for_bug(git_gecko, git_wpt, bug)
    env.gh_wpt.get_pull(sync.pr).mergeable = True

    env.gh_wpt.set_status(sync.pr, "failed", "http://test/", "tests failed",
                          "continuous-integration/travis-ci/pr")
    upstream.status_changed(git_gecko, git_wpt, sync,
                            "continuous-integration/travis-ci/pr",
                            "failed", "http://test/", sync.wpt_commits.head.sha1)

    hg_gecko_upstream.bookmark("mozilla/central", "-r", rev)

    update_repositories(git_gecko, git_wpt, wait_gecko_commit=rev)
    pushed, landed, failed = upstream.push(git_gecko, git_wpt, "central", rev,
                                           raise_on_error=True)

    sync = upstream.UpstreamSync.for_bug(git_gecko, git_wpt, bug)
    env.gh_wpt.set_status(sync.pr, "success", "http://test/", "tests failed",
                          "continuous-integration/travis-ci/pr")
    upstream.status_changed(git_gecko, git_wpt, sync,
                            "continuous-integration/travis-ci/pr",
                            "success", "http://test/", sync.wpt_commits.head.sha1)

    assert sync.gecko_landed()
    assert sync.status == "complete"
