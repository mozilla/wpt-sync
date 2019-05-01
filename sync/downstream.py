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
from errors import AbortError
from gitutils import update_repositories
from lock import SyncLock, mut, constructor
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
    status_transitions = [("open", "complete"),
                          ("complete", "open")]  # Unfortunately, if a backout occurs

    @classmethod
    @constructor(lambda args: ("downstream", str(args['pr_id'])))
    def new(cls, lock, git_gecko, git_wpt, wpt_base, pr_id, pr_title, pr_body):
        # TODO: add PR link to the comment
        sync = super(DownstreamSync, cls).new(lock,
                                              git_gecko,
                                              git_wpt,
                                              pr=pr_id,
                                              gecko_base=DownstreamSync.gecko_landing_branch(),
                                              gecko_head=DownstreamSync.gecko_landing_branch(),
                                              wpt_base=wpt_base,
                                              wpt_head="origin/pr/%s" % pr_id)

        with sync.as_mut(lock):
            sync.create_bug(git_wpt, pr_id, pr_title, pr_body)
        return sync

    def make_bug_comment(self, git_wpt, pr_id, pr_title, pr_body):
        pr_msg = env.gh_wpt.cleanup_pr_body(pr_body)
        # TODO: Ensure we have the right set of commits before geting here
        author = self.wpt_commits[0].author if self.wpt_commits else ""

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
    def has_metadata(cls, message):
        required_keys = ["wpt-commits",
                         "wpt-pr"]
        metadata = sync_commit.get_metadata(message)
        return all(item in metadata for item in required_keys)

    @property
    def landable_status(self):
        if self.skip:
            return base.LandableStatus.skip
        if self.metadata_ready:
            return base.LandableStatus.ready
        if self.error:
            return base.LandableStatus.error

        return base.LandableStatus.missing_try_results

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
    @mut()
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
            logger.debug("Sync for PR %s has no valid try push" % self.pr)
            return DownstreamAction.try_push

        if latest_try_push.status != "complete":
            return DownstreamAction.wait_try
        if latest_try_push.infra_fail:
            if len(self.latest_busted_try_pushes()) > 5:
                return DownstreamAction.manual_fix

            if not latest_try_push.stability:
                logger.debug("Sync for PR %s has a try push, but it has an infra failure" % self.pr)
                return DownstreamAction.try_push

            # Don't worry about recreating stability try pushes for infra failures
            return DownstreamAction.ready

        if not latest_try_push.stability and self.requires_stability_try:
            logger.debug("Sync for PR %s has a initial try push but requires a stability try push" %
                         self.pr)
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
    @mut()
    def results_notified(self, value):
        self.data["results-notified"] = value

    @property
    def skip(self):
        return self.data.get("skip", False)

    @skip.setter
    @mut()
    def skip(self, value):
        self.data["skip"] = value

    @property
    def wpt(self):
        git_work = self.wpt_worktree.get()
        git_work.git.reset(hard=True)
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

    @mut()
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
            return TryPush.create(self._lock,
                                  self,
                                  self.affected_tests(),
                                  stability=False,
                                  try_cls=try_cls)
        elif action == DownstreamAction.try_push_stability:
            return TryPush.create(self._lock,
                                  self,
                                  self.affected_tests(),
                                  stability=True,
                                  try_cls=try_cls)

    @mut()
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

    @mut()
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

    @property
    def metadata_commit(self):
        if len(self.gecko_commits) == 0:
            return
        if self.gecko_commits[-1].metadata.get("wpt-type") == "metadata":
            return self.gecko_commits[-1]

    @mut()
    def ensure_metadata_commit(self):
        if self.metadata_commit:
            return self.metadata_commit

        git_work = self.gecko_worktree.get()

        if "metadata-commit" in self.data:
            gitutils.cherry_pick(git_work, self.data["metadata-commit"])
            # We only care about the cached value inside this function, so
            # remove it now so we don't have to maintain it
            del self.data["metadata-commit"]
        else:
            assert all(item.metadata.get("wpt-type") != "metadata" for item in self.gecko_commits)
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

    @mut()
    def set_bug_component(self, files_changed):
        new_component = bugcomponents.get(self.gecko_worktree.get(),
                                          files_changed,
                                          default=("Testing", "web-platform-tests"))
        env.bz.set_component(self.bug, *new_component)

    @mut()
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

    @mut()
    def update_bug_components(self, renames):
        if not renames:
            return

        self.ensure_metadata_commit()

        gecko_work = self.gecko_worktree.get()
        bugcomponents.update(gecko_work, renames)

        self._commit_metadata()

    @mut()
    def _commit_metadata(self, amend=True):
        assert self.metadata_commit
        gecko_work = self.gecko_worktree.get()
        if gecko_work.is_dirty():
            try:
                gecko_work.git.commit(amend=True, no_edit=True)
            except git.GitCommandError as e:
                if amend and e.status == 1 and "--allow-empty" in e.stdout:
                    logger.warning("Amending commit made it empty, resetting")
                    gecko_work.git.reset("HEAD^")

    @mut()
    def update_commits(self):
        exception = None
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
                        with SyncLock.for_process(revert_sync.process_name) as revert_lock:
                            with revert_sync.as_mut(revert_lock):
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

            def plain_apply():
                logger.info("Applying on top of the current commits")
                self.wpt_to_gecko_commits()
                return True

            def rebase_apply():
                logger.info("Applying with a rebase onto latest inbound")
                new_base = self.gecko_integration_branch()
                if len(self.gecko_commits) > 0:
                    gecko_work = self.gecko_worktree.get()
                    if self.gecko_commits[0].metadata["wpt-type"] == "dependent":
                        # If we have any dependent commits first reset to the new
                        # head. This prevents conflicts if the dependents already
                        # landed
                        # TODO: Actually check if they landed?
                        reset_head = new_base
                    else:
                        reset_head = "HEAD"
                    gecko_work.git.reset(reset_head, hard=True)
                    try:
                        self.gecko_rebase(new_base)
                    except git.GitCommandError:
                        try:
                            gecko_work.git.rebase(abort=True)
                        except git.GitCommandError:
                            pass
                        raise AbortError("Rebasing onto latest gecko failed")
                    self.wpt_to_gecko_commits(base=new_base)
                    return True

            def dependents_apply():
                logger.info("Applying with upstream dependents")
                dependencies = self.unlanded_commits_same_files()
                if dependencies:
                    logger.info("Found dependencies:\n%s" %
                                "\n".join(item.msg.splitlines()[0] for item in dependencies))
                    self.wpt_to_gecko_commits(dependencies)
                    env.bz.comment(self.bug,
                                   "PR %s applied with additional changes from upstream: %s"
                                   % (self.pr, ", ".join(item.sha1 for item in dependencies)))
                    return True

            error = None
            for fn in [plain_apply, rebase_apply, dependents_apply]:
                try:
                    if fn():
                        error = None
                        break
                    else:
                        logger.error("Applying with %s was a no-op" % fn.__name__)
                except Exception as e:
                    import traceback
                    error = e
                    logger.error("Applying with %s errored" % fn.__name__)
                    logger.error(traceback.format_exc(e))

            if error is not None:
                raise error

            logger.debug("PR %s gecko HEAD now %s" % (self.pr, self.gecko_commits.head.sha1))
            if old_gecko_head == self.gecko_commits.head.sha1:
                logger.info("Gecko commits did not change for PR %s" % self.pr)
                return False

            # If we have a metadata commit already, ensure it's applied now
            if "metadata-commit" in self.data:
                self.ensure_metadata_commit()

            renames = self.wpt_renames()
            self.move_metadata(renames)
            self.update_bug_components(renames)

            files_changed = self.files_changed()
            self.set_bug_component(files_changed)
        except Exception as e:
            exception = e
            raise
        finally:
            # If we managed to apply all the commits without error, reset the error flag
            # otherwise update it with the current exception
            self.error = exception
        return True

    @mut()
    def wpt_to_gecko_commits(self, dependencies=None, base=None):
        """Create a patch based on wpt branch, apply it to corresponding gecko branch.

        If there is a commit with wpt-type metadata, this function will remove it. The
        sha1 will be stashed in self.data["metadata-commit"] so it can be restored next time
        we call ensure_metadata_commit()
        """
        # The logic here is that we can retain any dependent commits as long as we have
        # at least the set in the dependencies array, followed by the gecko commits created
        # from the wpt_commits, interspersed with any number of manifest commits,
        # followed by zero or one metadata commits

        if dependencies:
            expected_commits = [(item.sha1, item, True) for item in dependencies]
        else:
            # If no dependencies are supplied, retain the ones that we alredy have, if any
            expected_commits = []
            for commit in self.gecko_commits:
                if commit.metadata.get("wpt-type") == "dependency":
                    expected_commits.append((commit.metadata["wpt-commit"], None, True))
                else:
                    break

        # Expect all the new commits
        expected_commits.extend((item.sha1, item, False) for item in self.wpt_commits
                                if not item.is_merge)

        existing_commits = [commit for commit in self.gecko_commits
                            if commit.metadata.get("wpt-commit") and
                            commit.metadata.get("wpt-type") in ("dependency", None)]

        retain_commits = 0
        for gecko_commit, (wpt_sha1, _, _) in zip(existing_commits, expected_commits):
            if gecko_commit.metadata.get("wpt-commit") != wpt_sha1:
                break
            retain_commits += 1

        keep_commits = existing_commits[:retain_commits]
        add_commits = expected_commits[retain_commits:]

        # Strip out any leading commits that come from currently applied dependencies that are
        # not being retained
        strip_count = 0
        for _, wpt_commit, _ in add_commits:
            if wpt_commit is not None:
                break
            strip_count += 1
        add_commits = add_commits[strip_count:]

        if len(keep_commits) == len(existing_commits) and not add_commits:
            logger.info("Commits did not change")
            return

        logger.info("Keeping %i existing commits; adding %i new commits" % (len(keep_commits),
                                                                            len(add_commits)))

        if self.metadata_commit:
            # If we have a metadata commit, store it in self.data["metadata-commit"]
            # remove it when updating commits, and reapply it when we next call
            # ensure_metadata_commit
            self.data["metadata-commit"] = self.metadata_commit.sha1

        reset_head = None
        if not keep_commits:
            reset_head = base if base is not None else self.gecko_landing_branch()
        elif len(keep_commits) < len(existing_commits):
            reset_head = keep_commits[-1]
        elif ("metadata-commit" in self.data and
              self.gecko_commits[-1].metadata.get("wpt-type") == "metadata"):
            reset_head = self.gecko_commits[-2]

        # Clear the set of affected tests since there are updates
        del self.data["affected-tests"]

        gecko_work = self.gecko_worktree.get()

        if reset_head:
            self.gecko_commits.head = reset_head
        gecko_work.git.reset(hard=True)

        for _, wpt_commit, is_dependency in add_commits:
            logger.info("Moving commit %s" % wpt_commit.sha1)
            if is_dependency:
                metadata = {
                    "wpt-type": "dependency",
                    "wpt-commit": wpt_commit.sha1
                }
                msg_filter = None
            else:
                metadata = {
                    "wpt-pr": self.pr,
                    "wpt-commit": wpt_commit.sha1
                }
                msg_filter = self.message_filter

            wpt_commit.move(gecko_work,
                            dest_prefix=env.config["gecko"]["path"]["wpt"],
                            msg_filter=msg_filter,
                            metadata=metadata,
                            patch_fallback=True)

    def unlanded_commits_same_files(self):
        import landing

        sync_point = landing.load_sync_point(self.git_gecko, self.git_wpt)
        base = sync_point["upstream"]
        head = "origin/master"
        changed = self.wpt_commits.files_changed
        commits = []
        for commit in self.git_wpt.iter_commits("%s..%s" % (base, head),
                                                reverse=True,
                                                paths=list(changed)):
            commit = sync_commit.WptCommit(self.git_wpt, commit)
            # Check for same-pr rather than same-commit because we always
            # use the commits on the PR branch, not the merged commits.
            # The other option is to use the GH API to decide if the PR
            # merged and if so what the merge commit was, although in that
            # case we would still not know the commit prior to merge, which
            # is what we need
            if commit.pr() == str(self.pr):
                break
            commits.append(commit)
        return commits

    def message_filter(self, msg):
        msg = sync_commit.try_filter(msg)
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

    @mut()
    def update_metadata(self, log_files, stability=False):
        meta_path = env.config["gecko"]["path"]["meta"]
        args = log_files
        if stability:
            args.extend(["--stability", "wpt-sync Bug %s" % self.bug])

        gecko_work = self.gecko_worktree.get()
        mach = Mach(gecko_work.working_dir)
        logger.debug("Updating metadata")
        mach.wpt_manifest_update()
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

    @mut()
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
        for try_push in sorted(self.try_pushes(), key=lambda x: -x._ref.process_name.seq_id):
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
            except (ValueError, git.BadName):
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
                with SyncLock.for_process(sync.process_name) as revert_lock:
                    with sync.as_mut(revert_lock):
                        sync.update_wpt_commits()
                unreverted_commits[sync] = {item.sha1 for item in sync.wpt_commits}
            if sha in unreverted_commits[sync]:
                unreverted_commits[sync].remove(sha)

        rv = {sync for sync, unreverted in unreverted_commits.iteritems()
              if not unreverted}
        return rv


