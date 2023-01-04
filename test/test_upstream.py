from sync import commit as sync_commit, upstream
from sync.gitutils import update_repositories
from sync.lock import SyncLock
from sync.repos import cinnabar
from conftest import git_commit


def test_create_pr(env, git_gecko, git_wpt, upstream_gecko_commit):
    bug = 1234
    test_changes = {"README": b"Change README\n"}
    rev = upstream_gecko_commit(test_changes=test_changes, bug=bug,
                                message=b"Change README")

    update_repositories(git_gecko, git_wpt, wait_gecko_commit=rev)

    initial_data_commits = list(git_gecko.iter_commits(env.config["sync"]["ref"]))
    pushed, landed, failed = upstream.gecko_push(git_gecko, git_wpt, "autoland", rev,
                                                 raise_on_error=True)
    assert len(pushed) == 1
    assert len(landed) == 0
    assert len(failed) == 0
    # Creating one upstream sync creates an initial commit and then one to update the
    # relevant PR id and remote branch. This could be reduced to just one.
    data_commits = list(git_gecko.iter_commits(env.config["sync"]["ref"]))
    assert len(data_commits) == len(initial_data_commits) + 2

    syncs = upstream.UpstreamSync.for_bug(git_gecko, git_wpt, bug)

    assert list(syncs.keys()) == ["open"]
    assert len(syncs["open"]) == 1
    sync = syncs["open"].pop()
    assert sync.bug == 1234
    assert sync.status == "open"
    assert len(sync.gecko_commits) == 1
    assert len(sync.wpt_commits) == 1
    assert sync.gecko_commits.head.sha1 == cinnabar(git_gecko).hg2git(rev)

    wpt_commit = sync.wpt_commits[0]

    assert wpt_commit.msg.split(b"\n")[0] == b"Change README"
    assert "README" in wpt_commit.commit.tree
    assert wpt_commit.metadata == {
        'bugzilla-url': 'https://bugzilla-dev.allizom.org/show_bug.cgi?id=1234',
        'gecko-commit': rev
    }
    assert sync.pr
    assert "Posting to bug %s" % bug in env.bz.output.getvalue()
    assert "Created PR with id %s" % sync.pr in env.gh_wpt.output.getvalue()
    assert sync.gecko_commits[0].upstream_sync(git_gecko, git_wpt) == sync


def test_create_pr_backout(git_gecko, git_wpt, upstream_gecko_commit,
                           upstream_gecko_backout):
    bug = 1234
    test_changes = {"README": b"Change README\n"}
    rev = upstream_gecko_commit(test_changes=test_changes, bug=bug,
                                message=b"Change README")

    update_repositories(git_gecko, git_wpt, wait_gecko_commit=rev)
    upstream.gecko_push(git_gecko, git_wpt, "autoland", rev,
                        raise_on_error=True)

    syncs = upstream.UpstreamSync.for_bug(git_gecko, git_wpt, bug)
    assert list(syncs.keys()) == ["open"]
    assert len(syncs["open"]) == 1
    sync = syncs["open"].pop()
    assert sync.bug == 1234
    assert sync.status == "open"
    assert len(sync.gecko_commits) == 1
    assert len(sync.wpt_commits) == 1
    assert sync.pr

    backout_rev = upstream_gecko_backout(rev, bug)

    update_repositories(git_gecko, git_wpt, wait_gecko_commit=backout_rev)
    upstream.gecko_push(git_gecko, git_wpt, "autoland", backout_rev, raise_on_error=True)
    syncs = upstream.UpstreamSync.for_bug(git_gecko, git_wpt, bug)
    assert list(syncs.keys()) == ["incomplete"]
    assert len(syncs["incomplete"]) == 1
    sync = syncs["incomplete"].pop()
    assert sync.bug == 1234
    assert len(sync.gecko_commits) == 0
    assert len(sync.wpt_commits) == 1
    assert len(sync.upstreamed_gecko_commits) == 1
    assert sync.status == "incomplete"
    backout_commit = sync_commit.GeckoCommit(git_gecko, cinnabar(git_gecko).hg2git(rev))
    assert backout_commit.upstream_sync(git_gecko, git_wpt) == sync


