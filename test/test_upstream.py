import os

import git

from sync import model, upstream, pipeline

pipeline.raise_early = True

def test_create_pr_integration(config, session, hg_gecko_upstream, git_gecko, git_wpt, gh_wpt, bz):
    path = os.path.join(hg_gecko_upstream.working_tree, config["gecko"]["path"]["wpt"], "README")
    with open(path, "w") as f:
        f.write("Example change\n")

    hg_gecko_upstream.add(os.path.relpath(path, hg_gecko_upstream.working_tree))
    # This updates the bookmark for mozilla/inbound only
    hg_gecko_upstream.commit("-m", "Bug 1111 - Change README")
    upstream.push(config, session, git_gecko, git_wpt, gh_wpt, bz,
                  "mozilla-inbound",
                  hg_gecko_upstream.log("-l1", "--template={node}"))

    assert git_wpt.branches["gecko_upstream_sync_1111"]

    sync = upstrea.WptUpstreamSync("1111", git_gecko, git_wpt)

    assert len(sync.wpt_commits) == 1
    assert len(sync.gecko_commits) == 1
    assert sync.gecko_commits[0].canonical_rev == hg_gecko_upstream.log("-l1", "--template={node}").strip()

    assert commit.message.split("\n")[0] == "Change README"
    assert "README" in commit.tree
    assert "Posting to bug 1111" in bz.output.getvalue()
    assert "Created PR with id %s" % sync.pr_id in gh_wpt.output.getvalue()


def test_create_pr_landing(config, session, hg_gecko_upstream, git_gecko, git_wpt, gh_wpt, bz):
    path = os.path.join(hg_gecko_upstream.working_tree, config["gecko"]["path"]["wpt"], "README")
    with open(path, "w") as f:
        f.write("Example change\n")

    hg_gecko_upstream.add(os.path.relpath(path, hg_gecko_upstream.working_tree))
    # This updates the bookmark for mozilla/inbound only
    hg_gecko_upstream.commit("-m", "Bug 1111 - Change README")
    upstream.integration_commit(config, session, git_gecko, git_wpt, gh_wpt, bz,
                                hg_gecko_upstream.log("-l1", "--template={node}"),
                                "mozilla-inbound")


    # Generate an empty merge commit between central and inbound
    # http://hgtip.com/tips/advanced/2010-04-23-debug-command-tricks/
    hg_gecko_upstream.checkout("mozilla/central")
    hg_gecko_upstream.debugsetparents("mozilla/central", "mozilla/inbound")
    hg_gecko_upstream.revert("-a", "-r", "mozilla/inbound")
    hg_gecko_upstream.commit("-m", "Merge mozilla/inbound to mozilla/central")

    upstream.landing_commit(config, session, git_gecko, git_wpt, gh_wpt, bz,
                            hg_gecko_upstream.log("-l1", "--template={node}"))

    session.commit()

    syncs = list(session.query(model.UpstreamSync))
    assert len(syncs) == 1
    sync = syncs[0]
    assert sync.repository.name == "central"
    assert "Merged PR with id %s" % sync.pr_id in gh_wpt.output.getvalue()
    assert "Merged associated web-platform-tests PR" in bz.output.getvalue()


def test_create_pr_backout_landing(config, session, hg_gecko_upstream, git_gecko, git_wpt, gh_wpt, bz):
    path = os.path.join(hg_gecko_upstream.working_tree, config["gecko"]["path"]["wpt"], "README")
    with open(path, "w") as f:
        f.write("Example change\n")

    hg_gecko_upstream.add(os.path.relpath(path, hg_gecko_upstream.working_tree))
    # This updates the bookmark for mozilla/inbound only
    commit = hg_gecko_upstream.commit("-m", "Bug 1111 - Change README")
    upstream.integration_commit(config, session, git_gecko, git_wpt, gh_wpt, bz,
                                hg_gecko_upstream.log("-l1", "--template={node}"),
                                "mozilla-inbound")

    head_rev = hg_gecko_upstream.log("-l1", "--template={node}").strip()

    hg_gecko_upstream.backout("-r", "tip", "-m", "Backed out changeset %s (Bug 1111)" % head_rev[:12])

    upstream.integration_commit(config, session, git_gecko, git_wpt, gh_wpt, bz,
                                hg_gecko_upstream.log("-l1", "--template={node}"),
                                "mozilla-inbound")

    session.commit()

    syncs = list(session.query(model.UpstreamSync))
    assert len(syncs) == 1
    sync = syncs[0]
    assert len(sync.gecko_commits) == 0
