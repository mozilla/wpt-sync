# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

from __future__ import absolute_import, print_function

"""Functionality to support VCS syncing for WPT."""

import os
import re
import requests
import subprocess
import traceback
from collections import defaultdict

import git

from . import (
    bugcomponents,
    log,
    model,
    settings,
    taskcluster,
)

from .model import (
    DownstreamSync,
    GeckoCommit,
    PullRequest,
    Repository,
    TryPush,
    get_or_create
)
from .pipeline import pipeline, step, AbortError
from .projectutil import Mach, WPT
from .worktree import ensure_worktree

rev_re = re.compile("revision=(?P<rev>[0-9a-f]{40})")
logger = log.get_logger("downstream")


def new_wpt_pr(config, session, git_gecko, git_wpt, bz, body):
    """ Start a new downstream sync """
    pr_id = body['payload']['pull_request']['number']
    pr_data = body["payload"]["pull_request"]
    bug = bz.new(summary="[wpt-sync] PR {} - {}".format(pr_id, pr_data["title"]),
                 comment=pr_data["body"],
                 product="Testing",
                 component="web-platform-tests")

    pr = PullRequest(id=pr_id)
    session.add(pr)
    sync = DownstreamSync(pr=pr,
                          repository=Repository.by_name(session, "web-platform-tests"),
                          bug=bug)
    session.add(sync)


def get_files_changed(project_path):
    wpt = WPT(project_path)
    output = wpt.files_changed()
    return set(output.split("\n"))


def is_worktree_tip(repo, worktree_path, rev):
    if not worktree_path:
        return False
    branch_name = os.path.basename(worktree_path)
    if branch_name in repo.heads:
        return repo.heads[branch_name].commit.hexsha == rev
    return False


@pipeline
def status_changed(config, session, bz, git_gecko, git_wpt, sync, event):
    if event["context"] != "continuous-integration/travis-ci/pr":
        logger.debug("Ignoring status for context {}.".format(event["context"]))
        return
    # TODO agree what sync.status values represent
    # if sync.status == model.DownstreamSyncStatus.merged:
    #     logger.debug("Ignoring status for PR {} because it's already "
    #                  "been imported.".format(sync.pr.id))
    #     return
    if event["state"] == "pending":
        # PR is mergeable
        if not is_worktree_tip(git_wpt, sync.wpt_worktree, event["sha"]):
            update_sync(config, session, git_gecko, git_wpt, sync, bz)
    elif event["state"] == "passed":
        if len(sync.try_pushes) > 0 and not sync.try_pushes[-1].stale:
            # we've already made a try push for this version of the PR
            return
        push_to_try(config, session, git_gecko, bz, sync,
                    affected_tests=get_affected_tests(sync.wpt_worktree))
        # TODO set a status on the PR
        # TODO: check only for status of Firefox job(s)


def update_sync(config, session, git_gecko, git_wpt, sync, bz):
    wpt_work, wpt_branch = get_sync_pr(config, session, git_wpt, bz, sync)
    files_changed = get_files_changed(wpt_work.working_dir)
    gecko_work = get_sync_worktree(config, session, git_gecko, sync)
    wpt_to_gecko_commits(config, session, wpt_work, gecko_work, sync, bz)
    commit_manifest_update(config, session, gecko_work)

    renames = get_renames(config, session, wpt_work)
    move_metadata(config, session, git_gecko, gecko_work, sync, renames)
    update_bug_components(config, session, gecko_work, renames)

    set_bug_component(config, session, bz, gecko_work, sync, files_changed)
    update_try_pushes(config, session, sync)


