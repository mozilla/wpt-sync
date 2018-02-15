# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

from __future__ import print_function

"""Functionality to support VCS syncing for WPT."""

import os
import traceback
from collections import defaultdict

import base
import bugcomponents
import log
import notify
import commit as sync_commit
from env import Environment
from gitutils import update_repositories
from projectutil import Mach, WPT
from trypush import TryPush

logger = log.get_logger(__name__)
env = Environment()


class DownstreamSync(base.SyncProcess):
    sync_type = "downstream"
    obj_id = "pr"

    @classmethod
    def new(cls, git_gecko, git_wpt, wpt_base, pr_id, pr_title, pr_body):
        # TODO: add PR link to the comment
        sync = super(DownstreamSync, cls).new(git_gecko,
                                              git_wpt,
                                              pr=pr_id,
                                              gecko_base=DownstreamSync.gecko_landing_branch(),
                                              gecko_head=DownstreamSync.gecko_landing_branch(),
                                              wpt_base=wpt_base,
                                              wpt_head="origin/pr/%s" % pr_id)

        comment = sync.make_bug_comment(git_wpt, pr_id, pr_title, pr_body)
        bug = env.bz.new(summary="[wpt-sync] Sync PR %s - %s" % (pr_id, pr_title),
                         comment=comment,
                         product="Testing",
                         component="web-platform-tests",
                         whiteboard="[wptsync downstream]",
                         priority="P3")
        sync.bug = bug
        return sync

    def make_bug_comment(self, git_wpt, pr_id, pr_title, pr_body):
        pr_msg = env.gh_wpt.cleanup_pr_body(pr_body)
        author = self.wpt_commits[0].author

        msg = ["Sync web-platform-tests PR %s into mozilla-central"
               " (this bug is closed when the sync is complete)." % pr_id,
               "",
               "PR: %s" % env.gh_wpt.pr_url(pr_id),
               ""
               "Details from upstream follow.",
               "",
               "%s wrote:" % author,
               ">  %s" % pr_title,
               ">  "]
        msg.extend((">  %s" % line for line in pr_msg.split("\n")))
        return "\n".join(msg)

    @classmethod
    def for_pr(cls, git_gecko, git_wpt, pr_id):
        items = cls.load_all(git_gecko, git_wpt, obj_id=pr_id)
        if len(items) > 1:
            raise ValueError("Got multiple syncs for PR")
        return items[0] if items else None

    @classmethod
    def for_bug(cls, git_gecko, git_wpt, bug):
        syncs = cls.load_all(git_gecko, git_wpt, status="*", obj_id="*")
        syncs = [item for item in syncs if item.bug == bug]

        if len(syncs) == 0:
            return None

        return syncs[0]

    @property
    def pr_head(self):
        return sync_commit.WptCommit(self.git_wpt, "origin/pr/%s" % self.pr)

    @property
    def pr_status(self):
        return self.data.get("pr-status", "open")

    @pr_status.setter
    def pr_status(self, value):
        self.data["pr-status"] = value

    @property
    def metadata_ready(self):
        return self.data.get("metadata-ready", False)

    @metadata_ready.setter
    def metadata_ready(self, value):
        self.data["metadata-ready"] = value

    @property
    def results_notified(self):
        return self.data.get("results-notified", False)

    @results_notified.setter
    def results_notified(self, value):
        self.data["results-notified"] = value

    @property
    def wpt(self):
        git_work = self.wpt_worktree.get()
        return WPT(os.path.join(git_work.working_dir))

    def update_status(self, action, merge_sha=None, wpt_base=None):
        if action == "closed" and not merge_sha:
            self.pr_status = "closed"
            self.finish()
        elif action == "closed":
            # We are storing the wpt base as a reference
            self.data["wpt-base"] = wpt_base
            # If we can, try to add a notification to the bug
            self.try_notify()
        elif action == "reopened" or action == "open":
            self.status = "open"
            self.pr_status = "open"

    def update_wpt_head(self):
        if not self.wpt_commits.head or self.wpt_commits.head.sha1 != self.pr_head.sha1:
            self.wpt_commits.head = self.pr_head

    def files_changed(self):
        # TODO: Would be nice to do this from mach with a gecko worktree
        return set(self.wpt.files_changed().split("\n"))

    def commit_manifest_update(self):
        gecko_work = self.gecko_worktree.get()
        mach = Mach(gecko_work.working_dir)
        mach.wpt_manifest_update()
        if gecko_work.is_dirty():
            gecko_work.index.add(["testing/web-platform/meta/MANIFEST.json"])
            metadata = {
                "wpt-pr": self.pr,
                "wpt-type": "manifest"
            }
            msg = sync_commit.Commit.make_commit_msg("Bug %s [wpt PR %s] - Update wpt manifest" %
                                                     (self.bug, self.pr), metadata)
            gecko_work.index.commit(message=msg)

    @property
    def metadata_commit(self):
        if self.gecko_commits[-1].metadata.get("wpt-type") == "metadata":
            return self.gecko_commits[-1]

    def ensure_metadata_commit(self):
        if self.metadata_commit:
            return self.metadata_commit

        assert all(item.metadata.get("wpt-type") != "metadata" for item in self.gecko_commits)
        git_work = self.gecko_worktree.get()
        metadata = {
            "wpt-pr": self.pr,
            "wpt-type": "metadata"
        }
        msg = sync_commit.Commit.make_commit_msg(
            "Bug %s [wpt PR %s]- Update wpt metadata, a=testonly" %
            (self.bug, self.pr), metadata)
        git_work.git.commit(message=msg, allow_empty=True)
        commit = git_work.commit("HEAD")
        return sync_commit.GeckoCommit(self.git_gecko, commit.hexsha)

    def set_bug_component(self, files_changed):
        new_component = bugcomponents.get(self.gecko_worktree.get(),
                                          files_changed,
                                          default=("Testing", "web-platform-tests"))
        env.bz.set_component(self.bug, *new_component)

    def renames(self):
        renames = {}
        diff_blobs = self.wpt_commits.head.commit.diff(
            self.git_wpt.merge_base(self.data["wpt-base"], self.wpt_commits.head.sha1))
        for item in diff_blobs:
            if item.rename_from:
                renames[item.rename_from] = item.rename_to
        return renames

    def move_metadata(self, renames):
        if not renames:
            return

        self.ensure_metadata_commit()

        gecko_work = self.gecko_worktree.get()
        metadata_base = env.config["gecko"]["path"]["meta"]
        for old_path, new_path in renames.iteritems():
            old_meta_path = os.path.join(metadata_base,
                                         old_path + ".ini")
            if os.path.exists(os.path.join(gecko_work.working_dir,
                                           old_meta_path)):
                new_meta_path = os.path.join(metadata_base,
                                             new_path + ".ini")
                dir_name = os.path.join(gecko_work.working_dir,
                                        os.path.dirname(new_meta_path))
                if not os.path.exists(dir_name):
                    os.makedirs(dir_name)
                gecko_work.index.move((old_meta_path, new_meta_path))

        self._commit_metadata()

    def update_bug_components(self, renames):
        if not renames:
            return

        self.ensure_metadata_commit()

        gecko_work = self.gecko_worktree.get()
        mozbuild_path = os.path.join(gecko_work.working_dir,
                                     env.config["gecko"]["path"]["wpt"],
                                     os.pardir,
                                     "moz.build")

        tests_base = os.path.split(env.config["gecko"]["path"]["wpt"])[1]

        def tests_rel_path(path):
            return os.path.join(tests_base, path)

        mozbuild_rel_renames = {tests_rel_path(old): tests_rel_path(new)
                                for old, new in renames.iteritems()}

        if os.path.exists(mozbuild_path):
            new_data = bugcomponents.remove_obsolete(mozbuild_path,
                                                     moves=mozbuild_rel_renames)
            with open(mozbuild_path, "w") as f:
                f.write(new_data)
        else:
            logger.warning("Can't find moz.build file to update")

        self._commit_metadata()

    def _commit_metadata(self):
        assert self.metadata_commit
        gecko_work = self.gecko_worktree.get()
        if gecko_work.is_dirty():
            gecko_work.git.commit(amend=True, no_edit=True)

    def update_commits(self):
        self.update_wpt_head()
        old_gecko_head = self.gecko_commits.head.sha1
        logger.debug("PR %s gecko HEAD was %s" % (self.pr, old_gecko_head))
        self.wpt_to_gecko_commits()

        logger.debug("PR %s gecko HEAD now %s" % (self.pr, self.gecko_commits.head.sha1))
        if old_gecko_head == self.gecko_commits.head.sha1:
            logger.info("Gecko commits did not change for PR %s" % self.pr)
            return False

        self.commit_manifest_update()

        renames = self.renames()
        self.move_metadata(renames)
        self.update_bug_components(renames)

        files_changed = self.files_changed()
        self.set_bug_component(files_changed)
        return True

    def wpt_to_gecko_commits(self):
        """Create a patch based on wpt branch, apply it to corresponding gecko branch"""
        existing_gecko_commits = [commit for commit in self.gecko_commits
                                  if commit.metadata.get("wpt-commit")]

        retain_commits = []
        if existing_gecko_commits:
            # TODO: Don't retain commits if the gecko HEAD has moved too much here since the
            # metadata won't be valid (e.g. check for dates > 1 day in the past)
            for gecko_commit, wpt_commit in zip(existing_gecko_commits, self.wpt_commits):
                if gecko_commit.metadata["wpt-commit"] == wpt_commit.sha1:
                    retain_commits.append(gecko_commit)
                else:
                    break

        logger.info("Retaining %s previous commits" % len(retain_commits))

        if retain_commits and len(retain_commits) < len(existing_gecko_commits):
            self.gecko_commits.head = retain_commits[-1].sha1
        elif not retain_commits:
            # If there's no commit in common don't try to reuse the current branch at all
            logger.info("Resetting gecko to latest central")
            self.gecko_commits.head = self.gecko_landing_branch()

        append_commits = self.wpt_commits[len(retain_commits):]
        if not append_commits:
            return

        gecko_work = self.gecko_worktree.get()
        for commit in append_commits:
            logger.info("Moving commit %s" % commit.sha1)
            metadata = {
                "wpt-pr": self.pr,
                "wpt-commit": commit.sha1
            }
            commit.move(gecko_work,
                        dest_prefix=env.config["gecko"]["path"]["wpt"],
                        msg_filter=self.message_filter,
                        metadata=metadata)

        return True

    def message_filter(self, msg):
        parts = msg.split("\n", 1)
        if len(parts) > 1:
            summary, body = parts
        else:
            summary = parts[0]
            body = ""
        new_msg = "Bug %s [wpt PR %s] - %s, a=testonly\n%s" % (self.bug,
                                                               self.pr,
                                                               summary,
                                                               body)
        return new_msg, {}

    def affected_tests(self, revish=None):
        # TODO? support files, harness changes -- don't want to update metadata
        if "affected-tests" not in self.data:
            tests_by_type = defaultdict(list)
            logger.info("Updating MANIFEST.json")
            self.wpt.manifest()
            args = ["--show-type", "--new"]
            if revish:
                args.append(revish)
            logger.info("Getting a list of tests affected by changes.")
            output = self.wpt.tests_affected(*args)
            if output:
                for item in output.strip().split("\n"):
                    path, test_type = item.strip().split("\t")
                    tests_by_type[test_type].append(path)
            self.data["affected-tests"] = tests_by_type
        return self.data["affected-tests"]

    def update_metadata(self, log_files, stability=False):
        meta_path = env.config["gecko"]["path"]["meta"]
        args = log_files
        if stability:
            args.extend(["--stability", "wpt-sync Bug %s" % self.bug])

        gecko_work = self.gecko_worktree.get()
        mach = Mach(gecko_work.working_dir)
        logger.debug("Updating metadata")
        output = mach.wpt_update(*args)
        prefix = "disabled:"
        disabled = []
        for line in output.split("\n"):
            if line.startswith(prefix):
                disabled.append(line[len(prefix):].strip())

        if gecko_work.is_dirty(untracked_files=True, path=meta_path):
            self.ensure_metadata_commit()
            gecko_work.index.add([meta_path])
            self._commit_metadata()

        return disabled

    def try_notify(self):
        if self.results_notified:
            return

        if not self.bug:
            logger.error("Sync has no associated bug")

        logger.info("Trying to generate results notification for PR %s" % self.pr)

        complete_try_push = None
        for try_push in sorted(self.try_pushes(), key=lambda x: -x._ref._process_name.seq_id):
            if try_push.status == "complete" and not try_push.stability:
                complete_try_push = try_push
                break

        if not complete_try_push:
            logger.info("No complete try push available for PR %s" % self.pr)
            return

        # Get the list of central tasks and download the wptreport logs
        central_tasks = notify.get_central_tasks(self.git_gecko, self)
        if not central_tasks:
            logger.info("Not all mozilla-central results avaiable for PR %s" % self.pr)
            return

        try_tasks = complete_try_push.wpt_tasks()
        complete_try_push.download_logs(report=True, raw=False)

        msg = notify.get_msg(try_tasks, central_tasks)
        env.bz.comment(self.bug, msg)
        self.results_notified = True


