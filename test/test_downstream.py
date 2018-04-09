from mock import patch
from sync import downstream, handlers, load, tree, trypush


def test_new_wpt_pr(env, git_gecko, git_wpt, pull_request, set_pr_status, mock_mach, mock_wpt):
    pr = pull_request([("Test commit", {"README": "Example change\n"})],
                      "Test PR")

    mock_mach.set_data("file-info", """Testing :: web-platform-tests
  testing/web-platform/tests/README
""")

    mock_wpt.set_data("files-changed", "README\n")

    downstream.new_wpt_pr(git_gecko, git_wpt, pr)
    sync = load.get_pr_sync(git_gecko, git_wpt, pr["number"])
    env.gh_wpt.set_status(pr["number"], "success", "http://test/", "description",
                          "continuous-integration/travis-ci/pr")
    assert sync is not None
    assert sync.status == "open"
    assert len(sync.gecko_commits) == 1
    assert len(sync.wpt_commits) == 1
    assert sync.gecko_commits[0].metadata == {
        "wpt-pr": str(pr["number"]),
        "wpt-commit": pr["head"]
    }
    assert "Creating a bug in component Testing :: web-platform" in env.bz.output.getvalue()


def test_wpt_pr_status_success(git_gecko, git_wpt, pull_request, set_pr_status,
                               hg_gecko_try, mock_wpt):
    mock_wpt.set_data("tests-affected", "")

    pr = pull_request([("Test commit", {"README": "Example change\n"})],
                      "Test PR")
    downstream.new_wpt_pr(git_gecko, git_wpt, pr)
    sync = set_pr_status(pr, "success")
    try_push = sync.latest_try_push
    assert sync.last_pr_check == {"state": "success", "sha": pr.head}
    assert try_push is not None
    assert try_push.status == "open"
    assert try_push.try_rev == hg_gecko_try.log("-l1", "--template={node}")
    assert try_push.stability is False


def test_downstream_move(git_gecko, git_wpt, pull_request, set_pr_status,
                         hg_gecko_try, local_gecko_commit,
                         sample_gecko_metadata, initial_wpt_content):
    local_gecko_commit(message="Add wpt metadata", meta_changes=sample_gecko_metadata)
    pr = pull_request([("Test commit",
                        {"example/test.html": None,
                         "example/test1.html": initial_wpt_content["example/test.html"]})],
                      "Test PR")
    downstream.new_wpt_pr(git_gecko, git_wpt, pr)
    sync = set_pr_status(pr, "success")
    assert sync.gecko_commits[-1].metadata["wpt-type"] == "metadata"


def test_wpt_pr_approved(git_gecko, git_wpt, pull_request, set_pr_status,
                         hg_gecko_try, mock_wpt):
    mock_wpt.set_data("tests-affected", "")

    pr = pull_request([("Test commit", {"README": "Example change\n"})],
                      "Test PR")
    pr._approved = False
    tree.is_open = lambda x: True
    downstream.new_wpt_pr(git_gecko, git_wpt, pr)
    sync = set_pr_status(pr, "success")

    sync.data["affected-tests"] = ["example"]

    try_push = sync.latest_try_push
    assert sync.last_pr_check == {"state": "success", "sha": pr.head}
    try_push.success = lambda: True
    with patch('sync.trypush.tc.get_wpt_tasks',
               return_value=([], [])):
        downstream.try_push_complete(git_gecko, git_wpt, try_push, sync)
    assert try_push.status == "complete"
    assert sync.latest_try_push == try_push

    pr._approved = True
    handlers.handle_pull_request_review(git_gecko, git_wpt,
                                        {"action": "submitted",
                                         "review": {"state": "approved"},
                                         "pull_request": {"number": pr.number}})
    assert sync.latest_try_push != try_push
    assert sync.latest_try_push.stability


def test_revert_pr(env, git_gecko, git_wpt, git_wpt_upstream, pull_request, pull_request_fn,
                   set_pr_status, wpt_worktree):
    pr = pull_request([("Test commit", {"README": "Example change\n"})],
                      "Test PR")

    downstream.new_wpt_pr(git_gecko, git_wpt, pr)
    sync = load.get_pr_sync(git_gecko, git_wpt, pr["number"])

    commit = sync.wpt_commits[0]
    sync.wpt_commits.base = sync.data["wpt-base"] = git_wpt_upstream.head.commit.hexsha
    git_wpt_upstream.git.merge(commit.sha1)

    def revert_fn():
        git_wpt.remotes["origin"].fetch()
        wpt_work = wpt_worktree()
        wpt_work.git.revert(commit.sha1, no_edit=True)
        wpt_work.git.push("origin", "HEAD:refs/heads/revert")
        git_wpt_upstream.commit("revert")
        return "revert"

    pr_revert = pull_request_fn(revert_fn, title="Revert Test PR")

    downstream.new_wpt_pr(git_gecko, git_wpt, pr_revert)
    sync_revert = load.get_pr_sync(git_gecko, git_wpt, pr_revert["number"])

    # Refresh the instance data
    sync.data._load()
    assert sync.skip
    assert sync_revert.skip


def test_next_try_push(git_gecko, git_wpt, pull_request, set_pr_status, MockTryCls,
                       hg_gecko_try, pull_request_commit):
    pr = pull_request([("Test commit", {"README": "Example change\n"})],
                      "Test PR")
    downstream.new_wpt_pr(git_gecko, git_wpt, pr)
    sync = set_pr_status(pr, "success")

    assert sync.next_try_push() is None
    assert sync.metadata_ready is False

    # No affected tests and one try push, means we should be ready
    sync.data["affected-tests"] = []

    assert sync.requires_try
    assert not sync.requires_stability_try

    try_push = trypush.TryPush.create(sync, try_cls=MockTryCls)
    try_push.status = "complete"

    assert try_push.wpt_head == sync.wpt_commits.head.sha1

    assert sync.metadata_ready
    assert sync.next_try_push() is None

    sync.data["affected-tests"] = ["example"]

    assert sync.requires_stability_try
    assert not sync.metadata_ready

    new_try_push = sync.next_try_push(try_cls=MockTryCls)
    assert new_try_push is not None
    assert new_try_push.stability
    assert not sync.metadata_ready

    new_try_push.status = "complete"
    assert sync.metadata_ready
    assert not sync.next_try_push()

    pull_request_commit(pr.number, [("Second test commit", {"README": "Another change\n"})])
    git_wpt.remotes.origin.fetch()

    sync.update_commits()
    assert sync.latest_try_push is not None
    assert sync.latest_valid_try_push is None
    assert not sync.metadata_ready

    updated_try_push = sync.next_try_push(try_cls=MockTryCls)
    assert not updated_try_push.stability