@step()
def get_sync_pr(config, session, git_wpt, bz, sync):
    try:
        logger.info("Fetching web-platform-tests origin/master")
        git_wpt.git.fetch("origin", "master", no_tags=True)
        logger.info("Fetch done")
        wpt_work, branch_name, _ = ensure_worktree(config, session, git_wpt, "web-platform-tests",
                                                   sync, "PR_" + str(sync.pr.id), "origin/master")
        wpt_work.index.reset("origin/master", hard=True)
        wpt_work.git.fetch("origin", "pull/{}/head:heads/pull_{}".format(sync.pr.id, sync.pr.id),
                           no_tags=True)
        wpt_work.git.merge("heads/pull_{}".format(sync.pr.id))
    except git.GitCommandError as e:
        logger.error("Failed to obtain web-platform-tests PR {}:\n{}".format(sync.pr.id, e))
        bz.comment(sync.bug,
                   "Downstreaming from web-platform-tests failed because obtaining "
                   "PR {} failed:\n{}".format(sync.pr.id, e))
        # TODO set error message, set sync state to aborted?
        raise
    return wpt_work, branch_name


@step()
def get_sync_worktree(config, session, git_gecko, sync):
    logger.info("Fetching mozilla-unified")
    git_gecko.git.fetch("mozilla")
    logger.info("Fetch done")
    gecko_work, gecko_branch, _ = ensure_worktree(
        config, session, git_gecko, "gecko", sync,
        "PR_" + str(sync.pr.id), config["gecko"]["refs"]["central"])
    return gecko_work


@step()
def wpt_to_gecko_commits(config, session, wpt_work, gecko_work, sync, bz):
    """Create a patch based on wpt branch, apply it to corresponding gecko branch"""
    assert wpt_work.active_branch.name == os.path.basename(sync.wpt_worktree)
    assert gecko_work.active_branch.name == os.path.basename(sync.gecko_worktree)

    # Get a list of existing commits so we can skip these when we stop and resume
    existing_commits = list(
        gecko_work.iter_commits("%s.." % config["gecko"]["refs"]["central"], reverse=True))

    # TODO - What about PRs that aren't based on master
    for i, c in enumerate(wpt_work.iter_commits("origin/master..", reverse=True)):
        try:
            patch = wpt_work.git.show(c, pretty="email") + "\n"
            if patch.endswith("\n\n\n"):
                # skip empty patch
                continue
        except git.GitCommandError as e:
            logger.error("Failed to create patch from {}:\n{}".format(c.hexsha, e))
            bz.comment(sync.bug,
                       "Downstreaming from web-platform-tests failed because creating patch "
                       "from {} failed:\n{}".format(c.hexsha, e))
            raise AbortError(set_flag="error_apply_failed")

        if i < len(existing_commits):
            # TODO: Maybe check the commit messges match here?
            logger.info("Skipping already applied patch")
            continue

        try:
            proc = gecko_work.git.am("--directory=" + config["gecko"]["path"]["wpt"], "-",
                                     istream=subprocess.PIPE,
                                     as_process=True)
            stdout, stderr = proc.communicate(patch)
            if proc.returncode != 0:
                raise git.GitCommandError(
                    ["am", "--directory=" + config["gecko"]["path"]["wpt"], "-"],
                    proc.returncode, stderr, stdout)
        except git.GitCommandError as e:
            logger.error("Failed to import patch downstream {}\n\n{}\n\n{}".format(
                c.hexsha, patch, e))
            bz.comment(sync.bug,
                       "Downstreaming from web-platform-tests failed because applying patch "
                       "from {} failed:\n{}".format(c.hexsha, e))
            # TODO set error, sync status aborted, prevent previous steps from continuing
            raise AbortError(set_flag="error_apply_failed")


@step()
def get_renames(config, session, wpt_work):
    renames = {}
    master = wpt_work.commit("origin/master")
    diff_blobs = master.diff(wpt_work.head.commit)
    for item in diff_blobs:
        if item.rename_from:
            renames[item.rename_from] = renames[item.rename_to]
    return renames


@step()
def commit_manifest_update(config, session, gecko_work):
    gecko_work.git.reset("HEAD", hard=True)
    mach = Mach(gecko_work.working_dir)
    mach.wpt_manifest_update()
    if gecko_work.is_dirty():
        gecko_work.git.add("testing/web-platform/meta")
        gecko_work.git.commit(amend=True, no_edit=True)