@base.entry_point("downstream")
def new_wpt_pr(git_gecko, git_wpt, pr_data, raise_on_error=True):
    """ Start a new downstream sync """
    update_repositories(git_gecko, git_wpt)
    pr_id = pr_data["number"]
    if DownstreamSync.for_pr(git_gecko, git_wpt, pr_id):
        return
    wpt_base = "origin/%s" % pr_data["base"]["ref"]
    sync = DownstreamSync.new(git_gecko,
                              git_wpt,
                              wpt_base,
                              pr_id,
                              pr_data["title"],
                              pr_data["body"])
    try:
        sync.update_commits()
    except Exception as e:
        sync.error = e
        if raise_on_error:
            raise
        traceback.print_exc()
        logger.error(e)
    else:
        sync.error = None
    # Now wait for the status to change before we take any actions


@base.entry_point("downstream")
def status_changed(git_gecko, git_wpt, sync, context, status, url, head_sha,
                   raise_on_error=False):
    update_repositories(git_gecko, git_wpt)
    previous_check = sync.last_pr_check
    try:
        logger.debug("Got status %s for PR %s" % (status, sync.pr))
        if status == "pending":
            # We got new commits that we missed
            sync.update_commits()
            return
        check_state, _ = env.gh_wpt.get_combined_status(sync.pr)
        sync.last_pr_check = {"state": check_state, "sha": head_sha}
        if check_state == "success" and previous_check != sync.last_pr_check:
            head_changed = sync.update_commits()
            if sync.latest_try_push and not head_changed:
                logger.info("Commits on PR %s didn't change" % sync.pr)
                return
            TryPush.create(sync, sync.affected_tests())
    except Exception as e:
        sync.error = e
        if raise_on_error:
            raise
        traceback.print_exc()
        logger.error(e)
    else:
        sync.error = None


