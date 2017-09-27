# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

from __future__ import absolute_import, print_function

"""Functionality to support VCS syncing for WPT."""

import os
import re
import subprocess
import traceback
from collections import defaultdict

import git

from . import (
    bugcomponents,
    log,
    model,
    settings,
)

from .model import (
    DownstreamSync,
    GeckoCommit,
    PullRequest,
    Repository,
    TryPush,
    get_or_create
)
from .projectutil import Mach, WPT
from .taskcluster import (
    download_logs, filter_suite, filter_completed, get_tasks_in_group
)
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


def commit_manifest_update(gecko_work):
    gecko_work.git.reset("HEAD", hard=True)
    mach = Mach(gecko_work.working_dir)
    mach.wpt_manifest_update()
    if gecko_work.is_dirty:
        gecko_work.git.add("testing/web-platform/meta")
        gecko_work.git.commit(amend=True, no_edit=True)


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


def status_changed(config, session, bz, git_gecko, git_wpt, sync, event):
    if event["context"] != "continuous-integration/travis-ci/pr":
        logger.debug("Ignoring status for context {}.".format(event["context"]))
        return
    # TODO agree what sync.status values represent
    # if sync.status == model.DownstreamSyncStatus.merged:
    #     logger.debug("Ignoring status for PR {} because it's already "
    #                  "been imported.".format(sync.pr_id))
    #     return
    if event["state"] == "pending":
        # PR is mergeable
        if not is_worktree_tip(git_wpt, sync.wpt_worktree, event["sha"]):
            update_sync(config, session, git_gecko, git_wpt, sync, bz)
    elif event["state"] == "passed":
        if len(sync.try_pushes) > 0 and not sync.try_pushes[-1].stale:
            # we've already made a try push for this version of the PR
            return
        try_url = push_to_try(config, session, git_gecko, sync,
                              affected_tests=get_affected_tests(sync.wpt_worktree))
        bz.comment(sync.bug,
                   "Pushed to try. Results: {}".format(try_url))
        # TODO set a status on the PR
        # TODO: check only for status of Firefox job(s)


def update_sync(config, session, git_gecko, git_wpt, sync, bz):
    try:
        wpt_work, wpt_branch = get_pr(config, session, git_wpt, sync)
    except git.GitCommandError as e:
        logger.error("Failed to obtain web-platform-tests PR {}:\n{}".format(sync.pr_id, e))
        bz.comment(sync.bug,
                   "Downstreaming from web-platform-tests failed because obtaining "
                   "PR {} failed:\n{}".format(sync.pr_id, e))
        return False

    files_changed = get_files_changed(wpt_work.working_dir)

    logger.info("Fetching mozilla-unified")
    git_gecko.git.fetch("mozilla")
    logger.info("Fetch done")
    gecko_work, gecko_branch, _ = ensure_worktree(
        config, session, git_gecko, "gecko", sync,
        "PR_" + str(sync.pr_id), config["gecko"]["refs"]["central"])
    gecko_work.index.reset(config["gecko"]["refs"]["central"], hard=True)
    success = wpt_to_gecko_commits(config, wpt_work, gecko_work, sync, bz)
    if not success:
        return
    commit_manifest_update(gecko_work)

    renames = get_renames(config, session, wpt_work)
    move_metadata(config, session, git_gecko, gecko_work, sync, renames)
    update_bug_components(config, session, gecko_work, renames)

    # Getting the component now doesn't really work well because a PR that
    # only adds files won't have a component before we move the commits.
    # But we would really like to start a bug for logging now, so get a
    # component here and change it if needed after we have put all the new
    # commits onto the gecko tree.
    new_component = bugcomponents.get(config, gecko_work, files_changed,
                                      default=("Testing", "web-platform-tests"))
    bz.set_component(sync.bug, *new_component)
    #for push in sync.try_pushes:
    #    push.stale = True
    #    # TODO cancel previous try pushes?


def get_pr(config, session, git_wpt, sync):
    logger.info("Fetching web-platform-tests origin/master")
    git_wpt.git.fetch("origin", "master", no_tags=True)
    logger.info("Fetch done")
    wpt_work, branch_name, _ = ensure_worktree(config, session, git_wpt, "web-platform-tests",
                                               sync, "PR_" + str(sync.pr_id), "origin/master")
    wpt_work.index.reset("origin/master", hard=True)
    wpt_work.git.fetch("origin", "pull/{}/head:heads/pull_{}".format(sync.pr_id, sync.pr_id),
                       no_tags=True)
    wpt_work.git.merge("heads/pull_{}".format(sync.pr_id))
    return wpt_work, branch_name


def wpt_to_gecko_commits(config, wpt_work, gecko_work, sync, bz):
    """Create a patch based on wpt branch, apply it to corresponding gecko branch"""
    assert wpt_work.active_branch.name == os.path.basename(sync.wpt_worktree)
    assert gecko_work.active_branch.name == os.path.basename(sync.gecko_worktree)

    # TODO - What about PRs that aren't based on master
    for c in wpt_work.iter_commits("origin/master..", reverse=True):
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
            return False

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
            return False
    return True


def get_renames(config, session, wpt_work):
    renames = {}
    master = wpt_work.commit("origin/master")
    diff_blobs = master.diff(wpt_work.head.commit)
    for item in diff_blobs:
        if item.rename_from:
            renames[item.rename_from] = renames[item.rename_to]
    return renames


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
        sync.metadata_commit = get_or_create(session, GeckoCommit,
                                             rev=git_gecko.cinnabar.git2hg(commit.hexsha))


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

    new_data = bugcomponents.update(mozbuild_path, moves=mozbuild_rel_renames)
    with open(mozbuild_path, "w") as f:
        f.write(new_data)

    if gecko_work.is_dirty:
        gecko_work.git.add(os.path.relpath(mozbuild_path, gecko_work.working_dir))
        gecko_work.commit(amend=True, no_edit=True)


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


