from sync import downstream, load


def test_new_wpt_pr(env, git_gecko, git_wpt, pull_request, set_pr_status, mock_mach, mock_wpt):
    pr = pull_request([("Test commit", {"README": "Example change\n"})],
                      "Test PR")

    mock_mach.set_data("file-info", """Testing :: web-platform-tests
  testing/web-platform/tests/README
""")

    mock_wpt.set_data("files-changed", "README\n")

    downstream.new_wpt_pr(git_gecko, git_wpt, pr)
    sync = load.get_pr_sync(git_gecko, git_wpt, pr["number"])
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
    assert try_push is not None
    assert try_push.status == "open"
    assert try_push.try_rev == sync.gecko_commits[-1].canonical_rev
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

# def test_new_wpt_pr(config, session, git_gecko, git_wpt, bz):
#     body = {
#         "payload": {
#             "pull_request": {
#                 "number": 9,
#                 "title": "Test PR",
#                 "body": "PR body"
#             },
#         },
#     }
#     pr_id = body["payload"]["pull_request"]["number"]
#     downstream.new_wpt_pr(config, session, git_gecko, git_wpt, bz, body["payload"])
#     pulls = list(session.query(model.PullRequest))
#     assert len(pulls) == 1
#     assert pulls[0].id == pr_id
#     syncs = list(session.query(model.DownstreamSync))
#     assert len(syncs) == 1
#     assert syncs[0].pr.id == pr_id
#     assert "Summary: [wpt-sync] PR {}".format(pr_id) in bz.output.getvalue()


# def test_is_worktree_tip(git_wpt_upstream):
#     # using git_wpt_upstream to test with any non-bare repo with many branches
#     rev = git_wpt_upstream.heads.master.commit.hexsha
#     wrong_rev = git_wpt_upstream.heads["pull/0/head"].commit.hexsha
#     assert downstream.is_worktree_tip(git_wpt_upstream, "some/path/to/master", rev) == True
#     assert downstream.is_worktree_tip(git_wpt_upstream, None, rev) == False
#     assert downstream.is_worktree_tip(git_wpt_upstream, "some/path/to/master", wrong_rev) == False


# def test_status_changed(config, session, git_gecko, git_wpt, bz):
#     status_event = {
#         "sha": "0",
#         "state": "pending",
#         "context": "continuous-integration/travis-ci/pr",
#     }
#     pr, _ = model.get_or_create(session, model.PullRequest, id=1)
#     sync = model.DownstreamSync(pr=pr, bug=2)
#     session.add(sync)
#     with patch('sync.downstream.update_sync') as update_sync:
#         # The first time we receive a status for a new rev, is_worktree_tip is False
#         with patch('sync.downstream.is_worktree_tip', side_effect=[False, True]):
#             rv = downstream.status_changed(config,
#                                            session,
#                                            bz,
#                                            git_gecko,
#                                            git_wpt,
#                                            sync,
#                                            status_event)
#             update_sync.assert_called_once()
#             # Update sync is not called again
#             rv = downstream.status_changed(config,
#                                            session,
#                                            bz,
#                                            git_gecko,
#                                            git_wpt,
#                                            sync,
#                                            status_event)
#             update_sync.assert_called_once()


# def test_get_pr(config, session, git_wpt):
#     sync = Mock(spec=model.DownstreamSync)
#     sync.pr.id = 0
#     sync.wpt_worktree = None
#     wpt_work, branch_name = downstream.get_sync_pr(config, session, git_wpt, None, sync)
#     assert branch_name == "PR_" + str(sync.pr.id)
#     assert sync.wpt_worktree == wpt_work.working_dir
#     assert "remotes/origin/pull/{}/head".format(sync.pr.id) in wpt_work.git.branch(all=True)
#     assert wpt_work.active_branch.name == branch_name


# def test_wpt_to_gecko_commits(config, session, git_wpt, git_gecko, pr_content, bz):
#     git_wpt.git.fetch("origin", "master", no_tags=True)
#     git_gecko.git.fetch("mozilla")
#     sync = Mock(spec=model.DownstreamSync)
#     sync.wpt_worktree = None
#     sync.gecko_worktree = None
#     wpt_work, branch_name, _ = worktree.ensure_worktree(
#         config, session, git_wpt, "web-platform-tests", sync,
#         "test", "origin/master")
#     # add some commits to wpt_work
#     count = 0
#     for path, content in pr_content[0]:
#         count += 1
#         file_path = os.path.join(wpt_work.working_dir, path)
#         with open(file_path, "w") as f:
#             f.write(content)
#         wpt_work.git.add(path)
#         wpt_work.git.commit("-m", "Commit {}".format(count))
#     gecko_work, gecko_branch, _ = worktree.ensure_worktree(
#         config, session, git_gecko, "gecko", sync,
#         "test", config["gecko"]["refs"]["central"])
#     central = gecko_work.head.commit.hexsha
#     downstream.wpt_to_gecko_commits(config, session, wpt_work, gecko_work, sync, bz)
#     new_commits = [c for c in gecko_work.iter_commits(
#         "{}..".format(central), reverse=True)]
#     assert len(new_commits) == len(pr_content[0])
#     assert new_commits[0].message.splitlines()[0] == "Commit 1"
#     assert new_commits[1].message.splitlines()[0] == "Commit 2"
#     for c in new_commits:
#         assert len(c.stats.files) == 1
#         assert "testing/web-platform/tests/README" in c.stats.files


