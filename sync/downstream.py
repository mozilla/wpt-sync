# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""Functionality to support VCS syncing for WPT."""


from __future__ import annotations
import os
import re
import subprocess
import traceback
from collections import defaultdict
from datetime import datetime


import enum
import git
import newrelic

from . import bugcomponents
from . import gitutils
from . import log
from . import notify
from . import trypush
from . import commit as sync_commit
from .base import FrozenDict, entry_point
from .commit import GeckoCommit, WptCommit
from .env import Environment
from .errors import AbortError
from .gitutils import update_repositories
from .lock import SyncLock, mut, constructor
from .projectutil import Mach, WPT
from .sync import LandableStatus, SyncProcess
from .trypush import TryPush

from git.objects.tree import Tree
from git.repo.base import Repo
from typing import Any, List, Mapping, MutableMapping, cast, TYPE_CHECKING

logger = log.get_logger(__name__)
env = Environment()


@enum.unique
class DownstreamAction(enum.Enum):
    ready = 0
    manual_fix = 1
    try_push = 2
    try_push_stability = 3
    wait_try = 4
    wait_upstream = 5

    def reason_str(self):
        # type () -> Text
        return {DownstreamAction.ready: "",
                DownstreamAction.manual_fix: "",
                DownstreamAction.try_push: "valid try push required",
                DownstreamAction.try_push_stability: "stability try push required",
                DownstreamAction.wait_try: "waiting for try to complete",
                DownstreamAction.wait_upstream: "waiting for PR to be merged"}.get(self, "")


