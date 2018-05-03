# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

from __future__ import print_function

"""Functionality to support VCS syncing for WPT."""

import os
import re
import traceback
from collections import defaultdict

import enum
import git

import base
import bugcomponents
import gitutils
import log
import notify
import trypush
import commit as sync_commit
from env import Environment
from gitutils import update_repositories
from pipeline import AbortError
from projectutil import Mach, WPT
from trypush import TryPush

logger = log.get_logger(__name__)
env = Environment()


@enum.unique
class DownstreamAction(enum.Enum):
    ready = 0
    manual_fix = 1
    try_push = 2
    try_push_stability = 3
    wait_try = 4
    wait_approved = 5

    def reason_str(self):
        return {DownstreamAction.ready: "",
                DownstreamAction.manual_fix: "",
                DownstreamAction.try_push: "valid try push required",
                DownstreamAction.try_push_stability: "stability try push required",
                DownstreamAction.wait_try: "waiting for try to complete",
                DownstreamAction.wait_approved: "waiting for PR to be approved"}.get(self, "")


class DownstreamSync(base.SyncProcess):
    sync_type = "downstream"
    obj_id = "pr"
    statuses = ("open", "complete")
    status_transitions = [("open", "complete")]

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

        sync.create_bug(git_wpt, pr_id, pr_title, pr_body)
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
    def has_metadata(cls, message):
        required_keys = ["wpt-commits",
                         "wpt-pr"]
        metadata = sync_commit.get_metadata(message)
        return all(item in metadata for item in required_keys)

    @property
    def pr_head(self):
        """Head commit of the PR. Typically this is equal to
        self.wpt_commits.head but if the PR is rebased onto master
        then the head of the PR won't match the commit that actually
        merged unless it happens to be a fast-forward"""
        return sync_commit.WptCommit(self.git_wpt, "origin/pr/%s" % self.pr)

    @property
    def pr_status(self):
        return self.data.get("pr-status", "open")

    @pr_status.setter
    def pr_status(self, value):
        self.data["pr-status"] = value

    @property
    def next_action(self):
        """Work out the next action for the sync based on the current status.

        Returns a DownstreamAction indicating the next step to take."""

        if self.data.get("force-metadata-ready"):
            # This is mostly for testing
            return DownstreamAction.ready
        if self.skip:
            return DownstreamAction.ready
        if not self.requires_try:
            return DownstreamAction.ready
        if self.error:
            return DownstreamAction.manual_fix

        latest_try_push = self.latest_valid_try_push

        if not latest_try_push:
            return DownstreamAction.try_push

        if latest_try_push.status != "complete":
            return DownstreamAction.wait_try
        if latest_try_push.infra_fail:
            if len(self.latest_busted_try_pushes()) > 5:
                return DownstreamAction.manual_fix

            if not latest_try_push.stability:
                return DownstreamAction.try_push

            # Don't worry about recreating stability try pushes for infra failures
            return DownstreamAction.ready

        if not latest_try_push.stability and self.requires_stability_try:
            pr = env.gh_wpt.get_pull(self.pr)
            pr_ready = pr.merged or env.gh_wpt.is_approved(self.pr)
            if pr_ready:
                return DownstreamAction.try_push_stability
            return DownstreamAction.wait_approved

        return DownstreamAction.ready

    @property
    def metadata_ready(self):
        return self.next_action == DownstreamAction.ready

    @property
    def results_notified(self):
        return self.data.get("results-notified", False)

    @results_notified.setter
    def results_notified(self, value):
        self.data["results-notified"] = value

    @property
    def skip(self):
        return self.data.get("skip", False)

    @skip.setter
    def skip(self, value):
        self.data["skip"] = value

    @property
    def wpt(self):
        git_work = self.wpt_worktree.get()
        return WPT(os.path.join(git_work.working_dir))

    @property
    def requires_try(self):
        return not self.skip

    @property
    def requires_stability_try(self):
        return self.requires_try and bool(self.affected_tests())

    @property
    def latest_valid_try_push(self):
        """Try push for the current head of the PR, if any.

        In legacy cases we don't store the wpt-head for the try push
        so we always assume that any try push is valid"""

        latest_try_push = self.latest_try_push
        if latest_try_push and self.affected_tests() and latest_try_push.expired():
            latest_try_push = None

        # Check if the try push is for the current PR head
        if (latest_try_push and
            latest_try_push.wpt_head and
            latest_try_push.wpt_head not in (self.pr_head.sha1, self.wpt_commits.head.sha1)):
            # If the try push isn't for the head of the PR or for the post merge head
            # then we need a new try push
            latest_try_push = None

        return latest_try_push

    def next_try_push(self, try_cls=trypush.TrySyntaxCommit):
        """Schedule a new try push for the sync, if required.

        A stability try push will only be scheduled if the upstream PR is
        approved, which we check directly from GitHub. Therefore returning
        None is not an indication that the sync is ready to land, just that
        there's no further action at this time.
        """
        if not self.requires_try or self.status != "open":
            return

        self.update_commits()

        action = self.next_action
        if action == DownstreamAction.try_push:
            return TryPush.create(self, self.affected_tests(), stability=False,
                                  try_cls=try_cls)
        elif action == DownstreamAction.try_push_stability:
            return TryPush.create(self, self.affected_tests(), stability=True,
                                  try_cls=try_cls)

    def create_bug(self, git_wpt, pr_id, pr_title, pr_body):
        if self.bug is not None:
            return
        comment = self.make_bug_comment(git_wpt, pr_id, pr_title, pr_body)
        bug = env.bz.new(summary="[wpt-sync] Sync PR %s - %s" % (pr_id, pr_title),
                         comment=comment,
                         product="Testing",
                         component="web-platform-tests",
                         whiteboard="[wptsync downstream]",
                         priority="P4",
                         url=env.gh_wpt.pr_url(pr_id))
        self.bug = bug

    def update_wpt_commits(self):
        """Update the set of commits in the PR from the latest upstream."""
        if not self.wpt_commits.head or self.wpt_commits.head.sha1 != self.pr_head.sha1:
            self.wpt_commits.head = self.pr_head

        if len(self.wpt_commits) == 0 and self.git_wpt.is_ancestor(self.wpt_commits.head.sha1,
                                                                   "origin/master"):
            # The commits landed on master so we need to change the commit
            # range to not use origin/master as a base
            base_commit = None
            assert self.wpt_commits.head.pr() == self.pr
            for commit in self.git_wpt.iter_commits(self.wpt_commits.head.sha1):
                wpt_commit = sync_commit.WptCommit(self.git_wpt, commit)
                if wpt_commit.pr() != self.pr:
                    base_commit = wpt_commit
                    break

            self.wpt_commits.base = self.data["wpt-base"] = base_commit.sha1

    def files_changed(self):
        # TODO: Would be nice to do this from mach with a gecko worktree
        return set(self.wpt.files_changed().split("\n"))

    def commit_manifest_update(self):
        gecko_work = self.gecko_worktree.get()
        mach = Mach(gecko_work.working_dir)
        mach.wpt_manifest_update()
        if gecko_work.is_dirty():
            manifest_path = os.path.join(env.config["gecko"]["path"]["meta"],
                                         "MANIFEST.json")
            gecko_work.index.add([manifest_path])
            metadata = {
                "wpt-pr": self.pr,
                "wpt-type": "manifest"
            }
            msg = sync_commit.Commit.make_commit_msg("Bug %s [wpt PR %s] - Update wpt manifest" %
                                                     (self.bug, self.pr), metadata)
            gecko_work.index.commit(message=msg)
        if gecko_work.is_dirty():
            # This can happen if the mozilla/meta/MANIFEST.json is updated
            gecko_work.git.reset(hard=True)

    @property
    def metadata_commit(self):
        if len(self.gecko_commits) == 0:
            return
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
            "Bug %s [wpt PR %s] - Update wpt metadata, a=testonly" %
            (self.bug, self.pr), metadata)
        git_work.git.commit(message=msg, allow_empty=True)
        commit = git_work.commit("HEAD")
        return sync_commit.GeckoCommit(self.git_gecko, commit.hexsha)

    def set_bug_component(self, files_changed):
        new_component = bugcomponents.get(self.gecko_worktree.get(),
                                          files_changed,
                                          default=("Testing", "web-platform-tests"))
        env.bz.set_component(self.bug, *new_component)

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
        bugcomponents.update(gecko_work, renames)

        self._commit_metadata()

    def _commit_metadata(self):
        assert self.metadata_commit
        gecko_work = self.gecko_worktree.get()
        if gecko_work.is_dirty():
            gecko_work.git.commit(amend=True, no_edit=True)

    def update_commits(self):
        try:
            self.update_wpt_commits()

            # Check if this sync reverts some unlanded earlier PR and if so mark both
            # as skip and don't try to apply the commits here
            reverts = self.reverts_syncs()
            if reverts:
                all_open = all(item.status == "open" for item in reverts)
                for revert_sync in reverts:
                    if revert_sync.status == "open":
                        logger.info("Skipping sync for PR %s because it is later reverted" %
                                    revert_sync.pr)
                        revert_sync.skip = True
                # TODO: If this commit reverts some closed syncs, then set the metadata
                # commit of this commit to the revert of the metadata commit from that
                # sync
                if all_open:
                    logger.info("Sync was a revert of other open syncs, skipping")
                    self.skip = True
                    return False

            old_gecko_head = self.gecko_commits.head.sha1
            logger.debug("PR %s gecko HEAD was %s" % (self.pr, old_gecko_head))
            self.wpt_to_gecko_commits()

            logger.debug("PR %s gecko HEAD now %s" % (self.pr, self.gecko_commits.head.sha1))
            if old_gecko_head == self.gecko_commits.head.sha1:
                logger.info("Gecko commits did not change for PR %s" % self.pr)
                return False

            self.commit_manifest_update()

            renames = self.wpt_renames()
            self.move_metadata(renames)
            self.update_bug_components(renames)

            files_changed = self.files_changed()
            self.set_bug_component(files_changed)
        except Exception:
            raise
        else:
            # If we managed to apply all the commits without error, reset the error flag
            self.error = None
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

        # If we have a metadata commit already, remove it and re-apply it at the end to
        # ensure it's always the last commit in the branch
        metadata_commit = self.data.get("metadata-commit")

        try:
            gecko_work = self.gecko_worktree.get()
            if (len(self.gecko_commits) > 1 and
                self.gecko_commits[-1].metadata.get("wpt-type") == "metadata"):
                metadata_commit = self.gecko_commits[-1].sha1
                self.data["metadata-commit"] = metadata_commit
                self.gecko_commits.head = self.gecko_commits[-2]
                gecko_work.git.reset(hard=True)

            if retain_commits and len(retain_commits) < len(existing_gecko_commits):
                self.gecko_commits.head = retain_commits[-1].sha1
                gecko_work.git.reset(hard=True)
            elif not retain_commits:
                # If there's no commit in common don't try to reuse the current branch at all
                logger.info("Resetting gecko to latest central")
                self.gecko_commits.head = self.gecko_landing_branch()
                gecko_work.git.reset(hard=True)

            append_commits = self.wpt_commits[len(retain_commits):]
            if not append_commits:
                return

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
        except Exception:
            raise
        else:
            if (metadata_commit is not None and
                self.gecko_commits[-1].metadata.get("wpt-type") != "metadata"):
                gitutils.cherry_pick(gecko_work, metadata_commit)
            self.data["metadata-commit"] = None

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
            gecko_work.git.add(meta_path, all=True)
            self._commit_metadata()

        return disabled

    def try_notify(self):
        if self.results_notified:
            return

        if not self.bug:
            logger.error("Sync for PR %s has no associated bug" % self.pr)
            return

        if not self.affected_tests():
            logger.debug("PR %s doesn't have affected tests so skipping results notification" %
                         self.pr)
            return

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
            logger.info("Not all mozilla-central results available for PR %s" % self.pr)
            return

        try_tasks = complete_try_push.wpt_tasks()
        complete_try_push.download_logs(report=True, raw=False)

        msg = notify.get_msg(try_tasks, central_tasks)
        env.bz.comment(self.bug, msg)
        self.results_notified = True

    def reverts_syncs(self):
        """Return a set containing the previous syncs reverted by this one, if any"""
        revert_re = re.compile("This reverts commit ([0-9A-Fa-f]+)")
        unreverted_commits = defaultdict(set)
        for commit in self.wpt_commits:
            if not commit.msg.startswith("Revert "):
                # If not everything is a revert then return
                return set()
            revert_shas = revert_re.findall(commit.msg)
            if len(revert_shas) == 0:
                return set()
            # Just use the first match for now
            sha = revert_shas[0]
            try:
                self.git_wpt.rev_parse(sha)
            except git.BadName:
                # Commit isn't in this repo (could be upstream)
                return set()
            pr = env.gh_wpt.pr_for_commit(sha)
            if pr is None:
                return set()
            sync = DownstreamSync.for_pr(self.git_gecko, self.git_wpt, pr)
            if sync is None:
                return set()
            if sync not in unreverted_commits:
                # Ensure we have the latest commits for the reverted sync
                sync.update_wpt_commits()
                unreverted_commits[sync] = {item.sha1 for item in sync.wpt_commits}
            if sha in unreverted_commits[sync]:
                unreverted_commits[sync].remove(sha)

        rv = {sync for sync, unreverted in unreverted_commits.iteritems()
              if not unreverted}
        return rv


