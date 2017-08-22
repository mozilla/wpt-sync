import os

from sync import model, push


def upstream_commit(session, git_wpt_upstream, gh_wpt):
    path = os.path.join(git_wpt_upstream.working_dir, "README")
    with open(path, "w") as f:
        f.write("Example change\n")

    git_wpt_upstream.index.add(["README"])
    commit = git_wpt_upstream.index.commit("Example change")
    gh_wpt.commit_prs[commit.hexsha] = 1
    pr, _ = model.get_or_create(session, model.PullRequest, id=1)
    pr.title = "Example PR"

    model.get_or_create(session,
                        model.Sync,
                        bug=1234,
                        pr_id=1,
                        direction=model.SyncDirection.downstream)
    return commit


def test_push_register_commit(session, git_wpt_upstream, git_wpt, gh_wpt):
    landing, _ = model.get_or_create(session, model.Landing)
    orig_last_landed = landing.last_landed_commit

    commit = upstream_commit(session, git_wpt_upstream, gh_wpt)
    push.wpt_push(session, git_wpt, gh_wpt)

    # This errors unless we added the commit
    wpt_commit = session.query(model.WptCommit).filter(model.WptCommit.rev == commit.hexsha).one()
    assert wpt_commit.pr.id == 1
    landing, _ = model.get_or_create(session, model.Landing)

    assert landing.last_push_commit == commit.hexsha
    assert landing.last_landed_commit == orig_last_landed


def test_push_land_local(config, session, git_wpt_upstream, git_gecko, git_wpt, gh_wpt, bz,
                         upstream_wpt_commit, local_gecko_commit, mock_mach):
    landing, _ = model.get_or_create(session, model.Landing)
    orig_push = landing.last_push_commit

    # Create an upstream commit and an equivalent local gecko commit
    commit = upstream_wpt_commit()
    local_gecko_commit(sync_direction=model.SyncDirection.downstream)

    push.wpt_push(session, git_wpt, gh_wpt)
    push.land_to_gecko(config, session, git_gecko, git_wpt, gh_wpt, bz)

    landing, _ = model.get_or_create(session, model.Landing)
    assert landing.last_landed_commit == commit.hexsha
    new_commit = git_gecko.commit(config["gecko"]["refs"]["mozilla-inbound"])

    assert new_commit.message.startswith("1234 - [wpt-sync] Example change, a=testonly")
    assert new_commit.stats.files.keys() == ["testing/web-platform/tests/README"]
    assert len(mock_mach) == 1
    assert mock_mach[0]["args"] == ("wpt-manifest-update",)
