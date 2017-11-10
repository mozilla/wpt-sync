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

from .env import Environment
from .pipeline import AbortError
from .projectutil import Mach, WPT
from .worktree import ensure_worktree

rev_re = re.compile("revision=(?P<rev>[0-9a-f]{40})")
logger = log.get_logger("downstream")
env = Environment()


class WptDownstreamSync(base.WptSyncProcess):
    sync_type = "downstream"
    obj_id = "pr"
    statues = ("open", "ready", "merged")

    @classmethod
    def new(cls, git_gecko, git_wpt, wpt_head, pr_id, pr_title, pr_body):
        # TODO: add PR link to the comment
        sync = WptDownstreamSync.new(git_gecko,
                                     git_wpt,
                                     pr=pr_id,
                                     gecko_base=WptDownstreamSync.gecko_integration_branch,
                                     gecko_head=WptDownstreamSync.gecko_integration_branch,
                                     wpt_base=pr_data["base"]["ref"],
                                     wpt_head="origin/pr/%s" % pr_id)
        bug = self.bz.new(summary="[wpt-sync] PR %s - %s" % (self.pr, pr_title),
                          comment=pr_body,
                          product="Testing",
                          component="web-platform-tests")
        sync.bug = bug
        return sync

    @classmethod
    def for_pr(cls, git_gecko, git_wpt, pr_id):
        items = cls.load_all(git_gecko, git_wpt, obj_id=pr_id)
        if len(items) > 1:
            raise ValueError("Got multiple syncs for PR")
        return items[0] if items else None

    @property
    def pr_head(self):
        return sync_commit.WptCommit(self.git_wpt, "origin/pr/%s" % self.pr_id)

    @property
    def wpt(self):
        git_work = self.wpt_worktree.get()
        return WPT(os.path.join(git_work.working_dir))

    def update_wpt_head(self):
        if self.wpt_commits.head.sha1 != self.pr_head.sha1:
            self.wpt_commits.head = self.pr_head

    def files_changed(self):
        # TODO: Would be nice to do this from mach with a gecko worktree
        return set(self.wpt.files_changed().split("\n"))

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
        diff_blobs = self.wpt_commits.head.commit.diff(self.wpt_commits.base.commit)
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

    def update_commits(self):
        self.update_wpt_head()
        commits_changed = self.wpt_to_gecko_commits()

        if not commits_changed:
            return

        self.commit_manifest_update()

        renames = self.renames
        self.move_metadata(renames)
        self.update_bug_components(renames)

        files_changed = self.files_changed()
        self.set_bug_component(files_changed)

    def wpt_to_gecko_commits(self):
        """Create a patch based on wpt branch, apply it to corresponding gecko branch"""
        existing_gecko_commits = [commit for commit in self.gecko_commits
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

    def affected_tests(self, revish=None):
        # TODO? support files, harness changes -- don't want to update metadata
        tests_by_type = defaultdict(set)
        logger.info("Updating MANIFEST.json")
        self.wpt.manifest()
        args = ["--show-type", "--new"]
        if revish:
            args.append(revish)
        logger.info("Getting a list of tests affected by changes.")
        s = self.wpt.tests_affected(*args)
        if s is not None:
            for item in s.strip().split("\n"):
                pair = item.strip().split("\t")
                assert len(pair) == 2
                tests_by_type[pair[1]].add(pair[0])
        return tests_by_type

    def try_push_for_commit(self, sha):
        try_pushes = TryPush.load_all(self.git_gecko, self.pr, status="*")
        for try_push in try_pushes:
            if try_push.wpt_sha == sha:
                return try_push
        return None

    @property
    def latest_try_push(self):
        try_pushes = TryPush.load_all(self.git_gecko, self.pr, status="*")
        if try_pushes:
            return try_pushes[-1]

    def update_metadata(log_files, stability):
        meta_path = env.config["gecko"]["path"]["meta"]
        args = log_files
        if stability:
            args.extend(["--stability", "wpt-sync Bug %s" % self.bug])

        mach = Mach(gecko_work.working_dir)
        logger.debug("Updating metadata")
        # TODO filter the output to only include list of disabled tests
        disabled = mach.wpt_update(*args)

        if gecko_work.is_dirty(untracked_files=True, path=meta_path):
            gecko_work.git.add(meta_path)
            self.ensure_metadata_commit()

        if stability:
            self.status = "ready"
        return disabled


class TryPush(base.TagRefObject):
    """A try push is represented by a an annotated tag with a path like

    sync/try/<pr_id>/<status>/<id>

    Where id is a number to indicate the Nth try push for this PR.
    """
    statuses = ("open", "complete", "infra-fail")

    @property
    def pr(self):
        return self._process_name.obj_id

    @classmethod
    def new(cls, git_gecko, commit, pr_id):
        existing = self.load_all(git_gecko, cls.sync_type, status="*",
                                 pr_id, seq_id="*")
        if not existing:
            seq_id = "0"
        else:
            seq_id = str(int(existing[-1].seq_id) + 1)
        process_name = base.ProcessName(cls.sync_type, status, obj_id, seq_id)
        base.TagRefObject.create(git_gecko, str(process_name), commit)
        return cls(git_gecko, str(process_name), wpt_sync.GeckoCommit)

    @classmethod
    def load_all(cls, git_gecko, pr_id, status="open", seq_id="*"):
        return base.TagRefObject.load_all(git_gecko, status=status, obj_id=pr_id,
                                          seq_id=seq_id)

    @classmethod
    def for_commit(cls, git_gecko, sha1, status="open", seq_id="*"):
        pushes = cls.load_all(cls, git_gecko, "*", status=status)
        for push in reversed(pushes):
            if push.try_rev == sha1:
                return push

    @classmethod
    def for_taskgroup(cls, taskgroup_id, status="open", seq_id="*"):
        pushes = cls.load_all(cls, git_gecko, "*", status=status)
        for push in reversed(pushes):
            if push.taskgroup_id == taskgroup_id:
                return push

    @property
    def try_rev(self):
        return self.data.get("try-rev")

    @property
    def wpt_sha(self):
        return self.data.get("wpt-sha")

    @property
    def taskgroup_id(self):
        return self.data.get("taskgroup-id")

    @taskgroup_id.setter
    def taskgroup_id(self, value):
        self.data["taskgroup-id"] = value

    @classmethod
    def create(cls, git_gecko, sync, stability=False, wpt_sha=None):
        if not tree.is_open("try"):
            logger.info("try is closed")
            # TODO make this auto-retry
            raise AbortError("Try is closed")

        git_work = sync.gecko_worktree.get()

        rebuild_count = 0 if not stability else 10
        with TrySyntaxCommit(git_work, sync.affected_tests, rebuild_count) as c:
            try_rev = c.push()

        rv = cls.new(git_gecko, git_work.head.commit, sync.pr)
        if wpt_sha is not None:
            rv.data["wpt-sha"] = wpt_sha
        rv.data["try-rev"] = try_rev
        rv.data["stability"] = staility

        env.bz.comment(sync.bug,
                       "Pushed to try%s. Results: "
                       "https://treeherder.mozilla.org/#/jobs?repo=try&revision=%s" %
                       (" (stability)" if stability else "", try_rev))

        return rv

    def wpt_tasks(self):
        wpt_completed, wpt_tasks = taskcluster.get_wpt_tasks(self.taskgroup_id)
        err = None
        if not len(wpt_tasks):
            err = "No wpt tests found. Check decision task {}".format(self.taskgroup_id)
        if len(wpt_tasks) > len(wpt_completed):
            # TODO check for unscheduled versus failed versus exception
            err = "The tests didn't all run; perhaps a build failed?"

        if err:
            logger.debug(err)
            # TODO retry? manual intervention?
            self.status = "infra-fail"
            raise AbortError(err)
        self.status = "complete"
        return wpt_completed, wpt_tasks

    def download_logs(self):
        wpt_completed, wpt_tasks = self.wpt_tasks()
        dest = os.path.join(env.config["root"], env.config["paths"]["try_logs"])
        return taskcluster.download_logs(wpt_completed, dest)


class TryCommit(object):
    def __init__(self, worktree, tests_by_type, rebuild):
        self.worktree = worktree
        self.tests_by_type = tests_by_type
        self.rebuild = rebuild

    def __enter__(self):
        self.create()
        self.try_rev = self.worktree.head.commit.hexsha

    def __exit__(self, *args, **kwargs):
        assert self.worktree.head.commit.hexsha == self.try_rev

    def push(self):
        logger.info("Pushing to try with message:\n{}".format(self.worktree.head.commit.message))
        status, stdout, stderr = self.worktree.git.push('try', with_extended_output=True)
        rev_match = rev_re.search(stderr)
        if not rev_match:
            logger.debug("No revision found in string:\n\n{}\n".format(stderr))
        else:
            try_rev = rev_match.group('rev')
        if status != 0:
            logger.error("Failed to push to try.")
            # TODO retry
            raise AbortError("Failed to push to try")
        return try_rev


class TrySyntaxCommit(TryCommit):
    def create(self):
        message = self.try_message(self.tests_by_type, self.rebuild)
        self.worktree.index.commit(message=message)

    @staticmethod
    def try_message(tests_by_type=None, rebuild=0):
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


# Entry point
def new_wpt_pr(git_gecko, git_wpt, pr_data):
    """ Start a new downstream sync """
    update_repositories()
    pr_id = pr_data["number"]
    if WptDownstreamSync.for_pr(pr_id):
        return
    wpt_base = pr_data["base"]["ref"]
    sync = WptDownstreamSync.new(git_gecko,
                                 git_wpt,
                                 wpt_base,
                                 pr_id,
                                 pr_data["title"],
                                 pr_title["body"])
    sync.update_commits()
    # Now wait for the status to change before we take any actions


# Entry point
def status_changed(git_gecko, git_wpt, sync, context, status, head_sha):
    # TODO: seems like ignoring status that isn't our own would make more sense
    if context != "continuous-integration/travis-ci/pr":
        logger.debug("Ignoring status for context %s." % context)
        return

    update_repositories()

    pr_id = pr_for_commit(head_sha)
    sync = WptDownstreamSync.for_pr(pr_id)

    if status == "pending":
        # We got new commits that we missed
        sync.update_commits()
    elif status == "passed":
        if sync.try_push_for_commit(head_sha):
            return
        sync.update_commits()
        TryPush.create(git_gecko, sync)

        # TODO set a status on the PR
        # TODO: check only for status of Firefox job(s)


# Entry point
def update_taskgroup(git_gecko, git_wpt, sha1, task_id, result):
    try_push = TryPush.for_commit(git_gecko, sha1)
    if not try_push:
        return

    try_push.taskgroup_id = task_id

    if result != "success":
        # TODO manual intervention, schedule sync-retry?
        logger.debug("Try push Decision Task for PR {} "
                     "did not succeed:\n{}\n".format(try_push.sync.pr.id, body))
        try_push.status = "infra-fail"
        pr_id = try_push.pr_id
        sync = WptDownstreamSync.for_pr(git_gecko, git_wpt, pr_id)
        if sync and sync.bug:
            env.bz.comment(try_push.sync.bug,
                           "Try push failed: decision task returned error")


# Entry point
def on_taskgroup_resolved(git_gecko, git_wpt, taskgroup_id):
    try_push = TryPush.by_taskgroup(git_gecko, taskgroup_id)
    if not try_push:
        # this is not one of our try_pushes
        return

    pr_id = try_push.pr_id
    sync = WptDownstreamSync.for_pr(git_gecko, git_wpt, pr_id)
    log_files = try_push.download_logs()
    disabled = sync.update_metadata(log_files, stability)

    if not try_push.stability:
        # TODO check if tonnes of tests are failing -- don't want to update the
        # expectation data in that case
        # TODO decide whether to do another narrow push on mac
        TryPush.create(git_gecko, sync, stability=True)
    else:
        if disabled:
            logger.debug("The following tests were disabled:\n%s" % disabled)
            # TODO notify relevant people about test expectation changes, stability
            env.bz.comment(sync.bug, ("The following tests were disabled "
                                      "based on stability try push:\n %s". % disabled))
