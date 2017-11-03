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

    def try_pushes(self, status="active"):
        return TryPush.load(git_gecko, pr=self.pr, status=status)

    def try_push(self, stability=False):




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


class TryPush(base.VcsRefObject):
    """A try push is represented by a an annotated tag with a path like

    sync/try/<pr_id>/<status>/<id>

    Where id is a number to indicate the Nth try push for this PR.
    """

    ref_type = "tags"
    ref_format = ["type", "pr_id", "status". "revision"]

    statuses = ("active", "complete")

    def __init__(self, git_gecko, ref):
        self.git_gecko = git_gecko
        base.VcsRefObject.__init__([git_gecko], ref)

    @property
    def status(self):
        return self.parse_ref(self.ref).get("status")

    @status.setter
    def status(self, value):
        self._set_ref_data(status=value)

    @property
    def pr(self):
        return self.parse_ref(self.ref).get("pr")

    @classmethod
    def load(cls, git_gecko, pr="*", status="open", revision="*"):
        rv = []
        for ref in cls.commit_refs(git_gecko, {"type": "try",
                                               "pr_id": pr,
                                               "status": status,
                                               "revision": id}, None).itervalues():
            rv.append(cls(git_gecko, ref))
        return id

    @classmethod
    def construct_tag(cls, pr_id, try_rev, status="active"):
        return cls.construct_ref({"type": "try", "pr_id": sync.pr,
                                  "status": status, "revision": try_rev})

    @classmethod
    def new(cls, git_gecko, commit, pr_id, try_rev, message="{}", status="active"):
        tag_name = self.construct_ref({"type": "try", "pr_id": pr_id,
                                       "status": status, "revision": try_rev})

        return base.VcsRefObject.new([git_gecko], tag_name,
                                     [git_gecko.head.commit], message=message)

    @property
    def taskgroup_id(self):
        raise NotImplementedError

    @taskgroup_id.setter
    def taskgroup_id(self, value):
        raise NotImplementedError

    @classmethod
    def create(cls, sync):
        if not tree.is_open("try"):
            logger.info("try is closed")
            # TODO make this auto-retry
            raise AbortError("Try is closed")

        git_work = sync.gecko_worktree()

        with TryCommit(git_work, message_func) as c:
            try_rev = cc.push()

        rv = cls.new(git_gecko, git_work.head.commit, sync.pr, try_rev):

        bz.comment(sync.bug, "Pushed to try. Results: "
                   "https://treeherder.mozilla.org/#/jobs?repo=try&revision={}".format(try_rev))

        return rv

    @staticmethod
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

    def get_wpt_tasks(self):
        wpt_completed, wpt_tasks = taskcluster.get_wpt_tasks(self.taskgroup_id)
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
        self.status = "complete"
        return wpt_completed, wpt_tasks

    def download_logs(self, wpt_completed):
        dest = os.path.join(self.config["root"], self.config["paths"]["try_logs"])
        return taskcluster.download_logs(self.tasks_completed, dest)


class TryCommit(object):
    def __init__(self, worktree, message_func=None, worktree_func=None):
        self.worktree = worktree
        self.message_func = message_func
        self.worktree_func = worktree_func
        self.try_rev = None

    def __enter__(self):
        if self.worktree_func is not None:
            self.worktree_func(self.worktree)
        message = self.message_func(self.worktree)
        self.worktree.index.commit(message=message)
        self.try_rev = self.worktree.head.commit.hexsha

    def __exit__(self, *args, **kwargs):
        assert self.worktree.head.commit.hexsha == self.try_rev

    def push(self):
        self.setup_files()
        logger.info("Pushing to try with message:\n{}".format(self.worktree.head.commit.message))
        status, stdout, stderr = gecko_work.git.push('try', with_extended_output=True)
        rev_match = rev_re.search(stderr)
        if not rev_match:
            logger.debug("No revision found in string:\n\n{}\n".format(stderr))
        else:
            try_rev = rev_match.group('rev')
        if status != 0:
            logger.error("Failed to push to try.")
            # TODO retry
            raise AbortError("Failed to push to try")
        assert try_rev == git_gecko.cinnabar.hg2git(git_gecko.head.commit.hexsha)
        return try_rev

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