def move_metadata(config, session, git_gecko, gecko_work, sync, renames):
    metadata_base = config["gecko"]["path"]["meta"]
    for old_path, new_path in renames.iteritems():
        old_meta_path = os.path.join(metadata_base,
                                     old_path + ".ini")
        if os.path.exists(os.path.join(gecko_work.working_dir,
                                       old_meta_path)):
            new_meta_path = os.path.join(metadata_base,
                                         new_path + ".ini")
            gecko_work.index.move(old_meta_path, new_meta_path)

    if renames:
        commit = gecko_work.commit(message="[wpt-sync] downstream {}: update metadata".format(
            gecko_work.active_branch.name))
        sync.metadata_commit = commit.hexsha


@step()
def update_bug_components(config, session, gecko_work, renames):
    mozbuild_path = os.path.join(gecko_work.working_dir,
                                 config["gecko"]["path"]["wpt"],
                                 os.pardir,
                                 "moz.build")

    tests_base = os.path.split(config["gecko"]["path"]["wpt"])[1]

    def tests_rel_path(path):
        return os.path.join(tests_base, path)

    mozbuild_rel_renames = {tests_rel_path(old): tests_rel_path(new)
                            for old, new in renames.iteritems()}

    if not os.path.exists(mozbuild_path):
        logger.warning("moz.build file not found, skipping update")

    new_data = bugcomponents.remove_obsolete(mozbuild_path, moves=mozbuild_rel_renames)
    with open(mozbuild_path, "w") as f:
        f.write(new_data)

    if gecko_work.is_dirty():
        gecko_work.git.add(os.path.relpath(mozbuild_path, gecko_work.working_dir))
        gecko_work.commit(amend=True, no_edit=True)


@step()
def set_bug_component(config, session, bz, gecko_work, sync, files_changed):
    # Getting the component now doesn't really work well because a PR that
    # only adds files won't have a component before we move the commits.
    # But we would really like to start a bug for logging now, so get a
    # component here and change it if needed after we have put all the new
    # commits onto the gecko tree.
    new_component = bugcomponents.get(config, gecko_work, files_changed,
                                      default=("Testing", "web-platform-tests"))
    bz.set_component(sync.bug, *new_component)


@step()
def update_try_pushes(config, session, sync):
    for push in sync.try_pushes:
        push.stale = True
        # TODO cancel previous try pushes?


def get_affected_tests(path_to_wpt, revish=None):
    # TODO? support files, harness changes -- don't want to update metadata
    tests_by_type = defaultdict(set)
    wpt = WPT(path_to_wpt)
    logger.info("Updating MANIFEST.json")
    wpt.manifest()
    args = ["--show-type", "--new"]
    if revish:
        args.append(revish)
    logger.info("Getting a list of tests affected by changes.")
    s = wpt.tests_affected(*args)
    if s is not None:
        for item in s.strip().split("\n"):
            pair = item.strip().split("\t")
            assert len(pair) == 2
            tests_by_type[pair[1]].add(pair[0])
    return tests_by_type


