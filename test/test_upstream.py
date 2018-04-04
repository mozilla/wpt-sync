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

    syncs = upstream.UpstreamSync.for_bug(git_gecko, git_wpt, bug)
    assert syncs.keys() == ["open"]
    assert len(syncs["open"]) == 1
    sync = syncs["open"][0]
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

    syncs = upstream.UpstreamSync.for_bug(git_gecko, git_wpt, bug)
    assert syncs.keys() == ["open"]
    assert len(syncs["open"]) == 1
    sync = syncs["open"][0]
    assert sync.bug == "1234"
    assert sync.status == "open"
    assert len(sync.gecko_commits) == 1
    assert len(sync.wpt_commits) == 1
    assert sync.pr

    backout_rev = upstream_gecko_backout(rev, bug)

    update_repositories(git_gecko, git_wpt, wait_gecko_commit=backout_rev)
    upstream.push(git_gecko, git_wpt, "inbound", backout_rev, raise_on_error=True)
    syncs = upstream.UpstreamSync.for_bug(git_gecko, git_wpt, bug)
    assert syncs.keys() == ["incomplete"]
    assert len(syncs["incomplete"]) == 1
    sync = syncs["incomplete"][0]
    assert sync.bug == "1234"
    assert len(sync.gecko_commits) == 0
    assert len(sync.wpt_commits) == 0
    assert sync.status == "incomplete"


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

    syncs = upstream.UpstreamSync.for_bug(git_gecko, git_wpt, bug)
    assert syncs.keys() == ["incomplete"]
    assert len(syncs["incomplete"]) == 1
    sync = syncs["incomplete"][0]
    assert sync.status == "incomplete"
    assert sync._process_name.seq_id == 0
    assert len(sync.upstreamed_gecko_commits) == 0

    # Make some unrelated commit in the root
    upstream_gecko_commit(other_changes=test_changes, bug="1235",
                          message="Change other file")

    relanding_rev = upstream_gecko_commit(test_changes=test_changes, bug=bug,
                                          message="Reland: Change README")

    update_repositories(git_gecko, git_wpt, wait_gecko_commit=rev)
    upstream.push(git_gecko, git_wpt, "inbound", relanding_rev, raise_on_error=True)

    syncs = upstream.UpstreamSync.for_bug(git_gecko, git_wpt, bug)
    assert syncs.keys() == ["open"]
    assert len(syncs["open"]) == 1
    sync = syncs["open"][0]
    assert sync._process_name.seq_id == 0
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

    syncs = upstream.UpstreamSync.for_bug(git_gecko, git_wpt, bug)
    assert syncs.keys() == ["open"]
    assert len(syncs["open"]) == 1
    sync = syncs["open"][0]
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

    syncs = upstream.UpstreamSync.for_bug(git_gecko, git_wpt, bug)
    assert syncs.keys() == ["open"]
    assert len(syncs["open"]) == 1
    sync = syncs["open"][0]
    env.gh_wpt.get_pull(sync.pr).mergeable = True
    original_remote_branch = sync.remote_branch

    hg_gecko_upstream.bookmark("mozilla/central", "-r", rev)

    update_repositories(git_gecko, git_wpt, wait_gecko_commit=rev)
    pushed, landed, failed = upstream.push(git_gecko, git_wpt, "central", rev,
                                           raise_on_error=True)

    syncs = upstream.UpstreamSync.for_bug(git_gecko, git_wpt, bug)
    assert syncs == {"wpt-merged": [sync]}
    assert sync.gecko_landed()
    assert sync.status == "wpt-merged"
    assert original_remote_branch not in git_wpt.remotes.origin.refs
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
    syncs = upstream.UpstreamSync.for_bug(git_gecko, git_wpt, bug)
    assert syncs.keys() == ["open"]
    assert len(syncs["open"]) == 1
    sync = syncs["open"][0]
    env.gh_wpt.get_pull(sync.pr).mergeable = True

    env.gh_wpt.set_status(sync.pr, "failure", "http://test/", "tests failed",
                          "continuous-integration/travis-ci/pr")
    upstream.status_changed(git_gecko, git_wpt, sync,
                            "continuous-integration/travis-ci/pr",
                            "failure", "http://test/", sync.wpt_commits.head.sha1)
    assert sync.last_pr_check == {"state": "failure", "sha": sync.wpt_commits.head.sha1}
    hg_gecko_upstream.bookmark("mozilla/central", "-r", rev)

    update_repositories(git_gecko, git_wpt, wait_gecko_commit=rev)
    pushed, landed, failed = upstream.push(git_gecko, git_wpt, "central", rev,
                                           raise_on_error=True)

    env.gh_wpt.set_status(sync.pr, "success", "http://test/", "tests failed",
                          "continuous-integration/travis-ci/pr")
    upstream.status_changed(git_gecko, git_wpt, sync,
                            "continuous-integration/travis-ci/pr",
                            "success", "http://test/", sync.wpt_commits.head.sha1)
    assert sync.last_pr_check == {"state": "success", "sha": sync.wpt_commits.head.sha1}
    assert sync.gecko_landed()
    assert sync.status == "wpt-merged"