@base.entry_point("downstream")
def new_wpt_pr(git_gecko, git_wpt, pr_data, raise_on_error=True, repo_update=True):
    """ Start a new downstream sync """
    if pr_data["user"]["login"] == env.config["web-platform-tests"]["github"]["user"]:
        raise ValueError("Tried to create a downstream sync for a PR created "
                         "by the wpt bot")
    if repo_update:
        update_repositories(git_gecko, git_wpt)
    pr_id = pr_data["number"]
    if DownstreamSync.for_pr(git_gecko, git_wpt, pr_id):
        return
    wpt_base = "origin/%s" % pr_data["base"]["ref"]

    with SyncLock("downstream", str(pr_id)) as lock:
        sync = DownstreamSync.new(lock,
                                  git_gecko,
                                  git_wpt,
                                  wpt_base,
                                  pr_id,
                                  pr_data["title"],
                                  pr_data["body"] or "")
        with sync.as_mut(lock):
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
@mut('sync')
def commit_status_changed(git_gecko, git_wpt, sync, context, status, url, head_sha,
                          raise_on_error=False, repo_update=True):
    if repo_update:
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
@mut('try_push', 'sync')
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
                try_push.infra_fail = True
                raise AbortError(message)
        elif len(try_push.failed_builds()):
            message = ("Try push had build failures")
            sync.error = message
            env.bz.comment(sync.bug, message)
            try_push.status = "complete"
            try_push.infra_fail = True
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
@mut('sync')
def pull_request_approved(git_gecko, git_wpt, sync):
    try:
        sync.next_try_push()
    except Exception as e:
        sync.error = e
        raise


@base.entry_point("downstream")
@mut('sync')
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
