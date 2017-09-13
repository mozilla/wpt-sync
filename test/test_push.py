import os
import sys

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
                        model.DownstreamSync,
                        bug=1234,
                        pr_id=1)
    return commit


def test_push_register_commit(session, git_wpt_upstream, git_wpt, gh_wpt):
    prev_landing = model.Landing.previous(session)

    assert prev_landing

    commit = upstream_commit(session, git_wpt_upstream, gh_wpt)
    push.wpt_push(session, git_wpt, gh_wpt, [commit.hexsha])

    # This errors unless we added the commit
    wpt_commit = session.query(model.WptCommit).filter(model.WptCommit.rev == commit.hexsha).one()
    assert wpt_commit.pr.id == 1
    landing = model.Landing.previous(session)

    assert landing.head_commit.rev == prev_landing.head_commit.rev


def test_push_land_local_simple(config, session, git_wpt_upstream, git_gecko, git_wpt, gh_wpt, bz,
                                upstream_wpt_commit, local_gecko_commit, mock_mach):
    # Create an upstream commit and an equivalent local gecko commit
    commit = upstream_wpt_commit()
    print "Made upstream commit %s" % commit.hexsha
    local_gecko_commit(cls=model.DownstreamSync, metadata_ready=True)

    success = push.land_to_gecko(config, session, git_gecko, git_wpt, gh_wpt, bz, None)
    assert success

    landing = model.Landing.previous(session)
    assert landing.status == model.LandingStatus.complete
    assert landing.head_commit.rev == commit.hexsha
    new_commit = git_gecko.commit(config["gecko"]["refs"]["mozilla-inbound"])

    assert new_commit.message.startswith("1234 - [wpt-sync] Example change, a=testonly")
    assert new_commit.stats.files.keys() == ["testing/web-platform/tests/README"]
    assert len(mock_mach) == 1
    assert mock_mach[0]["args"] == ("wpt-manifest-update",)


def test_push_land_local_not_ready(config, session, git_wpt_upstream, git_gecko,
                                   git_wpt, gh_wpt, bz, upstream_wpt_commit,
                                   local_gecko_commit, mock_mach):
    # Create an upstream commit and an equivalent local gecko commit
    commit = upstream_wpt_commit()
    print "Made upstream commit %s" % commit.hexsha
    initial_landing = model.Landing.previous(session)
    local_gecko_commit(cls=model.DownstreamSync, metadata_ready=False)

    success = push.land_to_gecko(config, session, git_gecko, git_wpt, gh_wpt, bz, None)
    assert not success

    # The new commit should not be landed, and since there was nothing to land
    # the landing object should be removed.
    landing = model.Landing.previous(session)
    assert landing == initial_landing
    assert landing.head_commit.rev == initial_landing.head_commit.rev
