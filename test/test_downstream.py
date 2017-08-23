import os

from mock import Mock, patch

from sync import downstream, model, worktree


def test_new_wpt_pr(config, session, git_gecko, git_wpt, bz):
    body = {
        "payload": {
            "pull_request": {
                "number": 9,
                "title": "Test PR",
                "body": "PR body"
            },
        },
    }
    pr_id = body["payload"]["pull_request"]["number"]
    downstream.new_wpt_pr(config, session, git_gecko, git_wpt, bz, body)
    pulls = list(session.query(model.PullRequest))
    assert len(pulls) == 1
    assert pulls[0].id == pr_id
    syncs = list(session.query(model.Sync))
    assert len(syncs) == 1
    assert syncs[0].pr_id == pr_id
    assert "Summary: [wpt-sync] PR {}".format(pr_id) in bz.output.getvalue()


def test_is_worktree_tip(git_wpt_upstream):
    # using git_wpt_upstream to test with any non-bare repo with many branches
    rev = git_wpt_upstream.heads.master.commit.hexsha
    wrong_rev = git_wpt_upstream.heads["pull/0/head"].commit.hexsha
    assert downstream.is_worktree_tip(git_wpt_upstream, "some/path/to/master", rev) == True
    assert downstream.is_worktree_tip(git_wpt_upstream, None, rev) == False
    assert downstream.is_worktree_tip(git_wpt_upstream, "some/path/to/master", wrong_rev) == False


def test_status_changed(config, session, git_gecko, git_wpt, bz):
    status_event = {
        "sha": "0",
        "state": "pending",
        "context": "continuous-integration/travis-ci/pr",
    }
    sync = Mock(spec=model.Sync)
    with patch('sync.downstream.update_sync') as update_sync:
        # The first time we receive a status for a new rev, is_worktree_tip is False
        with patch('sync.downstream.is_worktree_tip', side_effect=[False, True]):
            rv = downstream.status_changed(config,
                                           session,
                                           bz,
                                           git_gecko,
                                           git_wpt,
                                           sync,
                                           status_event)
            update_sync.assert_called_once()
            # Update sync is not called again
            rv = downstream.status_changed(config,
                                           session,
                                           bz,
                                           git_gecko,
                                           git_wpt,
                                           sync,
                                           status_event)
            update_sync.assert_called_once()


def test_get_pr(config, session, git_wpt):
    sync = Mock(spec=model.Sync)
    sync.pr_id = 0
    sync.wpt_worktree = None
    wpt_work, branch_name = downstream.get_pr(config, session, git_wpt, sync)
    assert branch_name == "PR_" + str(sync.pr_id)
    assert sync.wpt_worktree == wpt_work.working_dir
    assert "remotes/origin/pull/{}/head".format(sync.pr_id) in wpt_work.git.branch(all=True)
    assert wpt_work.active_branch.name == branch_name


def test_wpt_to_gecko_commits(config, session, git_wpt, git_gecko, pr_content, bz):
    git_wpt.git.fetch("origin", "master", no_tags=True)
    git_gecko.git.fetch("mozilla")
    sync = Mock(spec=model.Sync)
    sync.wpt_worktree = None
    sync.gecko_worktree = None
    wpt_work, branch_name = worktree.ensure_worktree(
        config, session, git_wpt, "web-platform-tests", sync,
        "test", "origin/master")
    # add some commits to wpt_work
    count = 0
    for path, content in pr_content[0]:
        count += 1
        file_path = os.path.join(wpt_work.working_dir, path)
        with open(file_path, "w") as f:
            f.write(content)
        wpt_work.git.add(path)
        wpt_work.git.commit("-m", "Commit {}".format(count))
    gecko_work, gecko_branch = worktree.ensure_worktree(
        config, session, git_gecko, "gecko", sync,
        "test", config["gecko"]["refs"]["central"])
    central = gecko_work.head.commit.hexsha
    downstream.wpt_to_gecko_commits(config, wpt_work, gecko_work, sync, bz)
    new_commits = [c for c in gecko_work.iter_commits(
        "{}..".format(central), reverse=True)]
    assert len(new_commits) == len(pr_content[0])
    assert new_commits[0].message == "Commit 1\n"
    assert new_commits[1].message == "Commit 2\n"
    for c in new_commits:
        assert len(c.stats.files) == 1
        assert "testing/web-platform/tests/README" in c.stats.files