class DownstreamSync(SyncProcess):
    sync_type = "downstream"
    obj_id = "pr"
    statuses = ("open", "complete")
    status_transitions = [("open", "complete"),
                          ("complete", "open")]  # Unfortunately, if a backout occurs

    @classmethod
    @constructor(lambda args: ("downstream", str(args["pr_id"])))
    def new(cls,
            lock: SyncLock,
            git_gecko: Repo,
            git_wpt: Repo,
            wpt_base: str,
            pr_id: int,
            pr_title: str,
            pr_body: str,
            ) -> DownstreamSync:
        # TODO: add PR link to the comment
        sync = super().new(lock,
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

    def make_bug_comment(self, git_wpt: Repo, pr_id: int, pr_title: str,
                         pr_body: str | None) -> str:
        pr_msg = env.gh_wpt.cleanup_pr_body(pr_body)
        # TODO: Ensure we have the right set of commits before geting here
        author = self.wpt_commits[0].author if self.wpt_commits else b""

        msg = ["Sync web-platform-tests PR %s into mozilla-central"
               " (this bug is closed when the sync is complete)." % pr_id,
               "",
               "PR: %s" % env.gh_wpt.pr_url(pr_id),
               ""
               "Details from upstream follow.",
               "",
               "%s wrote:" % author.decode("utf8", "ignore"),
               ">  %s" % pr_title]
        if pr_msg:
            msg.append(">  ")
            msg.extend(">  %s" % line for line in pr_msg.split("\n"))
        return "\n".join(msg)

    @classmethod
    def has_metadata(cls, message: bytes) -> bool:
        required_keys = ["wpt-commits",
                         "wpt-pr"]
        metadata = sync_commit.get_metadata(message)
        return all(item in metadata for item in required_keys)

    @property
    def landable_status(self) -> LandableStatus:
        if self.skip:
            return LandableStatus.skip
        if self.metadata_ready:
            return LandableStatus.ready
        if self.error:
            return LandableStatus.error

        return LandableStatus.missing_try_results

    @property
    def pr_head(self) -> WptCommit:
        """Head commit of the PR. Typically this is equal to
        self.wpt_commits.head but if the PR is rebased onto master
        then the head of the PR won't match the commit that actually
        merged unless it happens to be a fast-forward"""
        return sync_commit.WptCommit(self.git_wpt, "origin/pr/%s" % self.pr)

    @SyncProcess.error.setter  # type: ignore
    @mut()
    def error(self, value: str | None) -> Any | None:
        if self.pr:
            if value is not None:
                env.gh_wpt.add_labels(self.pr, "mozilla:gecko-blocked")
            elif self.error is not None:
                env.gh_wpt.remove_labels(self.pr, "mozilla:gecko-blocked")
        return SyncProcess.error.fset(self, value)  # type: ignore

    @property
    def pr_status(self):
        return self.data.get("pr-status", "open")

    @pr_status.setter  # type: ignore
    @mut()
    def pr_status(self, value):
        self.data["pr-status"] = value

    @property
    def notify_bugs(self) -> FrozenDict:
        return FrozenDict(**self.data.get("notify-bugs", {}))

    @notify_bugs.setter  # type: ignore
    @mut()
    def notify_bugs(self, value: FrozenDict) -> None:
        self.data["notify-bugs"] = value.as_dict()

    @property
    def next_action(self) -> DownstreamAction:
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
            next_action = self.try_rebase()
            if next_action:
                return next_action

        latest_try_push = self.latest_valid_try_push
        if (latest_try_push and not latest_try_push.taskgroup_id):
            if latest_try_push.status == "open":
                return DownstreamAction.wait_try
            elif latest_try_push.infra_fail:
                next_action = self.try_rebase()
                if next_action:
                    return next_action

        assert self.pr is not None
        pr = env.gh_wpt.get_pull(self.pr)
        if pr.merged:  # Wait till PR is merged to do anything
            if not latest_try_push:
                if self.requires_stability_try:
                    logger.debug("Sync for PR %s requires a stability try push" % self.pr)
                    return DownstreamAction.try_push_stability
                else:
                    return DownstreamAction.try_push

            if latest_try_push.status != "complete":
                return DownstreamAction.wait_try

            if self.requires_stability_try and not latest_try_push.stability:
                return DownstreamAction.try_push_stability

            # If we have infra failure, flag for human intervention. Retrying stability
            # runs would be very costly
            if latest_try_push.infra_fail:
                tasks = latest_try_push.tasks()
                if tasks is None:
                    return DownstreamAction.manual_fix

                # Check if we had any successful tests
                if tasks.has_completed_tests():
                    return DownstreamAction.ready
                else:
                    return DownstreamAction.manual_fix

            return DownstreamAction.ready
        else:
            return DownstreamAction.wait_upstream

    @property
    def metadata_ready(self) -> bool:
        return self.next_action == DownstreamAction.ready

    @property
    def results_notified(self) -> bool:
        return self.data.get("results-notified", False)

    @results_notified.setter  # type: ignore
    @mut()
    def results_notified(self, value):
        self.data["results-notified"] = value

    @property
    def skip(self) -> bool:
        return self.data.get("skip", False)

    @skip.setter  # type: ignore
    @mut()
    def skip(self, value: bool) -> None:
        self.data["skip"] = value

    @property
    def tried_to_rebase(self) -> bool:
        return self.data.get("tried_to_rebase", False)

    @tried_to_rebase.setter  # type: ignore
    @mut()
    def tried_to_rebase(self, value: bool) -> None:
        self.data["tried_to_rebase"] = value

    def try_rebase(self) -> DownstreamAction | None:
        if self.tried_to_rebase:
            return DownstreamAction.manual_fix
        else:
            try:
                logger.info("Rebasing onto %s" % self.gecko_integration_branch())
                self.tried_to_rebase = True
                self.gecko_rebase(self.gecko_integration_branch(), abort_on_fail=True)
                return None
            except AbortError:
                return DownstreamAction.manual_fix

    @property
    def wpt(self) -> WPT:
        git_work = self.wpt_worktree.get()
        git_work.git.reset(hard=True)
        return WPT(os.path.join(git_work.working_dir))

    @property
    def requires_try(self) -> bool:
        return not self.skip

    @property
    def requires_stability_try(self) -> bool:
        return self.requires_try and self.has_affected_tests_readonly

    @property
    def latest_valid_try_push(self) -> TryPush | None:
        """Try push for the current head of the PR, if any.

        In legacy cases we don't store the wpt-head for the try push
        so we always assume that any try push is valid"""

        latest_try_push = self.latest_try_push

        # Check if the try push is for the current PR head
        if (latest_try_push and
            latest_try_push.wpt_head and
            latest_try_push.wpt_head not in (self.pr_head.sha1, self.wpt_commits.head.sha1)):
            # If the try push isn't for the head of the PR or for the post merge head
            # then we need a new try push
            latest_try_push = None

        return latest_try_push

    @mut()
    def try_paths(self) -> Mapping[str, list[str]]:
        """Return a mapping of {test_type: path} for tests that should be run on try.

        Paths are relative to the gecko root"""
        affected_tests = self.affected_tests()
        base_path = env.config["gecko"]["path"]["wpt"]
        # Filter out paths that aren't in the head.
        # This can happen if the files were moved in a previous PR that we haven't yet
        # merged
        affected_paths = {}

        head_tree = self.gecko_commits.head.commit.tree

        def contains(tree: Tree, path: str) -> bool:
            path_parts = path.split(os.path.sep)
            for part in path_parts:
                try:
                    tree = tree[part]
                except KeyError:
                    return False
            return True

        for test_type, wpt_paths in affected_tests.items():
            paths = []
            for path in wpt_paths:
                gecko_path = os.path.join(base_path, path)
                if contains(head_tree, gecko_path):
                    paths.append(gecko_path)
            if paths:
                affected_paths[test_type] = paths

        if not affected_paths:
            # Default to just running infra tests
            infra_path = os.path.join(base_path, "infrastructure/")
            affected_paths = {
                "testharness": [infra_path],
                "reftest": [infra_path],
                "wdspec": [infra_path],
            }
        return affected_paths

    @mut()
    def next_try_push(self, try_cls: type = trypush.TryFuzzyCommit) -> TryPush | None:
        """Schedule a new try push for the sync, if required.

        A stability try push will only be scheduled if the upstream PR is
        approved, which we check directly from GitHub. Therefore returning
        None is not an indication that the sync is ready to land, just that
        there's no further action at this time.
        """
        if not self.requires_try or self.status != "open":
            return None

        self.update_commits()
        # Ensure affected tests is up to date
        self.affected_tests()

        action = self.next_action
        if action == DownstreamAction.try_push:
            return TryPush.create(self._lock,
                                  self,
                                  affected_tests=self.try_paths(),
                                  stability=False,
                                  hacks=False,
                                  try_cls=try_cls)
        elif action == DownstreamAction.try_push_stability:
            return TryPush.create(self._lock,
                                  self,
                                  affected_tests=self.try_paths(),
                                  stability=True,
                                  hacks=False,
                                  try_cls=try_cls)
        return None

    @mut()
    def create_bug(self, git_wpt: Repo, pr_id: int, pr_title: str, pr_body: str | None) -> None:
        if self.bug is not None:
            return
        comment = self.make_bug_comment(git_wpt, pr_id, pr_title, pr_body)
        summary = f"[wpt-sync] Sync PR {pr_id} - {pr_title}"
        if len(summary) > 255:
            summary = summary[:254] + "\u2026"
        bug = env.bz.new(summary=summary,
                         comment=comment,
                         product="Testing",
                         component="web-platform-tests",
                         whiteboard="[wptsync downstream]",
                         priority="P4",
                         url=env.gh_wpt.pr_url(pr_id))
        self.bug = bug  # type: ignore

    @mut()
    def update_wpt_commits(self) -> None:
        """Update the set of commits in the PR from the latest upstream."""
        if not self.wpt_commits.head or self.wpt_commits.head.sha1 != self.pr_head.sha1:
            self.wpt_commits.head = self.pr_head  # type: ignore

        if (len(self.wpt_commits) == 0 and
            self.git_wpt.is_ancestor(self.wpt_commits.head.commit,
                                     self.git_wpt.rev_parse("origin/master"))):
            # The commits landed on master so we need to change the commit
            # range to not use origin/master as a base
            base_commit = None
            assert isinstance(self.wpt_commits.head, sync_commit.WptCommit)
            assert self.wpt_commits.head.pr() == self.pr
            for commit in self.git_wpt.iter_commits(self.wpt_commits.head.sha1):
                wpt_commit = sync_commit.WptCommit(self.git_wpt, commit)
                if wpt_commit.pr() != self.pr:
                    base_commit = wpt_commit
                    break
            assert base_commit is not None
            self.data["wpt-base"] = base_commit.sha1
            self.wpt_commits.base = base_commit  # type: ignore

    @mut()
    def update_github_check(self) -> None:
        if not env.config["web-platform-tests"]["github"]["checks"]["enabled"]:
            return
        title = "gecko/sync"
        head_sha = self.wpt_commits.head.sha1

        # TODO: maybe just get this from GitHub rather than store it
        existing = self.data.get("check")
        check_id = None
        if existing is not None:
            if existing.get("sha1") == head_sha:
                check_id = existing.get("id")

        assert self.bug is not None
        url = env.bz.bugzilla_url(self.bug)
        external_id = str(self.bug)

        # For now hardcode the status at completed
        status = "completed"
        conclusion = "neutral"

        completed_at = datetime.now()

        output = {"title": "Gecko sync for PR %s" % self.pr,
                  "summary": "Gecko sync status: %s" % self.landable_status.reason_str(),
                  "test": self.build_check_text(head_sha)}

        try:
            logger.info("Generating GH check status")
            resp = env.gh_wpt.set_check(title, check_id=check_id, commit_sha=head_sha, url=url,
                                        external_id=external_id, status=status, started_at=None,
                                        conclusion=conclusion, completed_at=completed_at,
                                        output=output)
            self.data["check"] = {"id": resp["id"], "sha1": head_sha}
        except AssertionError:
            raise
        except Exception:
            # Just log errors trying to update the check status, but otherwise don't fail
            newrelic.agent.record_exception()
            import traceback
            logger.error("Creating PR status check failed")
            logger.error(traceback.format_exc())

    def build_check_text(self, commit_sha: str) -> str:
        text = """

        # Summary

        [Bugzilla](%(bug_link)s)

        # Try pushes
        %(try_push_section)s

        %(error_section)s
        """
        try_pushes = [try_push for try_push in
                      sorted(self.try_pushes(), key=lambda x: -x.process_name.seq_id)
                      if try_push.wpt_head == commit_sha]
        if not try_pushes:
            try_push_section = "No current try pushes"
        else:
            items = []
            for try_push in try_pushes:
                link_str = "Try push" + (" (stability)" if try_push.stability else "")
                items.append(" * [{}]({}): {}{}".format(link_str,
                                                        try_push.treeherder_url,
                                                        try_push.status,
                                                        " infra-fail" if try_push.infra_fail
                                                        else ""))
                try_push_section = "\n".join(items)

        error_section = "# Errors:\n ```%s```" % self.error if self.error else ""
        assert self.bug is not None
        return text % {"bug_link": env.bz.bugzilla_url(self.bug),
                       "try_push_section": try_push_section,
                       "error_section": error_section}

    def files_changed(self) -> set[str]:
        # TODO: Would be nice to do this from mach with a gecko worktree
        return set(self.wpt.files_changed().decode("utf8", "replace").split("\n"))

    @property
    def metadata_commit(self) -> GeckoCommit | None:
        if len(self.gecko_commits) == 0:
            return None
        if self.gecko_commits[-1].metadata.get("wpt-type") == "metadata":
            commit = self.gecko_commits[-1]
            assert isinstance(commit, GeckoCommit)
            return commit
        return None

    @mut()
    def ensure_metadata_commit(self) -> GeckoCommit:
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
                "wpt-pr": str(self.pr),
                "wpt-type": "metadata"
            }
            msg = sync_commit.Commit.make_commit_msg(
                b"Bug %s [wpt PR %s] - Update wpt metadata, a=testonly" %
                (str(self.bug).encode("utf8") if self.bug is not None else b"None",
                 str(self.pr).encode("utf8") if self.pr is not None else b"None"),
                metadata)
            sync_commit.create_commit(git_work, msg, allow_empty=True)
        commit = git_work.commit("HEAD")
        return sync_commit.GeckoCommit(self.git_gecko, commit.hexsha)

    @mut()
    def set_bug_component(self, files_changed: set[str]) -> None:
        new_component = bugcomponents.get(self.gecko_worktree.get(),
                                          files_changed,
                                          default=("Testing", "web-platform-tests"))
        env.bz.set_component(self.bug, *new_component)

    @mut()
    def move_metadata(self, renames: dict[str, str]) -> None:
        if not renames:
            return

        self.ensure_metadata_commit()

        gecko_work = self.gecko_worktree.get()
        metadata_base = env.config["gecko"]["path"]["meta"]
        for old_path, new_path in renames.items():
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
    def update_bug_components(self, renames: dict[str, str]) -> None:
        if not renames:
            return

        self.ensure_metadata_commit()

        gecko_work = self.gecko_worktree.get()
        bugcomponents.update(gecko_work, renames)

        self._commit_metadata()

    @mut()
    def _commit_metadata(self, amend: bool = True) -> None:
        assert self.metadata_commit
        gecko_work = self.gecko_worktree.get()
        if gecko_work.is_dirty():
            logger.info("Updating metadata commit")
            try:
                gecko_work.git.commit(amend=True, no_edit=True)
            except git.GitCommandError as e:
                if amend and e.status == 1 and "--allow-empty" in e.stdout:
                    logger.warning("Amending commit made it empty, resetting")
                    gecko_work.git.reset("HEAD^")

    @mut()
    def update_commits(self) -> bool:
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
                            assert isinstance(revert_lock, SyncLock)
                            with revert_sync.as_mut(revert_lock):
                                revert_sync.skip = True  # type: ignore
                # TODO: If this commit reverts some closed syncs, then set the metadata
                # commit of this commit to the revert of the metadata commit from that
                # sync
                if all_open:
                    logger.info("Sync was a revert of other open syncs, skipping")
                    self.skip = True  # type: ignore
                    return False

            old_gecko_head = self.gecko_commits.head.sha1
            logger.debug(f"PR {self.pr} gecko HEAD was {old_gecko_head}")

            def plain_apply() -> bool:
                logger.info("Applying on top of the current commits")
                self.wpt_to_gecko_commits()
                return True

            def rebase_apply() -> bool:
                logger.info("Applying with a rebase onto latest integration branch")
                new_base = self.gecko_integration_branch()
                gecko_work = self.gecko_worktree.get()
                reset_head = "HEAD"
                if (len(self.gecko_commits) > 0 and
                    self.gecko_commits[0].metadata.get("wpt-type") == "dependent"):
                    # If we have any dependent commits first reset to the new
                    # head. This prevents conflicts if the dependents already
                    # landed
                    # TODO: Actually check if they landed?
                    reset_head = new_base
                gecko_work.git.reset(reset_head, hard=True)
                self.gecko_rebase(new_base, abort_on_fail=True)
                self.wpt_to_gecko_commits()
                return True

            def dependents_apply() -> bool:
                logger.info("Applying with upstream dependents")
                dependencies = self.unlanded_commits_same_files()
                if dependencies:
                    logger.info("Found dependencies:\n%s" %
                                "\n".join(item.msg.splitlines()[0].decode("utf8", "replace")
                                          for item in dependencies))
                    self.wpt_to_gecko_commits(dependencies)
                    assert self.bug is not None
                    env.bz.comment(self.bug,
                                   "PR %s applied with additional changes from upstream: %s"
                                   % (self.pr, ", ".join(item.sha1 for item in dependencies)))
                    return True
                return False

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
                    logger.error(traceback.format_exc())

            if error is not None:
                raise error

            logger.debug(f"PR {self.pr} gecko HEAD now {self.gecko_commits.head.sha1}")
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
    def wpt_to_gecko_commits(self, dependencies: list[WptCommit] | None = None) -> None:
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
            expected_commits: list[tuple[str, WptCommit | None, bool]] = [
                (item.sha1, item, True)
                for item in dependencies]
        else:
            # If no dependencies are supplied, retain the ones that we alredy have, if any
            expected_commits = []
            for commit in self.gecko_commits:
                assert isinstance(commit, sync_commit.GeckoCommit)
                if commit.metadata.get("wpt-type") == "dependency":
                    expected_commits.append((commit.metadata["wpt-commit"], None, True))
                else:
                    break

        # Expect all the new commits
        for commit in self.wpt_commits:
            assert isinstance(commit, WptCommit)
            if not commit.is_merge:
                expected_commits.append((commit.sha1, commit, False))

        existing = [
            commit for commit in self.gecko_commits
            if commit.metadata.get("wpt-commit") and
            commit.metadata.get("wpt-type") in ("dependency", None)]
        if TYPE_CHECKING:
            existing_commits = cast(List[GeckoCommit], existing)
        else:
            existing_commits = existing

        retain_commits = 0
        for gecko_commit, (wpt_sha1, _, _) in zip(existing_commits, expected_commits):
            if gecko_commit.metadata.get("wpt-commit") != wpt_sha1:
                break
            retain_commits += 1

        keep_commits = existing_commits[:retain_commits]
        maybe_add_commits = expected_commits[retain_commits:]

        # Strip out any leading commits that come from currently applied dependencies that are
        # not being retained
        strip_count = 0
        for _, wpt_commit, _ in maybe_add_commits:
            if wpt_commit is not None:
                break
            strip_count += 1
        add_commits = maybe_add_commits[strip_count:]

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
            reset_head = self.data["gecko-base"]
        elif len(keep_commits) < len(existing_commits):
            reset_head = keep_commits[-1]
        elif ("metadata-commit" in self.data and
              self.gecko_commits[-1].metadata.get("wpt-type") == "metadata"):
            reset_head = self.gecko_commits[-2]

        # Clear the set of affected tests since there are updates
        del self.data["affected-tests"]

        gecko_work = self.gecko_worktree.get()

        if reset_head:
            self.gecko_commits.head = reset_head  # type: ignore
        gecko_work.git.reset(hard=True)

        for _, wpt_commit, is_dependency in add_commits:
            assert wpt_commit is not None
            logger.info("Moving commit %s" % wpt_commit.sha1)
            if is_dependency:
                metadata = {
                    "wpt-type": "dependency",
                    "wpt-commit": wpt_commit.sha1
                }
                msg_filter = None
            else:
                metadata = {
                    "wpt-pr": str(self.pr),
                    "wpt-commit": wpt_commit.sha1
                }
                msg_filter = self.message_filter

            wpt_commit.move(gecko_work,
                            dest_prefix=env.config["gecko"]["path"]["wpt"],
                            msg_filter=msg_filter,
                            metadata=metadata,
                            patch_fallback=True)

    def unlanded_commits_same_files(self) -> list[WptCommit]:
        from . import landing

        sync_point = landing.load_sync_point(self.git_gecko, self.git_wpt)
        base = sync_point["upstream"]
        head = "origin/master"
        changed = self.wpt_commits.files_changed
        commits = []
        for commit in self.git_wpt.iter_commits(f"{base}..{head}",
                                                reverse=True,
                                                paths=list(changed)):
            wpt_commit = sync_commit.WptCommit(self.git_wpt, commit)
            # Check for same-pr rather than same-commit because we always
            # use the commits on the PR branch, not the merged commits.
            # The other option is to use the GH API to decide if the PR
            # merged and if so what the merge commit was, although in that
            # case we would still not know the commit prior to merge, which
            # is what we need
            if wpt_commit.pr() == self.pr:
                break
            commits.append(wpt_commit)
        return commits

    def message_filter(self, msg: bytes) -> tuple[bytes, dict[str, str]]:
        msg = sync_commit.try_filter(msg)
        parts = msg.split(b"\n", 1)
        if len(parts) > 1:
            summary, body = parts
        else:
            summary = parts[0]
            body = b""

        new_msg = b"Bug %s [wpt PR %s] - %s, a=testonly\n\nSKIP_BMO_CHECK\n%s" % (
            str(self.bug).encode("utf8"),
            str(self.pr).encode("utf8"),
            summary,
            body)
        return new_msg, {}

    @mut()
    def affected_tests(self, revish: Any | None = None) -> Mapping[str, list[str]]:
        # TODO? support files, harness changes -- don't want to update metadata
        if "affected-tests" not in self.data:
            tests_by_type: MutableMapping[str, list[str]] = defaultdict(list)
            logger.info("Updating MANIFEST.json")
            self.wpt.manifest()
            args = ["--show-type", "--new"]
            if revish:
                args.append(revish)
            logger.info("Getting a list of tests affected by changes.")
            try:
                output = self.wpt.tests_affected(*args)
            except subprocess.CalledProcessError:
                logger.error("Calling wpt tests-affected failed")
                return tests_by_type
            if output:
                for item_bytes in output.strip().split(b"\n"):
                    item = item_bytes.decode("utf8", "replace")
                    path, test_type = item.strip().split("\t")
                    tests_by_type[test_type].append(path)
            self.data["affected-tests"] = tests_by_type
        return self.data["affected-tests"]

    @property
    def affected_tests_readonly(self) -> Mapping[str, list[str]]:
        if "affected-tests" not in self.data:
            logger.warning("Trying to get affected tests before it's set")
            return {}
        return self.data["affected-tests"]

    @property
    def has_affected_tests_readonly(self) -> bool:
        if "affected-tests" not in self.data:
            logger.warning("Trying to get affected tests before it's set")
            return True
        return bool(self.data["affected-tests"])

    @mut()
    def update_metadata(self, log_files, stability=False):
        meta_path = env.config["gecko"]["path"]["meta"]
        gecko_work = self.gecko_worktree.get()

        mach = Mach(gecko_work.working_dir)
        args = []

        if stability:
            help_text = mach.wpt_update("--help").decode("utf8")
            if "--stability " in help_text:
                args.extend(["--stability", "wpt-sync Bug %s" % self.bug])
            else:
                args.append("--update-intermittent")
        args.extend(log_files)

        logger.debug("Updating metadata")
        output = mach.wpt_update(*args)
        prefix = b"disabled:"
        disabled = []
        for line in output.split(b"\n"):
            if line.startswith(prefix):
                disabled.append(line[len(prefix):].decode("utf8", "replace").strip())

        if gecko_work.is_dirty(untracked_files=True, path=meta_path):
            self.ensure_metadata_commit()
            gecko_work.git.add(meta_path, all=True)
            self._commit_metadata()

        return disabled

    @mut()
    def try_notify(self, force: bool = False) -> None:
        newrelic.agent.record_custom_event("try_notify", params={
            "sync_bug": self.bug,
            "sync_pr": self.pr
        })

        if self.results_notified and not force:
            return

        if not self.bug:
            logger.error("Sync for PR %s has no associated bug" % self.pr)
            return

        if not self.affected_tests():
            logger.debug("PR %s doesn't have affected tests so skipping results notification" %
                         self.pr)
            newrelic.agent.record_custom_event("try_notify_no_affected", params={
                "sync_bug": self.bug,
                "sync_pr": self.pr
            })
            return

        logger.info("Trying to generate results notification for PR %s" % self.pr)

        results = notify.results.for_sync(self)

        if not results:
            # TODO handle errors here better, perhaps
            logger.error("Failed to get results notification for PR %s" % self.pr)
            newrelic.agent.record_custom_event("try_notify_failed", params={
                "sync_bug": self.bug,
                "sync_pr": self.pr
            })
            return

        message, truncated = notify.msg.for_results(results)

        with env.bz.bug_ctx(self.bug) as bug:
            if truncated:
                bug.add_attachment(data=message.encode("utf8"),
                                   file_name="wpt-results.md",
                                   summary="Notable wpt changes",
                                   is_markdown=True,
                                   comment=truncated)
            else:
                env.bz.comment(self.bug, message, is_markdown=True)

        bugs = notify.bugs.for_sync(self, results)
        notify.bugs.update_metadata(self, bugs)

        self.results_notified = True  # type: ignore

        with SyncLock.for_process(self.process_name) as lock:
            assert isinstance(lock, SyncLock)
            for try_push in self.try_pushes():
                with try_push.as_mut(lock):
                    try_push.cleanup_logs()

    def reverts_syncs(self) -> set[DownstreamSync]:
        """Return a set containing the previous syncs reverted by this one, if any"""
        revert_re = re.compile(b"This reverts commit ([0-9A-Fa-f]+)")
        unreverted_commits = defaultdict(set)
        for commit in self.wpt_commits:
            if not commit.msg.startswith(b"Revert "):
                # If not everything is a revert then return
                return set()
            revert_shas = revert_re.findall(commit.msg)
            if len(revert_shas) == 0:
                return set()
            # Just use the first match for now
            sha = revert_shas[0].decode("ascii")
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
            assert isinstance(sync, DownstreamSync)
            if sync not in unreverted_commits:
                # Ensure we have the latest commits for the reverted sync
                with SyncLock.for_process(sync.process_name) as revert_lock:
                    assert isinstance(revert_lock, SyncLock)
                    with sync.as_mut(revert_lock):
                        sync.update_wpt_commits()
                unreverted_commits[sync] = {item.sha1 for item in sync.wpt_commits}
            if sha in unreverted_commits[sync]:
                unreverted_commits[sync].remove(sha)

        rv = {sync for sync, unreverted in unreverted_commits.items()
              if not unreverted}
        return rv