def test_create_pr_backout_reland(git_gecko, git_wpt, upstream_gecko_commit,
                                  upstream_gecko_backout):
    bug = 1234
    test_changes = {"README": b"Change README\n"}
    rev = upstream_gecko_commit(test_changes=test_changes, bug=bug,
                                message=b"Change README")

    update_repositories(git_gecko, git_wpt, wait_gecko_commit=rev)
    upstream.gecko_push(git_gecko, git_wpt, "autoland", rev,
                        raise_on_error=True)

    backout_rev = upstream_gecko_backout(rev, bug)

    update_repositories(git_gecko, git_wpt, wait_gecko_commit=rev)
    upstream.gecko_push(git_gecko, git_wpt, "autoland", backout_rev, raise_on_error=True)

    syncs = upstream.UpstreamSync.for_bug(git_gecko, git_wpt, bug)
    assert list(syncs.keys()) == ["incomplete"]
    assert len(syncs["incomplete"]) == 1
    sync = syncs["incomplete"].pop()
    assert sync.status == "incomplete"
    assert sync.process_name.seq_id == 0
    assert len(sync.gecko_commits) == 0
    assert len(sync.upstreamed_gecko_commits) == 1
    assert len(sync.wpt_commits) == 1

    # Make some unrelated commit in the root
    upstream_gecko_commit(other_changes=test_changes, bug=1235,
                          message=b"Change other file")

    relanding_rev = upstream_gecko_commit(test_changes=test_changes, bug=bug,
                                          message=b"Reland: Change README")

    update_repositories(git_gecko, git_wpt, wait_gecko_commit=rev)
    upstream.gecko_push(git_gecko, git_wpt, "autoland", relanding_rev, raise_on_error=True)

    syncs = upstream.UpstreamSync.for_bug(git_gecko, git_wpt, bug)
    assert list(syncs.keys()) == ["open"]
    assert len(syncs["open"]) == 1
    sync = syncs["open"].pop()
    assert sync.process_name.seq_id == 0
    assert sync.bug == 1234
    assert len(sync.gecko_commits) == 1
    assert len(sync.wpt_commits) == 1
    assert len(sync.upstreamed_gecko_commits) == 1
    assert sync.status == "open"
    sync.wpt_commits[0].metadata["gecko-commit"] == relanding_rev


def test_create_partial_backout_reland(git_gecko, git_wpt, upstream_gecko_commit,
                                       upstream_gecko_backout):
    bug = 1234
    test_changes = {"README": b"Change README\n"}
    rev0 = upstream_gecko_commit(test_changes=test_changes, bug=bug,
                                 message=b"Change README")
    rev1 = upstream_gecko_commit(test_changes={"README": b"Change README again\n"}, bug=bug,
                                 message=b"Change README again")

    update_repositories(git_gecko, git_wpt)
    upstream.gecko_push(git_gecko, git_wpt, "autoland", rev1, raise_on_error=True)

    upstream_gecko_backout(rev1, bug)

    # Make some unrelated commit in the root
    upstream_gecko_commit(other_changes=test_changes, bug=1235,
                          message=b"Change other file")

    relanding_rev = upstream_gecko_commit(test_changes={"README": b"Change README once more\n"},
                                          bug=bug,
                                          message=b"Change README once more")

    update_repositories(git_gecko, git_wpt, wait_gecko_commit=relanding_rev)
    upstream.gecko_push(git_gecko, git_wpt, "autoland", relanding_rev, raise_on_error=True)

    syncs = upstream.UpstreamSync.for_bug(git_gecko, git_wpt, bug)
    assert list(syncs.keys()) == ["open"]
    assert len(syncs["open"]) == 1
    sync = syncs["open"].pop()
    assert sync.bug == 1234
    assert len(sync.gecko_commits) == 2
    assert len(sync.wpt_commits) == 2
    assert sync.status == "open"
    sync.wpt_commits[0].metadata["gecko-commit"] == rev0
    sync.wpt_commits[1].metadata["gecko-commit"] == relanding_rev