# def test_get_affected_tests():
#     output = (
#         "XMLHttpRequest/access-control-basic-allow-access-control-origin-"
#         "header-data-url.htm"
#         "\ttestharness\n"
#         "XMLHttpRequest/access-control-basic-allow-preflight-cache-timeout.htm"
#         "\ttestharness\n"
#         "css-backgrounds/background-clip-color-repaint.html"
#         "\treftest\n"
#         "webdriver/tests/contexts/maximize_window.py"
#         "\twdspec\n"
#         "webdriver/tests/contexts/resizing_and_positioning.py"
#         "\twdspec\n"
#         "webdriver/tests/contexts/positioning.py"
#         "\twdspec\n"
#     )
#     wpt = Mock()
#     wpt.tests_affected = Mock(return_value=output)
#     with patch("sync.downstream.WPT", return_value=wpt):
#         tests = downstream.get_affected_tests("some/path", "some_revision")
#         assert len(tests) == 3
#         assert len(tests["testharness"]) == 2
#         assert len(tests["wdspec"]) == 3
#         assert len(tests["reftest"]) == 1

# def test_get_affected_tests_empty():
#     wpt = Mock()
#     wpt.tests_affected = Mock(return_value=None)
#     with patch("sync.downstream.WPT", return_value=wpt):
#         assert len(downstream.get_affected_tests("some/path")) == 0
#

# def test_try_message_just_reftest():
#     base = "foo"
#     tests_affected = {
#         "reftest": ["reftestpath1"],
#     }
#     expected = (
#         "try: -b do -p win32,win64,linux64,linux -u "
#         "web-platform-tests-reftests "
#         "-t none --artifact --try-test-paths "
#         "web-platform-tests-reftests:{base}/reftestpath1".format(base=base)
#     )
#     assert downstream.try_message(tests_affected, base=base) == expected


# def test_try_message_wdspec_reftest():
#     base = "foo"
#     tests_affected = {
#         "reftest": ["reftestpath1"],
#         "wdspec": ["wdspecpath1"],
#     }
#     expected = (
#         "try: -b do -p win32,win64,linux64,linux -u "
#         "web-platform-tests-wdspec,web-platform-tests-reftests "
#         "-t none --artifact --try-test-paths "
#         "web-platform-tests-wdspec:{base}/wdspecpath1,"
#         "web-platform-tests-reftests:{base}/reftestpath1".format(base=base)
#     )
#     assert downstream.try_message(tests_affected, base=base) == expected


# def test_update_taskgroup_not_our_rev(session):
#     config = None
#     body = {
#         "origin": {"revision": "a" * 40},
#         "taskId": "c" * 22,
#     }
#     try_push = model.TryPush(rev="b" * 40, kind=model.TryKind.initial)
#     session.add(try_push)
#     downstream.update_taskgroup(config, session, body)
#     assert try_push.taskgroup_id is None


# def test_update_taskgroup(session):
#     config = None
#     rev = "a" * 40
#     task_id = "c" * 22
#     body = {
#         "origin": {"revision": rev},
#         "taskId": task_id,
#         "result": "success",
#     }
#     try_push = model.TryPush(rev=rev, kind=model.TryKind.initial)
#     session.add(try_push)
#     downstream.update_taskgroup(config, session, body)
#     assert try_push.taskgroup_id == task_id
#     assert try_push.complete == False
#     assert try_push.result is None


# def test_update_taskgroup_no_success_decision_task(session):
#     config = None
#     rev = "a" * 40
#     task_id = "c" * 22
#     body = {
#         "origin": {"revision": rev},
#         "taskId": task_id,
#     }
#     pr = model.PullRequest(id=1)
#     session.add(pr)
#     sync = model.DownstreamSync(pr=pr)
#     session.add(sync)

#     try_push = model.TryPush(rev=rev, kind=model.TryKind.initial)
#     sync.try_pushes.append(try_push)
#     session.add(try_push)
#     downstream.update_taskgroup(config, session, body)
#     assert try_push.taskgroup_id == task_id
#     assert try_push.complete == True
#     assert try_push.result == model.TryResult.infra