def test_no_upstream_downstream(env, git_gecko, git_wpt, upstream_gecko_commit,
                                upstream_gecko_backout):

    hg_rev = upstream_gecko_commit(test_changes={"README": "Example change"},
                                   message="""Example change

wpt-pr: 1
wpt-commits: 0000000000000000000000000000000000000000""")
    update_repositories(git_gecko, git_wpt, wait_gecko_commit=hg_rev)
    pushed, landed, failed = upstream.push(git_gecko, git_wpt, "mozilla-inbound",
                                           hg_rev, raise_on_error=True)
    assert not pushed
    assert not landed
    assert not failed
    backout_rev = upstream_gecko_backout(hg_rev, "1234")
    update_repositories(git_gecko, git_wpt, wait_gecko_commit=backout_rev)
    pushed, landed, failed = upstream.push(git_gecko, git_wpt, "mozilla-inbound",
                                           backout_rev, raise_on_error=True)
    assert not pushed
    assert not landed
    assert not failed


def test_upstream_existing(env, git_gecko, git_wpt, upstream_gecko_commit, upstream_wpt_commit):
    bug = "1234"
    test_changes_1 = {"README": "Change README\n"}
    upstream_gecko_commit(test_changes=test_changes_1, bug=bug,
                          message="Change README")
    test_changes_2 = {"OTHER": "Add other file\n"}
    gecko_rev_2 = upstream_gecko_commit(test_changes=test_changes_2, bug=bug,
                                        message="Add other")

    upstream_wpt_commit(file_data=test_changes_1)
    update_repositories(git_gecko, git_wpt, wait_gecko_commit=gecko_rev_2)
    pushed, landed, failed = upstream.push(git_gecko, git_wpt, "inbound", gecko_rev_2,
                                           raise_on_error=True)
    assert len(pushed) == 1
    assert len(landed) == 0
    assert len(failed) == 0

    syncs = upstream.UpstreamSync.for_bug(git_gecko, git_wpt, bug)
    sync = pushed.pop()
    assert syncs == {"open": [sync]}
    assert sync.bug == "1234"
    assert sync.status == "open"
    assert len(sync.gecko_commits) == 2
    assert len(sync.wpt_commits) == 1
    assert sync.gecko_commits.head.sha1 == git_gecko.cinnabar.hg2git(gecko_rev_2)

    wpt_commit = sync.wpt_commits[0]

    assert wpt_commit.msg.split("\n")[0] == "Add other"
    assert "OTHER" in wpt_commit.commit.tree
    assert wpt_commit.metadata == {
        'gecko-integration-branch': 'mozilla-inbound',
        'bugzilla-url': 'https://bugzilla-dev.allizom.org/show_bug.cgi?id=1234',
        'gecko-commit': gecko_rev_2
    }

    # Now make another push to the same bug and check we handle it correctly

    test_changes_3 = {"YET_ANOTHER": "Add more files\n"}
    gecko_rev_3 = upstream_gecko_commit(test_changes=test_changes_3, bug=bug,
                                        message="Add more")
    update_repositories(git_gecko, git_wpt, wait_gecko_commit=gecko_rev_3)
    pushed, landed, failed = upstream.push(git_gecko, git_wpt, "inbound", gecko_rev_3,
                                           raise_on_error=True)
    assert len(sync.gecko_commits) == 3
    assert len(sync.wpt_commits) == 2
    assert ([item.metadata.get("gecko-commit") for item in sync.wpt_commits] ==
            [gecko_rev_2, gecko_rev_3])