def test_land_pr(env, git_gecko, git_wpt, hg_gecko_upstream, upstream_gecko_commit):
    bug = 1234
    test_changes = {"README": b"Change README\n"}
    rev = upstream_gecko_commit(test_changes=test_changes, bug=bug,
                                message=b"Change README")

    update_repositories(git_gecko, git_wpt, wait_gecko_commit=rev)
    pushed, landed, failed = upstream.gecko_push(git_gecko, git_wpt, "autoland", rev,
                                                 raise_on_error=True)

    syncs = upstream.UpstreamSync.for_bug(git_gecko, git_wpt, bug)
    assert list(syncs.keys()) == ["open"]
    assert len(syncs["open"]) == 1
    sync = syncs["open"].pop()
    env.gh_wpt.get_pull(sync.pr).mergeable = True
    original_remote_branch = sync.remote_branch

    hg_gecko_upstream.bookmark("mozilla/central", "-r", rev)

    update_repositories(git_gecko, git_wpt, wait_gecko_commit=rev)
    pushed, landed, failed = upstream.gecko_push(git_gecko, git_wpt, "mozilla-central", rev,
                                                 raise_on_error=True)

    syncs = upstream.UpstreamSync.for_bug(git_gecko, git_wpt, bug)
    assert syncs == {"wpt-merged": {sync}}
    assert sync.gecko_landed()
    assert sync.status == "wpt-merged"
    assert original_remote_branch not in git_wpt.remotes.origin.refs
    pr = env.gh_wpt.get_pull(sync.pr)
    assert pr.merged

    with SyncLock.for_process(sync.process_name) as lock:
        with sync.as_mut(lock):
            upstream.update_pr(git_gecko,
                               git_wpt,
                               sync,
                               "closed",
                               pr["merge_commit_sha"],
                               '',
                               pr["merged_by"]["login"])

    user = env.config["web-platform-tests"]["github"]["user"]
    assert ("Upstream PR merged by %s" % user) in env.bz.output.getvalue().strip().split('\n')


def test_land_pr_after_status_change(env, git_gecko, git_wpt, hg_gecko_upstream,
                                     upstream_gecko_commit, set_pr_status):
    bug = 1234
    test_changes = {"README": b"Change README\n"}
    rev = upstream_gecko_commit(test_changes=test_changes, bug=bug,
                                message=b"Change README")

    update_repositories(git_gecko, git_wpt, wait_gecko_commit=rev)
    pushed, landed, failed = upstream.gecko_push(git_gecko, git_wpt, "autoland", rev,
                                                 raise_on_error=True)
    syncs = upstream.UpstreamSync.for_bug(git_gecko, git_wpt, bug)
    assert list(syncs.keys()) == ["open"]
    assert len(syncs["open"]) == 1
    sync = syncs["open"].pop()
    env.gh_wpt.get_pull(sync.pr).mergeable = True

    set_pr_status(sync.pr, "failure")
    with SyncLock("upstream", None) as lock:
        with sync.as_mut(lock):
            upstream.commit_check_changed(git_gecko, git_wpt, sync)

    pr = env.gh_wpt.get_pull(sync.pr)
    assert sync.last_pr_check == {"state": "failure", "sha": pr._commits[-1].sha}
    hg_gecko_upstream.bookmark("mozilla/central", "-r", rev)

    update_repositories(git_gecko, git_wpt, wait_gecko_commit=rev)
    pushed, landed, failed = upstream.gecko_push(git_gecko, git_wpt, "mozilla-central",
                                                 rev, raise_on_error=True)

    set_pr_status(sync.pr, "success")

    with SyncLock("upstream", None) as lock:
        with sync.as_mut(lock):
            upstream.commit_check_changed(git_gecko, git_wpt, sync)

    assert sync.last_pr_check == {"state": "success", "sha": pr._commits[-1].sha}
    assert sync.gecko_landed()
    assert sync.status == "wpt-merged"


def test_no_upstream_downstream(env, git_gecko, git_wpt, upstream_gecko_commit,
                                upstream_gecko_backout):

    hg_rev = upstream_gecko_commit(test_changes={"README": b"Example change"},
                                   message=b"""Example change

wpt-pr: 1
wpt-commits: 0000000000000000000000000000000000000000""")
    update_repositories(git_gecko, git_wpt, wait_gecko_commit=hg_rev)
    pushed, landed, failed = upstream.gecko_push(git_gecko, git_wpt, "autoland",
                                                 hg_rev, raise_on_error=True)
    assert not pushed
    assert not landed
    assert not failed
    backout_rev = upstream_gecko_backout(hg_rev, 1234)
    update_repositories(git_gecko, git_wpt, wait_gecko_commit=backout_rev)
    pushed, landed, failed = upstream.gecko_push(git_gecko, git_wpt, "autoland",
                                                 backout_rev, raise_on_error=True)
    assert not pushed
    assert not landed
    assert not failed