def try_message(tests_by_type=None, rebuild=0, base=None):
    """Build a try message

    Args:
        tests_by_type: dict of test paths grouped by wpt test type.
                       If dict is empty, no tests are affected so schedule just
                       first chunk of web-platform-tests.
                       If dict is None, schedule all wpt test jobs.
        rebuild: Number of times to repeat each test, max 20
        base: base directory for tests

    Returns:
        str: try message
    """
    test_data = {
        "test_jobs": [],
        "prefixed_paths": []
    }
    # Example: try: -b do -p win32,win64,linux64,linux,macosx64 -u
    # web-platform-tests[linux64-stylo,Ubuntu,10.10,Windows 7,Windows 8,Windows 10]
    #  -t none --artifact
    try_message = ("try: -b do -p win32,win64,linux64,linux -u {test_jobs} "
                   "-t none --artifact")
    if rebuild:
        try_message += " --rebuild {}".format(rebuild)
    test_type_suite = {
        "testharness": "web-platform-tests",
        "reftest": "web-platform-tests-reftests",
        "wdspec": "web-platform-tests-wdspec",
    }
    platform_suffix = "[linux64-stylo,Ubuntu,10.10,Windows 7,Windows 8,Windows 10]"

    def platform_filter(suite):
        return platform_suffix if suite == "web-platform-tests" else ""

    if tests_by_type is None:
        test_data["test_jobs"] = ",".join(
            [suite + platform_filter(suite) for suite in test_type_suite.itervalues()])
        return try_message.format(**test_data)

    tests_by_suite = defaultdict(list)
    if len(tests_by_type) > 0:
        try_message += " --try-test-paths {prefixed_paths}"
        for test_type in tests_by_type.iterkeys():
            suite = test_type_suite.get(test_type)
            if suite:
                tests_by_suite[test_type_suite[test_type]].extend(
                    tests_by_type[test_type])
            elif tests_by_type[test_type]:
                logger.warning(
                    """Unrecognized test type {}.
                    Will attempt to run the following tests as testharness tests:
                    {}""".format(test_type, tests_by_type[test_type]))
                tests_by_suite["web-platform-tests"].extend(
                    tests_by_type[test_type])
    else:
        # run first chunk of the wpt job if no tests are affected
        test_data["test_jobs"].append("web-platform-tests-1" + platform_suffix)

    for suite, paths in tests_by_suite.iteritems():
        if paths:
            test_data["test_jobs"].append(suite + platform_filter(suite))
        for p in paths:
            if base is not None and not p.startswith(base):
                # try server expects paths relative to m-c root dir
                p = os.path.join(base, p)
            test_data["prefixed_paths"].append(suite + ":" + p)
    test_data["test_jobs"] = ",".join(test_data["test_jobs"])
    if test_data["prefixed_paths"]:
        test_data["prefixed_paths"] = ",".join(test_data["prefixed_paths"])
    return try_message.format(**test_data)


def push_to_try(config, session, git_gecko, bz, sync, affected_tests=None,
                stability=False):
    gecko_work, message = try_commit(config, session, git_gecko, sync, affected_tests,
                                     stability)
    try_push(config, session, gecko_work, bz, sync, message, stability)


@step()
def try_commit(config, session, git_gecko, sync, affected_tests=None,
               stability=False):
    gecko_work, gecko_branch, _ = ensure_worktree(
        config, session, git_gecko, "gecko", sync,
        "PR_" + str(sync.pr.id), config["gecko"]["refs"]["central"])

    if gecko_work.head.commit.message.startswith("try:"):
        return gecko_work, gecko_work.head.commit.message

    if not stability and affected_tests is None:
        affected_tests = {}

    # TODO only push to try on mac if necessary
    message = try_message(base=config["gecko"]["path"]["wpt"],
                          tests_by_type=affected_tests,
                          rebuild=10 if stability else 0)
    gecko_work.git.commit('-m', message, allow_empty=True)
    return gecko_work, message


@step()
def try_push(config, session, gecko_work, bz, sync, message, stability=False):
    if not is_open("try"):
        logger.info("try is closed")
        # TODO make this auto-retry
        raise AbortError()

    try:
        logger.info("Pushing to try with message:\n{}".format(message))
        status, stdout, stderr = gecko_work.git.push('try', with_extended_output=True)
        rev_match = rev_re.search(stderr)
        if not rev_match:
            logger.debug("No revision found in string:\n\n{}\n".format(stderr))
        else:
            try_rev = rev_match.group('rev')
            results_url = ("https://treeherder.mozilla.org/#/"
                           "jobs?repo=try&revision=") + try_rev
            try_push = model.TryPush(
                rev=try_rev,
                kind=model.TryKind.stability if stability else model.TryKind.initial
            )
            sync.try_pushes.append(try_push)
            session.add(try_push)
        if status != 0:
            logger.error("Failed to push to try.")
            # TODO retry
            raise AbortError()
    finally:
        gecko_work.git.reset('HEAD~')
    bz.comment(sync.bug, "Pushed to try. Results: {}".format(results_url))