@base.entry_point("downstream")
def new_wpt_pr(git_gecko, git_wpt, pr_data, raise_on_error=True):
    """ Start a new downstream sync """
    if pr_data["user"]["login"] == env.config["web-platform-tests"]["github"]["user"]:
        raise ValueError("Tried to create a downstream sync for a PR created "
                         "by the wpt bot")
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
                              pr_data["body"] or "")
    try:
        sync.update_commits()
    except Exception as e:
        sync.error = e
        if raise_on_error:
            raise
        traceback.print_exc()
        logger.error(e)
    # Now wait for the status to change before we take any actions


@base.entry_point("downstream")
def commit_status_changed(git_gecko, git_wpt, sync, context, status, url, head_sha,
                          raise_on_error=False):
    update_repositories(git_gecko, git_wpt)
    if sync.skip:
        return
    try:
        logger.debug("Got status %s for PR %s" % (status, sync.pr))
        if status == "pending":
            # We got new commits that we missed
            sync.update_commits()
            return
        check_state, _ = env.gh_wpt.get_combined_status(sync.pr)
        sync.last_pr_check = {"state": check_state, "sha": head_sha}
        if check_state == "success":
            sync.next_try_push()
    except Exception as e:
        sync.error = e
        if raise_on_error:
            raise
        traceback.print_exc()
        logger.error(e)