def test_upstream_existing(env, git_gecko, git_wpt, upstream_gecko_commit, upstream_wpt_commit):
    bug = 1234
    test_changes_1 = {"README": b"Change README\n"}
    upstream_gecko_commit(test_changes=test_changes_1, bug=bug,
                          message=b"Change README")
    test_changes_2 = {"OTHER": b"Add other file\n"}
    gecko_rev_2 = upstream_gecko_commit(test_changes=test_changes_2, bug=bug,
                                        message=b"Add other")

    upstream_wpt_commit(file_data=test_changes_1)
    update_repositories(git_gecko, git_wpt, wait_gecko_commit=gecko_rev_2)
    pushed, landed, failed = upstream.gecko_push(git_gecko, git_wpt, "autoland", gecko_rev_2,
                                                 raise_on_error=True)
    assert len(pushed) == 1
    assert len(landed) == 0
    assert len(failed) == 0

    syncs = upstream.UpstreamSync.for_bug(git_gecko, git_wpt, bug)
    sync = pushed.pop()
    assert syncs == {"open": {sync}}
    assert sync.bug == 1234
    assert sync.status == "open"
    assert len(sync.gecko_commits) == 2
    assert len(sync.wpt_commits) == 1
    assert sync.gecko_commits.head.sha1 == cinnabar(git_gecko).hg2git(gecko_rev_2)

    wpt_commit = sync.wpt_commits[0]

    assert wpt_commit.msg.split(b"\n")[0] == b"Add other"
    assert "OTHER" in wpt_commit.commit.tree
    assert wpt_commit.metadata == {
        'bugzilla-url': 'https://bugzilla-dev.allizom.org/show_bug.cgi?id=1234',
        'gecko-commit': gecko_rev_2
    }

    # Now make another push to the same bug and check we handle it correctly

    test_changes_3 = {"YET_ANOTHER": b"Add more files\n"}
    gecko_rev_3 = upstream_gecko_commit(test_changes=test_changes_3, bug=bug,
                                        message=b"Add more")
    update_repositories(git_gecko, git_wpt, wait_gecko_commit=gecko_rev_3)
    pushed, landed, failed = upstream.gecko_push(git_gecko, git_wpt, "autoland", gecko_rev_3,
                                                 raise_on_error=True)
    assert len(sync.gecko_commits) == 3
    assert len(sync.wpt_commits) == 2
    assert ([item.metadata.get("gecko-commit") for item in sync.wpt_commits] ==
            [gecko_rev_2, gecko_rev_3])


def test_upstream_multi(env, git_gecko, git_wpt, upstream_gecko_commit):
    bug = 1234
    test_changes = {"README": b"Add README\n"}
    rev_0 = upstream_gecko_commit(test_changes=test_changes, bug=bug,
                                  message=b"Add README")

    update_repositories(git_gecko, git_wpt, wait_gecko_commit=rev_0)
    pushed, landed, failed = upstream.gecko_push(git_gecko, git_wpt, "autoland", rev_0,
                                                 raise_on_error=True)
    assert len(pushed) == 1
    sync_0 = pushed.pop()

    test_changes = {"README1": b"Add README1\n"}
    rev_1 = upstream_gecko_commit(test_changes=test_changes, bug=bug,
                                  message=b"Add README1")

    update_repositories(git_gecko, git_wpt, wait_gecko_commit=rev_1)
    pushed, landed, failed = upstream.gecko_push(git_gecko, git_wpt, "autoland", rev_1,
                                                 raise_on_error=True)
    assert len(pushed) == 1
    assert pushed == {sync_0}
    assert len(sync_0.upstreamed_gecko_commits) == 2
    assert sync_0.process_name.seq_id == 0

    with SyncLock.for_process(sync_0.process_name) as lock:
        with sync_0.as_mut(lock):
            sync_0.finish("wpt-merged")
    assert sync_0.status == "wpt-merged"

    # Add new files each time to avoid conflicts since we don't
    # Actually do the merges
    test_changes = {"README2": b"Add README2\n"}
    rev_2 = upstream_gecko_commit(test_changes=test_changes,
                                  bug=bug,
                                  message=b"Add README2")

    update_repositories(git_gecko, git_wpt, wait_gecko_commit=rev_2)
    pushed, landed, failed = upstream.gecko_push(git_gecko, git_wpt, "autoland", rev_2,
                                                 raise_on_error=True)

    assert len(pushed) == 1
    sync_1 = pushed.pop()
    assert sync_1 != sync_0
    assert sync_1.process_name.seq_id == 1

    syncs = upstream.UpstreamSync.for_bug(git_gecko, git_wpt, bug)
    assert set(syncs.keys()) == {"open", "wpt-merged"}
    assert set(syncs["open"]) == {sync_1}
    assert set(syncs["wpt-merged"]) == {sync_0}

    with SyncLock.for_process(sync_0.process_name) as lock:
        with sync_0.as_mut(lock), sync_1.as_mut(lock):
            sync_0.finish()
            sync_1.finish()

    test_changes = {"README3": b"Add README3\n"}
    rev_3 = upstream_gecko_commit(test_changes=test_changes, bug=bug,
                                  message=b"Add README3")

    update_repositories(git_gecko, git_wpt, wait_gecko_commit=rev_3)
    pushed, landed, failed = upstream.gecko_push(git_gecko, git_wpt, "autoland", rev_3,
                                                 raise_on_error=True)
    assert len(pushed) == 1
    sync_2 = pushed.pop()
    assert sync_2.process_name not in (sync_1.process_name, sync_0.process_name)
    assert sync_2.process_name.seq_id == 2

    syncs = upstream.UpstreamSync.for_bug(git_gecko, git_wpt, bug)
    assert set(syncs.keys()) == {"open", "complete"}
    assert set(syncs["open"]) == {sync_2}
    assert set(syncs["complete"]) == {sync_0, sync_1}