@pipeline
def update_taskgroup(config, session, body):
    if not (body.get("origin")
            and body["origin"].get("revision")
            and body.get("taskId")):
        logger.debug("Oh no, this payload doesn't have the format we expect!"
                     "Need 'revision' and 'taskId'. Got:\n{}\n".format(body))
        return

    try_push = TryPush.by_rev(session, body["origin"]["revision"])
    if not try_push:
        return

    update_try_push_taskgroup(config, session, try_push, body["taskId"],
                              body.get("result"), body)


@step()
def update_try_push_taskgroup(config, session, try_push, task_id, result, body):
    try_push.taskgroup_id = task_id

    if result != "success":
        # TODO manual intervention, schedule sync-retry?
        logger.debug("Try push Decision Task for PR {} "
                     "did not succeed:\n{}\n".format(try_push.sync.pr.id, body))
        try_push.complete = True
        try_push.result = model.TryResult.infra


@pipeline
def on_taskgroup_resolved(config, session, git_gecko, bz, taskgroup_id):
    try_push = TryPush.by_taskgroup(session, taskgroup_id)
    if not try_push:
        # this is not one of our try_pushes
        return

    wpt_completed, _ = get_wpt_tasks(config, session, try_push, taskgroup_id)
    get_wpt_tasks(config, session, try_push)
    log_files = download_logs(config, session, wpt_completed)
    disabled = update_metadata(config, session, git_gecko, try_push.sync,
                               try_push.kind, log_files)
    update_try_push(config, session, git_gecko, try_push, bz, log_files, disabled)


@step(state_arg="try_push")
def get_wpt_tasks(config, session, try_push, taskgroup_id):
    wpt_completed, wpt_tasks = taskcluster.get_wpt_tasks(taskgroup_id)
    err = None
    if not len(wpt_tasks):
        err = "No wpt tests found. Check decision task {}".format(taskgroup_id)
    if len(wpt_tasks) > len(wpt_completed):
        # TODO check for unscheduled versus failed versus exception
        err = "The tests didn't all run; perhaps a build failed?"

    if err:
        logger.debug(err)
        # TODO retry? manual intervention?
        raise AbortError(set_flag=["complete",
                                   ("result", model.TryResult.infra)])
    try_push.complete = True
    return wpt_completed, wpt_tasks


@step()
def download_logs(config, session, wpt_completed):
    return taskcluster.download_logs(wpt_completed, config["paths"]["try_logs"])


@step()
def update_metadata(config, session, git_gecko, kind, sync, log_files):
    meta_path = config["gecko"]["path"]["meta"]
    gecko_work, gecko_branch, _ = ensure_worktree(
        config, session, git_gecko, "gecko", sync,
        "PR_" + str(sync.pr.id), config["gecko"]["refs"]["central"])
    # git clean?
    args = log_files
    if kind == model.TryKind.stability:
        log_files.extend(["--stability", "wpt-sync Bug {}".format(sync.bug)])
    mach = Mach(gecko_work.working_dir)
    logger.debug("Updating metadata")
    # TODO filter the output to only include list of disabled tests
    disabled = mach.wpt_update(*args)
    if gecko_work.is_dirty(untracked_files=True, path=meta_path):
        gecko_work.git.add(meta_path)
        if sync.metadata_commit and gecko_work.head.commit.hexsha == sync.metadata_commit:
            gecko_work.git.commit(amend=True, no_edit=True)
        else:
            gecko_work.git.commit(message="[wpt-sync] downstream {}: update metadata".format(
                gecko_work.active_branch.name))
        sync.metadata_commit = gecko_work.head.commit.hexsha
    if kind == model.TryKind.stability:
        sync.metadata_ready = True
    return disabled