@entry_point("downstream")
def new_wpt_pr(git_gecko: Repo, git_wpt: Repo, pr_data: Mapping[str, Any],
               raise_on_error: bool = True, repo_update: bool = True) -> None:
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
                sync.update_github_check()
            except Exception as e:
                sync.error = e
                if raise_on_error:
                    raise
                traceback.print_exc()
                logger.error(e)
            # Now wait for the status to change before we take any actions


@entry_point("downstream")
@mut("try_push", "sync")
def try_push_complete(git_gecko, git_wpt, try_push, sync):
    if not try_push.taskgroup_id:
        logger.error("No taskgroup id set for try push")
        return

    if not try_push.status == "complete":
        # Ensure we don't have some old set of tasks

        tasks = try_push.tasks()
        if not tasks.complete(allow_unscheduled=True):
            logger.info("Try push %s is not complete" % try_push.treeherder_url)
            return
        logger.info("Try push %s is complete" % try_push.treeherder_url)
        try:
            if not tasks.validate():
                try_push.infra_fail = True
                if len(sync.latest_busted_try_pushes()) > 5:
                    message = ("Too many busted try pushes. "
                               "Check the try results for infrastructure issues.")
                    sync.error = message
                    env.bz.comment(sync.bug, message)
                    try_push.status = "complete"
                    raise AbortError(message)
            elif len(tasks.failed_builds()):
                message = ("Try push had build failures")
                sync.error = message
                env.bz.comment(sync.bug, message)
                try_push.status = "complete"
                try_push.infra_fail = True
                raise AbortError(message)
            else:
                logger.info(f"Try push {try_push!r} for PR {sync.pr} complete")
                disabled = []
                if tasks.has_failures():
                    if sync.affected_tests():
                        log_files = []
                        wpt_tasks = try_push.download_logs(tasks.wpt_tasks)
                        for task in wpt_tasks:
                            for run in task.get("status", {}).get("runs", []):
                                log = run.get("_log_paths", {}).get("wptreport.json")
                                if log:
                                    log_files.append(log)
                        if not log_files:
                            raise ValueError("No log files found for try push %r" % try_push)
                        disabled = sync.update_metadata(log_files, stability=try_push.stability)
                    else:
                        env.bz.comment(sync.bug, ("The PR was not expected to affect any tests, "
                                                  "but the try push wasn't a success. "
                                                  "Check the try results for infrastructure "
                                                  "issues"))
                        # TODO: consider marking the push an error here so that we can't
                        # land without manual intervention

                if try_push.stability and disabled:
                    logger.info("The following tests were disabled:\n%s" % "\n".join(disabled))
                    # TODO notify relevant people about test expectation changes, stability
                    env.bz.comment(sync.bug, ("The following tests were disabled "
                                              "based on stability try push:\n %s" %
                                              "\n".join(disabled)))

            try_push.status = "complete"
            sync.next_try_push()
        finally:
            sync.update_github_check()
    else:
        sync.next_try_push()
        sync.update_github_check()

    if sync.landable_status == LandableStatus.ready:
        sync.try_notify()


@entry_point("downstream")
@mut("sync")
def update_pr(git_gecko: Repo,
              git_wpt: Repo,
              sync: DownstreamSync,
              action: str,
              merge_sha: str,
              base_sha: str,
              merged_by: str | None = None,
              ) -> None:
    try:
        if action == "closed" and not merge_sha:
            sync.pr_status = "closed"  # type: ignore
            if sync.bug:
                env.bz.set_status(sync.bug, "RESOLVED", "INVALID")
            sync.finish()
        elif action == "closed":
            # We are storing the wpt base as a reference
            sync.data["wpt-base"] = base_sha
            sync.next_try_push()
        elif action == "reopened" or action == "open":
            sync.status = "open"  # type: ignore
            sync.pr_status = "open"  # type: ignore
            sync.next_try_push()
            assert sync.bug is not None
            if sync.bug:
                status = env.bz.get_status(sync.bug)
                if status is not None and status[0] == "RESOLVED":
                    env.bz.set_status(sync.bug, "REOPENED")
        sync.update_github_check()
    except Exception as e:
        sync.error = e
        raise