def test_upstream_reprocess_commits(git_gecko, git_wpt, upstream_gecko_commit,
                                    upstream_gecko_backout):
    bug = 1234
    test_changes = {"README": b"Change README\n"}
    rev = upstream_gecko_commit(test_changes=test_changes, bug=bug,
                                message=b"Change README")

    update_repositories(git_gecko, git_wpt, wait_gecko_commit=rev)
    pushed, _, _ = upstream.gecko_push(git_gecko, git_wpt, "autoland", rev,
                                       raise_on_error=True)
    sync = pushed.pop()
    assert sync.gecko_commits[0].upstream_sync(git_gecko, git_wpt) == sync

    backout_rev = upstream_gecko_backout(rev, bug)

    update_repositories(git_gecko, git_wpt, wait_gecko_commit=backout_rev)
    upstream.gecko_push(git_gecko, git_wpt, "autoland", backout_rev, raise_on_error=True)

    sync_point = git_gecko.refs["sync/upstream/autoland"]
    sync_point.commit = (sync_commit.GeckoCommit(git_gecko, cinnabar(git_gecko).hg2git(rev))
                         .commit.parents[0])

    pushed, landed, failed = upstream.gecko_push(git_gecko, git_wpt, "autoland", backout_rev,
                                                 raise_on_error=True)
    assert len(pushed) == len(landed) == len(failed) == 0


def setup_repo(env, git_wpt, git_gecko, hg_gecko_upstream, upstream_gecko_commit,
               set_pr_status):
    bug = 1234
    changes = {"README": b"Changes to README\n"}
    upstream_gecko_commit(test_changes=changes, bug=bug,
                          message=b"Change README")

    test_changes = {"CONFIG": b"Change CONFIG\n"}
    rev = upstream_gecko_commit(test_changes=test_changes, bug=bug,
                                message=b"Change CONFIG")

    update_repositories(git_gecko, git_wpt, wait_gecko_commit=rev)
    upstream.gecko_push(git_gecko, git_wpt, "autoland", rev, raise_on_error=True)

    syncs = upstream.UpstreamSync.for_bug(git_gecko, git_wpt, bug)
    sync = syncs["open"].pop()
    set_pr_status(sync.pr, "success")
    env.gh_wpt.get_pull(sync.pr).mergeable = True

    hg_gecko_upstream.bookmark("mozilla/central", "-r", rev)

    update_repositories(git_gecko, git_wpt, wait_gecko_commit=rev)
    pushed, landed, failed = upstream.gecko_push(git_gecko,
                                                 git_wpt,
                                                 "mozilla-central",
                                                 rev,
                                                 raise_on_error=True)
    assert len(pushed) == 0
    assert len(landed) == 1
    assert len(failed) == 0

    # Push the commits to upstream wpt
    with SyncLock.for_process(sync.process_name) as upstream_sync_lock:
        with sync.as_mut(upstream_sync_lock):
            sync.push_commits()

    return list(landed)[0]