@base.entry_point("downstream")
def try_push_complete(git_gecko, git_wpt, try_push, sync):
    if not try_push.taskgroup_id:
        logger.error("No taskgroup id set for try push")
        return

    if not try_push.status == "complete":
        # Ensure we don't have some old set of tasks
        try_push.wpt_tasks(force_update=True)
        if not try_push.is_complete(allow_unscheduled=True):
            logger.info("Try push is not complete")
            return

        if not try_push.validate_tasks():
            if len(sync.latest_busted_try_pushes()) > 5:
                message = ("Too many busted try pushes. "
                           "Check the try results for infrastructure issues.")
                sync.error = message
                env.bz.comment(sync.bug, message)
                try_push.status = "complete"
                raise AbortError(message)
        else:
            logger.info("Try push %r for PR %s complete" % (try_push, sync.pr))
            disabled = []
            if not try_push.success():
                if sync.affected_tests():
                    log_files = try_push.download_raw_logs()
                    if not log_files:
                        raise ValueError("No log files found for try push %r" % try_push)
                    disabled = sync.update_metadata(log_files, stability=try_push.stability)
                else:
                    env.bz.comment(sync.bug, ("The PR was not expected to affect any tests, "
                                              "but the try push wasn't a success. Check the try "
                                              "results for infrastructure issues"))
                    # TODO: consider marking the push an error here so that we can't land without
                    # manual intervention

            if try_push.stability and disabled:
                logger.info("The following tests were disabled:\n%s" % "\n".join(disabled))
                # TODO notify relevant people about test expectation changes, stability
                env.bz.comment(sync.bug, ("The following tests were disabled "
                                          "based on stability try push:\n %s" %
                                          "\n".join(disabled)))
    try_push.status = "complete"

    next_try_push = sync.next_try_push()

    if not next_try_push or next_try_push.stability:
        pr = env.gh_wpt.get_pull(sync.pr)
        if pr.merged:
            sync.try_notify()


@base.entry_point("downstream")
def pull_request_approved(git_gecko, git_wpt, sync):
    try:
        sync.next_try_push()
    except Exception as e:
        sync.error = e
        raise


@base.entry_point("downstream")
def update_pr(git_gecko, git_wpt, sync, action, merge_sha, base_sha):
    try:
        if action == "closed" and not merge_sha:
            sync.pr_status = "closed"
            sync.finish()
        elif action == "closed":
            # We are storing the wpt base as a reference
            sync.data["wpt-base"] = base_sha
            sync.next_try_push()
            sync.try_notify()
        elif action == "reopened" or action == "open":
            sync.status = "open"
            sync.pr_status = "open"
            sync.next_try_push()
    except Exception as e:
        sync.error = e
        raise