@base.entry_point("downstream")
def try_push_complete(git_gecko, git_wpt, try_push, sync):
    logger.info("Try push %r for PR %s complete" % (try_push, sync.pr))
    # Ensure we don't have some old set of tasks
    try_push.wpt_tasks(force_update=True)
    if not try_push.success():
        log_files = try_push.download_raw_logs()
        if not log_files:
            raise ValueError("No log files found for try push %r" % try_push)
        disabled = sync.update_metadata(log_files, stability=try_push.stability)
    else:
        disabled = []

    if sync.affected_tests() and not try_push.stability:
        logger.info("Creating a stability try push for PR %s" % sync.pr)
        # TODO check if tonnes of tests are failing -- don't want to update the
        # expectation data in that case
        # TODO decide whether to do another narrow push on mac
        TryPush.create(sync, sync.affected_tests(), stability=True)
    else:
        if disabled:
            logger.info("The following tests were disabled:\n%s" % "\n".join(disabled))
            # TODO notify relevant people about test expectation changes, stability
            env.bz.comment(sync.bug, ("The following tests were disabled "
                                      "based on stability try push:\n %s" % "\n".join(disabled)))
        logger.info("Metadata is ready for PR %s" % (sync.pr))
        sync.metadata_ready = True
    try_push.status = "complete"

    pr = env.gh_wpt.get_pull(sync.pr)
    if pr.merged:
        sync.try_notify()