@step()
def update_try_push(config, session, git_gecko, try_push, bz, log_files, disabled):
    if try_push.kind == model.TryKind.initial:
        # TODO check if tonnes of tests are failing -- don't want to update the
        # expectation data in that case
        # TODO decide whether to do another narrow push on mac
        try_url = push_to_try(config, session, git_gecko, try_push.sync,
                              stability=True)
        bz.comment(try_push.sync.bug,
                   "Pushed to try (stability). Results: {}".format(try_url))
    elif try_push.kind == model.TryKind.stability:
        if disabled:
            logger.debug("The following tests were disabled:\n{}".format(disabled))
        # TODO notify relevant people about test expectation changes, stability
        bz.comment(try_push.sync.bug, ("The following tests were disabled "
                                       "based on stability try push: {}".format(disabled)))


def get_tree_status(project):
    try:
        r = requests.get("https://treestatus.mozilla-releng.net/trees2")
        r.raise_for_status()
        tree_status = r.json().get("result", [])
        for s in tree_status:
            if s["tree"] == project:
                return s["status"]
    except Exception as e:
        logger.warning(traceback.format_exc(e))


def is_open(project):
    return "open" == get_tree_status(project)


@settings.configure
def main(config):
    from .tasks import setup
    session, git_gecko, git_wpt, gh_wpt, bz = setup()
    try:
        temp_test_wptupdate(config, session, git_gecko, git_wpt, gh_wpt, bz)
    except Exception:
        traceback.print_exc()
        import pdb
        pdb.post_mortem()


def temp_test_wptupdate(config, session, git_gecko, git_wpt, gh_wpt, bz):
    prefix = os.path.abspath(os.path.join(config["paths"]["try_logs"], "wdspec_logs"))
    log_files = [
        prefix + "/" + "wpt_raw_debug_fail_1.log",
        prefix + "/" + "wpt_raw_opt_fail_2.log",
        prefix + "/" + "wpt_raw_debug_fail_2.log",
        prefix + "/" + "wpt_raw_opt_fail_3.log",
        prefix + "/" + "wpt_raw_debug_fail_3.log",
        prefix + "/" + "wpt_raw_opt_pass_1.log",
        prefix + "/" + "wpt_raw_debug_fail_4.log",
        prefix + "/" + "wpt_raw_opt_pass_2.log",
        prefix + "/" + "wpt_raw_opt_fail_1.log",
        prefix + "/" + "wpt_raw_opt_pass_3.log",
    ]
    failing_log_files = log_files[:3]
    model.create()
    with model.session_scope(session):
        wpt_repository, _ = model.get_or_create(session, model.Repository,
            name="web-platform-tests")
        gecko_repository, _ = model.get_or_create(session, model.Repository,
            name="gecko")
    body = {
        "payload": {
            "pull_request": {
                "number": 12,
                "title": "Test PR",
                "body": "blah blah body"
            },
        },
    }
    pr_id = body["payload"]["pull_request"]["number"]
    # new pr opened
    with model.session_scope(session):
        new_wpt_pr(config, session, git_gecko, git_wpt, bz, body)
    sync = session.query(DownstreamSync).filter(DownstreamSync.pr_id == pr_id).first()
    status_event = {
        "sha": "409018c0a562e1b47d97b53428bb7650f763720d",
        "state": "pending",
        "context": "continuous-integration/travis-ci/pr",
    }
    with model.session_scope(session):
        status_changed(config, session, bz, git_gecko, git_wpt, sync, status_event)
    status_event = {
       "sha": "409018c0a562e1b47d97b53428bb7650f763720d",
       "state": "passed",
       "context": "continuous-integration/travis-ci/pr",
    }
    with model.session_scope(session):
        status_changed(config, session, bz, git_gecko, git_wpt, sync, status_event)

    with model.session_scope(session):
        update_metadata(config, session, git_gecko, model.TryKind.initial,  sync,failing_log_files)
        #update_metadata(config, session, git_gecko, model.TryKind.stability, sync, log_files)
    model.drop()

if __name__ == '__main__':
    main()
