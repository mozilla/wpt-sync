from sync import landing, downstream, tree, trypush
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
    assert mach_command["args"] == ("try", "fuzzy", "-q", "web-platform-tests !pgo !ccov", "--artifact")


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

    try_push.download_logs = lambda: []
    landing.try_push_complete(git_gecko, git_wpt, try_push, sync)

    assert sync.status == "complete"
    new_head = git_gecko.remotes.mozilla.refs["bookmarks/mozilla/inbound"].commit
    assert "Update web-platform-tests to %s" % head_rev in new_head.message
    assert new_head.tree["testing/web-platform/tests/README"].data_stream.read() == "example_change"
    sync_point = landing.load_sync_point(git_gecko, git_wpt)
    assert sync_point["local"] == new_head.parents[0].hexsha
    assert sync_point["upstream"] == head_rev



# def upstream_commit(session, git_wpt_upstream, gh_wpt):
#     path = os.path.join(git_wpt_upstream.working_dir, "README")
#     with open(path, "w") as f:
#         f.write("Example change\n")

#     git_wpt_upstream.index.add(["README"])
#     commit = git_wpt_upstream.index.commit("Example change")
#     gh_wpt.commit_prs[commit.hexsha] = 1
#     pr, _ = model.get_or_create(session, model.PullRequest, id=1)
#     pr.title = "Example PR"

#     model.get_or_create(session,
#                         model.DownstreamSync,
#                         bug=1234,
#                         pr_id=1)
#     return commit


# def test_push_register_commit(session, git_wpt_upstream, git_wpt, gh_wpt):
#     prev_landing = model.Landing.previous(session)

#     assert prev_landing

#     commit = upstream_commit(session, git_wpt_upstream, gh_wpt)
#     push.wpt_push(session, git_wpt, gh_wpt, [commit.hexsha])

#     # This errors unless we added the commit
#     wpt_commit = session.query(model.WptCommit).filter(model.WptCommit.rev == commit.hexsha).one()
#     assert wpt_commit.pr.id == 1
#     landing = model.Landing.previous(session)

#     assert landing.head_commit.rev == prev_landing.head_commit.rev


# def test_push_land_local_simple(config, session, git_wpt_upstream, git_gecko, git_wpt, gh_wpt, bz,
#                                 upstream_wpt_commit, local_gecko_commit, mock_mach):
#     # Create an upstream commit and an equivalent local gecko commit
#     commit = upstream_wpt_commit()
#     print "Made upstream commit %s" % commit.hexsha
#     local_gecko_commit(cls=model.DownstreamSync, metadata_ready=True)

#     assert push.land_to_gecko(config, session, git_gecko, git_wpt, gh_wpt, bz)

#     landing = model.Landing.previous(session)
#     assert landing.status == model.Status.complete
#     assert landing.head_commit.rev == commit.hexsha
#     new_commit = git_gecko.commit(config["gecko"]["refs"]["mozilla-inbound"])

#     assert new_commit.message.startswith("1234 - [wpt-sync] Example change, a=testonly")
#     assert new_commit.stats.files.keys() == ["testing/web-platform/tests/README"]
#     assert len(mock_mach) == 1
#     assert mock_mach[0]["args"] == ("wpt-manifest-update",)


# def test_push_land_local_not_ready(config, session, git_wpt_upstream, git_gecko,
#                                    git_wpt, gh_wpt, bz, upstream_wpt_commit,
#                                    local_gecko_commit, mock_mach):
#     # Create an upstream commit and an equivalent local gecko commit
#     commit = upstream_wpt_commit()
#     print "Made upstream commit %s" % commit.hexsha
#     initial_landing = model.Landing.previous(session)
#     local_gecko_commit(cls=model.DownstreamSync, metadata_ready=False)

#     assert not push.land_to_gecko(config, session, git_gecko, git_wpt, gh_wpt, bz)

#     # The new commit should not be landed, and since there was nothing to land
#     # the landing object should be removed.
#     landing = model.Landing.previous(session)
#     assert landing == initial_landing
#     assert landing.head_commit.rev == initial_landing.head_commit.rev
