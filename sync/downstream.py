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
    base,
    bugcomponents,
    log,
    model,
    settings,
    taskcluster,
    tree
)
from . import commit as sync_commit

from .model import (
    DownstreamSync,
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


class WptDownstreamSync(base.WptSyncProcess):
    sync_type = "downstream"

    statues = ("open", "merged", "landed")

    def create_bug(self, title, body):
        # TODO: add PR link to the comment
        bug = self.bz.new(summary="[wpt-sync] PR {} - {}".format(self.pr, title),
                          comment=pr_body,
                          product="Testing",
                          component="web-platform-tests")
        self.bug = bug
        return self.bug

    @classmethod
    def for_pr(cls, git_gecko, git_wpt, pr_id):
        items = cls.load(git_gecko, git_wpt, pr=pr_id)
        if len(items) > 1:
            raise ValueError("Got multiple syncs for PR")
        return items[0] if items else None

    def files_changed(self):
        # TODO: Would be nice to do this from mach with a gecko worktree
        git_work = self.wpt_worktree()
        wpt = WPT(git_work.working_dir)
        return set(wpt.files_changed().split("\n"))

    def commit_manifest_update(self):
        gecko_work = self.gecko_worktree()
        mach = Mach(gecko_work.working_dir)
        mach.wpt_manifest_update()
        if gecko_work.is_dirty():
            gecko_work.index.add("testing/web-platform/meta/MANIFEST.json")
            metadata = {
                "wpt-pr": self.pr,
                "wpt-type": "manifest"
            }
            msg = sync_commit.make_commit_msg("Update wpt manifest", metadata)
            gecko_work.index.commit(message=msg)

    @property
    def metadata_commit(self):
        if self.gecko_commits[-1].metadata["wpt-type"] == "metadata":
            return self.gecko_commits[-1]

    def ensure_metadata_commit(self):
        if self.metadata_commit:
            return self.metadata_commit

        assert all(item.metadata.get("wpt-type") != "metadata" for item in self.gecko_commits)
        git_work = self.gecko_worktree()
        metadata = {
            "wpt-pr": self.pr,
            "wpt-type": "metadata"
        }
        msg = sync_commit.make_commit_msg("Bug %s - Update wpt metadata for PR %s, a=testonly" %
                                          (self.bug, self.pr), metadata)
        commit = git_work.index.commit(msg, allow_empty=True)
        return sync_commit.GeckoCommit(self.git_gecko, commit.hexsha)

    def set_bug_component(self, files_changed):
        new_component = bugcomponents.get(self.config,
                                          self.gecko_worktree(),
                                          files_changed,
                                          default=("Testing", "web-platform-tests"))
        self.bz.set_component(self.bug, *new_component)

    def renames(self):
        renames = {}
        diff_blobs = self.wpt_head.commit.diff(self.wpt_base.commit)
        for item in diff_blobs:
            if item.rename_from:
                renames[item.rename_from] = renames[item.rename_to]
        return renames

    def move_metadata(self, renames):
        if not renames:
            return

        self.ensure_metadata_commit()

        gecko_work = self.gecko_worktree()
        metadata_base = self.config["gecko"]["path"]["meta"]
        for old_path, new_path in renames.iteritems():
            old_meta_path = os.path.join(metadata_base,
                                         old_path + ".ini")
            if os.path.exists(os.path.join(gecko_work.working_dir,
                                           old_meta_path)):
                new_meta_path = os.path.join(metadata_base,
                                             new_path + ".ini")
                gecko_work.index.move(old_meta_path, new_meta_path)

        self._commit_metadata()

    def update_bug_components(self, renames):
        if not renames:
            return

        self.ensure_metadata_commit()

        gecko_work = self.gecko_worktree()
        mozbuild_path = os.path.join(gecko_work.working_dir,
                                     self.config["gecko"]["path"]["wpt"],
                                     os.pardir,
                                     "moz.build")

        tests_base = os.path.split(self.config["gecko"]["path"]["wpt"])[1]

        def tests_rel_path(path):
            return os.path.join(tests_base, path)

        mozbuild_rel_renames = {tests_rel_path(old): tests_rel_path(new)
                                for old, new in renames.iteritems()}

        if not os.path.exists(mozbuild_path):
            logger.warning("moz.build file not found, skipping update")

        new_data = bugcomponents.remove_obsolete(mozbuild_path, moves=mozbuild_rel_renames)
        with open(mozbuild_path, "w") as f:
            f.write(new_data)

        self._commit_metadata()

    def _commit_metadata(self):
        assert self.metadata_commit
        gecko_work = self.gecko_worktree()
        if gecko_work.is_dirty():
            gecko_work.git.commit(amend=True, no_edit=True)

    def update_sync(self):
        self.wpt_head = "origin/pr/%s" % self.pr
        commits_changed = self.wpt_to_gecko_commits()

        if not commits_changed:
            return

        self.commit_manifest_update()

        renames = self.renames
        self.move_metadata(renames)
        self.update_bug_components(renames)

        files_changed = self.files_changed()
        self.set_bug_component(files_changed)

        update_try_pushes(config, session, sync)

    def wpt_to_gecko_commits(self):
        """Create a patch based on wpt branch, apply it to corresponding gecko branch"""
        existing_gecko_commits = [commit for commit in self.commits
                                  if commit.metadata.get("wpt-commit")]

        retain_commits = []
        if existing_gecko_commits:
            for gecko_commit, wpt_commit in zip(existing_gecko_commits, self.wpt_commits):
                if gecko_commit.metadata["wpt-commit"] == wpt_commit.sha1:
                    retain_commits.append(gecko_commit)
                else:
                    break

            if len(retain_commits) < len(existing_gecko_commits):
                self.gecko_head = retain_commits[-1].sha1

        append_commits = self.wpt_commits[len(retain_commits):]
        if not append_commits:
            return False

        for commit in append_commits:
            metadata = {
                "wpt-pr": self.pr,
                "wpt-commit": commit.sha1
            }
            commit.move(self.gecko_worktree(),
                        dest_prefix=self.config["gecko"]["path"]["wpt"],
                        metadata=metadata)

        return True


    def try_pushes(self):
        pass


# Entry point
def new_wpt_pr(git_gecko, git_wpt, payload):
    """ Start a new downstream sync """
    git_wpt.remotes.origin.fetch()
    pr_data = payload['pull_request']
    pr_id = pr_data["number"]
    sync = WptDownstreamSync.new(git_gecko, git_wpt, pr=pr_id,
                                 gecko_start_point=WptDownstreamSync.gecko_integration_branch(),
                                 wpt_start_point="origin/pr/%s" % pr_id)

    sync.create_bug(pr_data["title"], pr_data["body"])
    # Now wait for the status to change before we take any actions


# Entry point
def status_changed(git_gecko, git_wpt, context, state):
    # TODO: seems like ignoring status that isn't our own would make more sense
    if context != "continuous-integration/travis-ci/pr":
        logger.debug("Ignoring status for context {}.".format(event["context"]))
        return

    if state == "pending":
        # We got new commits that we missed
        if not sync.wpt_head.sha1 == event["sha"]:
            git_wpt.remotes.origin.fetch()
            sync.update_commits()
    elif state == "passed":
        if len(sync.try_pushes) > 0 and not sync.try_pushes[-1].stale:
            # we've already made a try push for this version of the PR
            logger.debug("We have an existing try push for this PR")
            return
        push_to_try(config, session, git_gecko, bz, sync,
                    affected_tests=get_affected_tests(sync.wpt_worktree))
        # TODO set a status on the PR
        # TODO: check only for status of Firefox job(s)


def is_worktree_tip(repo, worktree_path, rev):
    if not worktree_path:
        return False
    branch_name = os.path.basename(worktree_path)
    if branch_name in repo.heads:
        return repo.heads[branch_name].commit.hexsha == rev
    return False


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
    if not tree.is_open("try"):
        logger.info("try is closed")
        # TODO make this auto-retry
        raise AbortError("Try is closed")

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
            raise AbortError("Failed to push to try")
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
    log_files = download_logs(config, session, wpt_completed)
    disabled = update_metadata(config, session, git_gecko, try_push.kind,
                               try_push.sync, log_files)
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
        raise AbortError(err, set_flag=["complete",
                                        ("result", model.TryResult.infra)])
    try_push.complete = True
    return wpt_completed, wpt_tasks


@step()
def download_logs(config, session, wpt_completed):
    dest = os.path.join(config["root"], config["paths"]["try_logs"])
    return taskcluster.download_logs(wpt_completed, dest)


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
        new_wpt_pr(config, session, git_gecko, git_wpt, bz, body["payload"])
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