def test_upstream_multi(env, git_gecko, git_wpt, upstream_gecko_commit):
    bug = "1234"
    test_changes = {"README": "Add README\n"}
    rev_0 = upstream_gecko_commit(test_changes=test_changes, bug=bug,
                                  message="Add README")

    update_repositories(git_gecko, git_wpt, wait_gecko_commit=rev_0)
    pushed, landed, failed = upstream.push(git_gecko, git_wpt, "inbound", rev_0,
                                           raise_on_error=True)
    assert len(pushed) == 1
    sync_0 = pushed.pop()

    test_changes = {"README1": "Add README1\n"}
    rev_1 = upstream_gecko_commit(test_changes=test_changes, bug=bug,
                                  message="Add README1")

    update_repositories(git_gecko, git_wpt, wait_gecko_commit=rev_1)
    pushed, landed, failed = upstream.push(git_gecko, git_wpt, "inbound", rev_1,
                                           raise_on_error=True)
    assert len(pushed) == 1
    assert pushed == {sync_0}
    assert len(sync_0.upstreamed_gecko_commits) == 2
    assert sync_0._process_name.seq_id == 0

    sync_0.finish("wpt-merged")
    assert sync_0.status == "wpt-merged"

    # Add new files each time to avoid conflicts since we don't
    # Actually do the merges
    test_changes = {"README2": "Add README2\n"}
    rev_2 = upstream_gecko_commit(test_changes=test_changes,
                                  bug=bug,
                                  message="Add README2")

    update_repositories(git_gecko, git_wpt, wait_gecko_commit=rev_2)
    pushed, landed, failed = upstream.push(git_gecko, git_wpt, "inbound", rev_2,
                                           raise_on_error=True)

    assert len(pushed) == 1
    sync_1 = pushed.pop()
    assert sync_1 != sync_0
    assert sync_1._process_name.seq_id == 1

    syncs = upstream.UpstreamSync.for_bug(git_gecko, git_wpt, bug)
    assert set(syncs.keys()) == {"open", "wpt-merged"}
    assert set(syncs["open"]) == {sync_1}
    assert set(syncs["wpt-merged"]) == {sync_0}

    sync_0.finish()
    sync_1.finish()

    test_changes = {"README3": "Add README3\n"}
    rev_3 = upstream_gecko_commit(test_changes=test_changes, bug=bug,
                                  message="Add README3")

    update_repositories(git_gecko, git_wpt, wait_gecko_commit=rev_3)
    pushed, landed, failed = upstream.push(git_gecko, git_wpt, "inbound", rev_3,
                                           raise_on_error=True)
    assert len(pushed) == 1
    sync_2 = pushed.pop()
    assert sync_2._process_name not in (sync_1._process_name, sync_0._process_name)
    assert sync_2._process_name.seq_id == 2

    syncs = upstream.UpstreamSync.for_bug(git_gecko, git_wpt, bug)
    assert set(syncs.keys()) == {"open", "complete"}
    assert set(syncs["open"]) == {sync_2}
    assert set(syncs["complete"]) == {sync_0, sync_1}