# def test_update_taskgroup_failed_decision_task(session):
#     config = None
#     rev = "a" * 40
#     task_id = "c" * 22
#     body = {
#         "origin": {"revision": rev},
#         "taskId": task_id,
#         "result": "anything",
#     }
#     pr = model.PullRequest(id=1)
#     session.add(pr)
#     sync = model.DownstreamSync(pr=pr)
#     session.add(sync)
#     try_push = model.TryPush(rev=rev, kind=model.TryKind.initial)
#     sync.try_pushes.append(try_push)
#     session.add(try_push)
#     downstream.update_taskgroup(config, session, body)
#     assert try_push.taskgroup_id == task_id
#     assert try_push.complete == True
#     assert try_push.result == model.TryResult.infra


# def test_taskgroup_resolved_not_ours(config, session):
#     task_id = "a" * 22
#     try_push = model.TryPush(rev="b" * 40, kind=model.TryKind.initial)
#     session.add(try_push)
#     downstream.on_taskgroup_resolved(config, session, None, None, task_id)
#     assert try_push.complete == False


# def test_taskgroup_resolved_initial_empty(config, session):
#     task_id = "a" * 22
#     try_push = model.TryPush(rev="b" * 40, kind=model.TryKind.initial, taskgroup_id=task_id)
#     session.add(try_push)
#     with patch('sync.downstream.taskcluster.get_wpt_tasks',
#                return_value=([], [])) as get_wpt_tasks:
#         assert not downstream.on_taskgroup_resolved(config, session, None, None, task_id)
#         get_wpt_tasks.assert_called_once()
#         assert try_push.complete == True
#         assert try_push.result == model.TryResult.infra


# def test_taskgroup_resolved_initial_not_all_completed(config, session):
#     task_id = "a" * 22
#     try_push = model.TryPush(rev="b" * 40, kind=model.TryKind.initial, taskgroup_id=task_id)
#     session.add(try_push)
#     complete = []
#     all_tasks = [0]
#     with patch('sync.downstream.taskcluster.get_wpt_tasks',
#                return_value=(complete, all_tasks)) as get_wpt_tasks:
#         assert not downstream.on_taskgroup_resolved(config, session, None, None, task_id)
#         get_wpt_tasks.assert_called_once()
#         assert try_push.complete == True
#         assert try_push.result == model.TryResult.infra


# @pytest.mark.parametrize("kind", [model.TryKind.stability, model.TryKind.initial])
# def test_update_metadata_no_commit(config, session, git_gecko, kind):
#     log_files = ["a", "b", "c"]
#     disabled = ""
#     pr = model.PullRequest(id=1)
#     session.add(pr)
#     sync = model.DownstreamSync(pr=pr)
#     session.add(sync)
#     with patch('sync.downstream.Mach') as mach:
#         # provide dummy metadata update results
#         wpt_update = Mock(return_value=disabled)
#         mach.return_value = Mock(wpt_update=wpt_update)
#         result = downstream.update_metadata(config, session, git_gecko, kind, sync, log_files)
#     assert sync.metadata_ready == (kind == model.TryKind.stability)
#     assert sync.metadata_commit is None
#     assert result == disabled


# @pytest.mark.parametrize("kind", [model.TryKind.stability, model.TryKind.initial])
# def test_update_metadata(config, session, git_gecko, kind):
#     log_files = ["a", "b", "c"]
#     disabled = "blah" if kind == model.TryKind.stability else ""
#     branch = "PR_1"
#     meta = config["gecko"]["path"]["meta"]
#     pr = model.PullRequest(id=1)
#     session.add(pr)
#     sync = model.DownstreamSync(pr=pr)
#     session.add(sync)
#     worktree_path = worktree.get_worktree_path(config, session, git_gecko, "gecko", branch)

#     def side_effect(*args):
#         create_file_data({"README": "Example change\n"}, worktree_path, meta)
#         return disabled

#     with patch('sync.downstream.Mach') as mach:
#         wpt_update = Mock(side_effect=side_effect)
#         mach.return_value = Mock(wpt_update=wpt_update)
#         result = downstream.update_metadata(config, session, git_gecko,
#                                             kind, sync, log_files)
#     assert sync.metadata_ready == (kind == model.TryKind.stability)
#     commit = git_gecko.commit(branch)
#     assert sync.metadata_commit == commit.hexsha
#     assert commit.stats.files.keys() == [os.path.join(meta, "README")]
#     assert commit.summary == "[wpt-sync] downstream {}: update metadata".format(branch)
#     assert result == disabled