def test_pr_commits_merge(env, git_wpt, git_gecko, git_wpt_upstream,
                          hg_gecko_upstream, upstream_gecko_commit, set_pr_status):

    sync = setup_repo(env, git_wpt, git_gecko, hg_gecko_upstream, upstream_gecko_commit,
                      set_pr_status)

    # Make changes on master
    git_wpt_upstream.branches.master.checkout()
    base = git_wpt_upstream.head.commit.hexsha
    git_commit(git_wpt_upstream, b"Some other Commit", {"RANDOME_FILE": b"Changes to this\n"})

    pr = env.gh_wpt.get_pull(sync.pr)

    # Create a ref on the upstream to simulate the pr than GH would setup
    git_wpt_upstream.create_head(
        'pr/%d' % pr['number'],
        commit=git_wpt_upstream.refs['gecko/1234'].commit.hexsha
    )

    # Merge our Sync PR
    git_wpt_upstream.git.merge('gecko/1234')
    pr['merge_commit_sha'] = str(git_wpt_upstream.active_branch.commit.hexsha)
    pr['base'] = {'sha': base}
    git_wpt.remotes.origin.fetch()

    with SyncLock.for_process(sync.process_name) as upstream_sync_lock:
        with sync.as_mut(upstream_sync_lock):
            upstream.update_pr(git_gecko, git_wpt, sync, "closed", pr['merge_commit_sha'],
                               pr['base']['sha'], "test")
    pr_commits = sync.pr_commits

    for wpt_commit, pr_commit in zip(sync.wpt_commits._commits, pr_commits):
        assert wpt_commit.commit == pr_commit.commit


def test_pr_commits_squash_merge(env, git_wpt, git_gecko, git_wpt_upstream,
                                 hg_gecko_upstream, upstream_gecko_commit,
                                 set_pr_status):

    sync = setup_repo(env, git_wpt, git_gecko, hg_gecko_upstream, upstream_gecko_commit,
                      set_pr_status)

    # Make changes on master
    git_wpt_upstream.branches.master.checkout()
    base = git_wpt_upstream.head.commit.hexsha
    git_commit(git_wpt_upstream, b"Some other Commit", {"RANDOME_FILE": b"Changes to this\n"})

    pr = env.gh_wpt.get_pull(sync.pr)

    # Create a ref on the upstream to simulate the pr than GH would setup
    git_wpt_upstream.create_head(
        'pr/%d' % pr['number'],
        commit=git_wpt_upstream.refs['gecko/1234'].commit.hexsha
    )

    # Squash and Merge our Sync PR
    git_wpt_upstream.git.merge('gecko/1234', squash=True)
    git_wpt_upstream.index.commit('Merged PR #2', parent_commits=(git_wpt_upstream.head.commit,))
    pr['merge_commit_sha'] = str(git_wpt_upstream.active_branch.commit.hexsha)
    pr['base'] = {'sha': base}
    git_wpt.remotes.origin.fetch()

    with SyncLock.for_process(sync.process_name) as upstream_sync_lock:
        with sync.as_mut(upstream_sync_lock):
            upstream.update_pr(git_gecko, git_wpt, sync, "closed", pr['merge_commit_sha'],
                               pr['base']['sha'], "test")
    pr_commits = sync.pr_commits

    for wpt_commit, pr_commit in zip(sync.wpt_commits._commits, pr_commits):
        assert wpt_commit.commit == pr_commit.commit


def test_pr_commits_fast_forward(env, git_wpt, git_gecko, git_wpt_upstream,
                                 hg_gecko_upstream, upstream_gecko_commit,
                                 set_pr_status):

    sync = setup_repo(env, git_wpt, git_gecko, hg_gecko_upstream, upstream_gecko_commit,
                      set_pr_status)

    base = git_wpt_upstream.head.commit.hexsha

    pr = env.gh_wpt.get_pull(sync.pr)

    # Create a ref on the upstream to simulate the pr than GH would setup
    pr_head_commit = git_wpt_upstream.refs['gecko/1234'].commit.hexsha
    git_wpt_upstream.create_head(
        'pr/%d' % pr['number'],
        commit=pr_head_commit
    )

    # Fast forward merge our Sync PR
    git_wpt_upstream.git.merge('gecko/1234')
    git_wpt_upstream.head.commit = pr_head_commit
    pr['merge_commit_sha'] = pr_head_commit
    pr['base'] = {'sha': base}
    git_wpt.remotes.origin.fetch()

    with SyncLock.for_process(sync.process_name) as upstream_sync_lock:
        with sync.as_mut(upstream_sync_lock):
            upstream.update_pr(git_gecko, git_wpt, sync, "closed", pr['merge_commit_sha'],
                               pr['base']['sha'], "test")
    pr_commits = sync.pr_commits

    for wpt_commit, pr_commit in zip(sync.wpt_commits._commits, pr_commits):
        assert wpt_commit.commit == pr_commit.commit
