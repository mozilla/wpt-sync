import os

import git

from sync import model, upstream


def test_create_pr_integration(config, session, hg_gecko_upstream, git_gecko, git_wpt, gh_wpt, bz):
    path = os.path.join(hg_gecko_upstream.working_tree, config["gecko"]["path"]["wpt"], "README")
    with open(path, "w") as f:
        f.write("Example change\n")

    hg_gecko_upstream.add(os.path.relpath(path, hg_gecko_upstream.working_tree))
    # This updates the bookmark for mozilla/inbound only
    hg_gecko_upstream.commit("-m", "Bug 1111 - Change README")
    upstream.integration_commit(config, session, git_gecko, git_wpt, gh_wpt, bz, "mozilla-inbound")

    syncs = list(session.query(model.Sync))
    assert len(syncs) == 1

    sync = syncs[0]
    assert sync.direction == model.SyncDirection.upstream
    assert sync.repository.name == "mozilla-inbound"
    assert sync.bug == 1111
    assert len(sync.commits) == 1
    assert sync.commits[0].rev == hg_gecko_upstream.log("-l1", "--template={node}").strip()
    worktree_path = os.path.join(config["root"], config["paths"]["worktrees"], sync.wpt_worktree)
    assert os.path.exists(worktree_path)
    git_work = git.Repo(worktree_path)
    commit = git_work.iter_commits().next()

    assert commit.message.split("\n")[0] == "Change README"
    assert "README" in commit.tree
    assert "Posting to bug 1111" in bz.output.getvalue()
    assert "Created PR with id %s" % sync.pr in gh_wpt.output.getvalue()


def test_create_pr_landing(config, session, hg_gecko_upstream, git_gecko, git_wpt, gh_wpt, bz):
    path = os.path.join(hg_gecko_upstream.working_tree, config["gecko"]["path"]["wpt"], "README")
    with open(path, "w") as f:
        f.write("Example change\n")

    hg_gecko_upstream.add(os.path.relpath(path, hg_gecko_upstream.working_tree))
    # This updates the bookmark for mozilla/inbound only
    hg_gecko_upstream.commit("-m", "Bug 1111 - Change README")
    upstream.integration_commit(config, session, git_gecko, git_wpt, gh_wpt, bz, "mozilla-inbound")


    # Generate an empty merge commit between central and inbound
    # http://hgtip.com/tips/advanced/2010-04-23-debug-command-tricks/
    hg_gecko_upstream.checkout("mozilla/central")
    hg_gecko_upstream.debugsetparents("mozilla/central", "mozilla/inbound")
    hg_gecko_upstream.revert("-a", "-r", "mozilla/inbound")
    hg_gecko_upstream.commit("-m", "Merge mozilla/inbound to mozilla/central")

    upstream.landing_commit(config, session, git_gecko, git_wpt, gh_wpt, bz)

    syncs = list(session.query(model.Sync))
    assert len(syncs) == 1
    sync = syncs[0]
    assert sync.repository.name == "central"
    assert "Merged PR with id %s" % sync.pr in gh_wpt.output.getvalue()
    assert "Merged associated web-platform-tests PR" in bz.output.getvalue()


def test_create_pr_backout_landing(config, session, hg_gecko_upstream, git_gecko, git_wpt, gh_wpt, bz):
    path = os.path.join(hg_gecko_upstream.working_tree, config["gecko"]["path"]["wpt"], "README")
    with open(path, "w") as f:
        f.write("Example change\n")

    hg_gecko_upstream.add(os.path.relpath(path, hg_gecko_upstream.working_tree))
    # This updates the bookmark for mozilla/inbound only
    hg_gecko_upstream.commit("-m", "Bug 1111 - Change README")
    upstream.integration_commit(config, session, git_gecko, git_wpt, gh_wpt, bz, "mozilla-inbound")

    head_rev = hg_gecko_upstream.log("-l1", "--template={node}").strip()

    hg_gecko_upstream.backout("-r", "tip", "-m", "Backed out changeset %s (Bug 1111)" % head_rev[:12])

    upstream.integration_commit(config, session, git_gecko, git_wpt, gh_wpt, bz, "mozilla-inbound")

    syncs = list(session.query(model.Sync))
    assert len(syncs) == 1
    sync = syncs[0]
    assert len(sync.commits) == 0