def push_to_try(config, session, git_gecko, sync, affected_tests=None,
                stability=False):
    # TODO check if try is closed, retry
    gecko_work, gecko_branch, _ = ensure_worktree(
        config, session, git_gecko, "gecko", sync,
        "PR_" + str(sync.pr_id), config["gecko"]["refs"]["central"])
    results_url = None
    if not stability and affected_tests is None:
        affected_tests = {}
    # TODO only push to try on mac if necessary
    message = try_message(base=config["gecko"]["path"]["wpt"],
                          tests_by_type=affected_tests,
                          rebuild=10 if stability else 0)
    gecko_work.git.commit('-m', message, allow_empty=True)
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
            logger.debug("Failed to push to try.")
            # TODO retry? eventually flag for manual fix?
    finally:
        gecko_work.git.reset('HEAD~')
    return results_url


def update_taskgroup(config, session, body):
    if not (body.get("origin")
            and body["origin"].get("revision")
            and body.get("taskId")):
        logger.debug("Oh no, this payload doesn't have the format we expect!"
                     "Need 'revision' and 'taskId'. Got:\n{}\n".format(body))
        return

    try_push = session.query(TryPush).filter(
        TryPush.rev == body["origin"]["revision"]).first()
    if not try_push:
        # this is not one of our decision tasks
        return

    try_push.taskgroup_id = body["taskId"]

    if body.get("result") != "success":
        # TODO manual intervention, retry?
        logger.debug("Try push Decision Task for PR {} "
                     "did not succeed:\n{}\n".format(try_push.sync.pr_id, body))
        try_push.complete = True
        try_push.result = model.TryResult.infra


def on_taskgroup_resolved(config, session, git_gecko, bz, taskgroup_id):
    try_push = session.query(TryPush).filter(
        TryPush.taskgroup_id == taskgroup_id).first()
    if not try_push:
        # this is not one of our try_pushes
        return
    try_push.complete = True
    tasks = get_tasks_in_group(try_push.taskgroup_id)
    wpt_tasks = filter_suite(tasks, "web-platform-tests")
    wpt_completed = filter_completed(wpt_tasks)
    if len(wpt_tasks) != len(wpt_completed):
        # TODO check for unscheduled versus failed versus exception
        logger.debug("The tests didn't all run; perhaps a build failed?")
        # TODO retry? manual intervention?
        return
    log_files = download_logs(wpt_completed, config["paths"]["try_logs"])
    if try_push.kind == model.TryKind.initial:
        update_metadata(config, session, git_gecko, try_push.sync, log_files)
        # TODO check if tonnes of tests are failing -- don't want to update the
        # expectation data in that case
        # TODO decide whether to do another narrow push on mac
        try_url = push_to_try(config, session, git_gecko, try_push.sync,
                              stability=True)
        bz.comment(try_push.sync.bug,
                   "Pushed to try (stability). Results: {}".format(try_url))
    elif try_push.kind == model.TryKind.stability:
        # TODO disable obviously unstable tests after stability push
        # TODO notify relevant people about test expectation changes, stability
        try_push.sync.metadata_ready = True
        return


def update_metadata(config, session, git_gecko, sync, log_files):
    gecko_work, gecko_branch, _ = ensure_worktree(
        config, session, git_gecko, "gecko", sync,
        "PR_" + str(sync.pr_id), config["gecko"]["refs"]["central"])
    # git clean?
    mach = Mach(gecko_work.working_dir)
    mach.wpt_update(*log_files)
    if gecko_work.is_dirty:
        gecko_work.git.add("testing/web-platform/meta")
        if sync.metadata_commit and gecko_work.head.commit.hexsha == sync.metadata_commit.rev:
            gecko_work.git.commit(amend=True)
        else:
            gecko_work.git.commit(message="[wpt-sync] downstream {}: update metadata".format(
                gecko_work.active_branch.name))
            sync.metadata_commit = get_or_create(session, GeckoCommit,
                                                 rev=git_gecko.cinnabar.git2hg(
                                                     gecko_work.head.commit.hexsha))


@settings.configure
def main(config):
    from . import handlers
    session, git_gecko, git_wpt, gh_wpt, bz = handlers.setup(config)
    model.drop()
    try:
        model.create()
        wpt_repository, _ = model.get_or_create(session, model.Repository,
            name="web-platform-tests")
        gecko_repository, _ = model.get_or_create(session, model.Repository,
            name="gecko")
        body = {
            "payload": {
                "pull_request": {
                    "number": 11,
                    "title": "Test PR",
                    "body": "blah blah body"
                },
            },
        }
        pr_id = body["payload"]["pull_request"]["number"]
        # new pr opened
        new_wpt_pr(config, session, git_gecko, git_wpt, bz, body)
        sync = session.query(DownstreamSync).filter(DownstreamSync.pr_id == pr_id).first()
        status_event = {
            "sha": "409018c0a562e1b47d97b53428bb7650f763720d",
            "state": "pending",
            "context": "continuous-integration/travis-ci/pr",
        }
        status_changed(config, session, bz, git_gecko, git_wpt, sync, status_event)
        status_event = {
           "sha": "409018c0a562e1b47d97b53428bb7650f763720d",
           "state": "passed",
           "context": "continuous-integration/travis-ci/pr",
        }
        status_changed(config, session, bz, git_gecko, git_wpt, sync, status_event)
    except Exception:
        traceback.print_exc()
        import pdb
        pdb.post_mortem()


if __name__ == '__main__':
    main()
