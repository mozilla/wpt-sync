from __future__ import annotations
import os
import shutil
from collections import defaultdict

import enum
import git
from celery.exceptions import OperationalError
import six

from . import bug
from . import bugcomponents
from . import commit as sync_commit
from . import downstream
from . import gitutils
from . import log
from . import tree
from . import load
from . import repos
from . import trypush
from . import upstream
from .base import entry_point
from .commit import GeckoCommit, first_non_merge
from .env import Environment
from .gitutils import update_repositories
from .lock import SyncLock, constructor, mut
from .errors import AbortError, RetryableError
from .projectutil import Mach
from .repos import cinnabar, pygit2_get
from .sync import LandableStatus, SyncProcess

from typing import Any, IO, List, Tuple, Union, cast, TYPE_CHECKING
from git.repo.base import Repo
if TYPE_CHECKING:
    from sync.base import ProcessName
    from sync.commit import Commit, WptCommit
    from sync.downstream import DownstreamSync
    from sync.gh import AttrDict
    from sync.trypush import TryPush, TryPushTasks
    from sync.upstream import UpstreamSync

    LandableCommits = List[Tuple[int, Union[DownstreamSync, UpstreamSync], List[WptCommit]]]

env = Environment()

logger = log.get_logger(__name__)


class SyncPoint:
    def __init__(self, data: Any | None = None) -> None:
        self._items: dict[str, str] = {}
        if data is not None:
            self._items.update(data)

    def __getitem__(self, key: str) -> str:
        return self._items[key]

    def __setitem__(self, key: str, value: str) -> None:
        self._items[key] = value

    def load(self, fp):
        with open(fp, "rb") as f:
            self.loads(f)

    def loads(self, data: bytes) -> None:
        for line in data.split(b"\n"):
            if line:
                key, value = (item.decode("utf8") for item in line.split(b": ", 1))
                self._items[key] = value

    def dump(self, fp: IO[bytes]) -> None:
        fp.write(self.dumps().encode("utf8") + b"\n")

    def dumps(self) -> str:
        return "\n".join(f"{key}: {value}" for key, value in self._items.items())


@enum.unique
class TryPushResult(enum.Enum):
    success = 0
    acceptable_failures = 1
    infra_fail = 2
    too_many_failures = 3
    pending = 4

    def is_failure(self) -> bool:
        return self in (TryPushResult.infra_fail, TryPushResult.too_many_failures)

    def is_ok(self) -> bool:
        return self in (TryPushResult.success, TryPushResult.acceptable_failures)


class LandingSync(SyncProcess):
    sync_type = "landing"
    obj_id = "bug"
    statuses = ("open", "complete")
    status_transitions = [("open", "complete"), ("complete", "open")]

    def __init__(self, git_gecko: Repo, git_wpt: Repo, process_name: ProcessName) -> None:
        super().__init__(git_gecko, git_wpt, process_name)
        self._unlanded_gecko_commits = None

    @classmethod
    @constructor(lambda args: ("landing", None))
    def new(cls,
            lock: SyncLock,
            git_gecko: Repo,
            git_wpt: Repo,
            wpt_base: str,
            wpt_head: str,
            bug: int | None = None,
            ) -> LandingSync:
        # There is some chance here we create a bug but never create the branch.
        # Probably need something to clean up orphan bugs

        # The gecko branch is a new one based on master
        gecko_base = cls.gecko_integration_branch()
        gecko_head = cls.gecko_integration_branch()

        if bug is None:
            bug = env.bz.new("Update web-platform-tests to %s" % wpt_head,
                             "",
                             "Testing",
                             "web-platform-tests",
                             whiteboard="[wptsync landing]")

        return super().new(lock,
                           git_gecko,
                           git_wpt,
                           gecko_base,
                           gecko_head,
                           wpt_base=wpt_base,
                           wpt_head=wpt_head,
                           bug=bug)

    @classmethod
    def has_metadata(cls, message: bytes) -> bool:
        required_keys = ["wpt-head",
                         "wpt-type"]
        metadata = sync_commit.get_metadata(message)
        return (all(item in metadata for item in required_keys) and
                metadata.get("wpt-type") == "landing")

    def unlanded_gecko_commits(self):
        """Get a list of gecko commits that correspond to commits which have
        landed on the gecko integration branch, but are not yet merged into the
        upstream commit we are updating to.

        There are two possible sources of such commits:
          * Unlanded PRs. These correspond to upstream syncs with status of "open"
          * Gecko PRs that landed between the wpt commit that we are syncing to
            and latest upstream master.

        :return: List of commits in the order in which they originally landed in gecko"""

        if self._unlanded_gecko_commits is None:
            commits = []

            def on_integration_branch(commit):
                # Calling this continually is O(N*M) where N is the number of unlanded commits
                # and M is the average depth of the commit in the gecko tree
                # If we need a faster implementation one approach would be to store all the
                # commits not on the integration branch and check if this commit is in that set
                return self.git_gecko.is_ancestor(commit,
                                                  self.git_gecko.rev_parse(
                                                      self.gecko_integration_branch()))

            # All the commits from unlanded upstream syncs that are reachable from the
            # integration branch
            unlanded_syncs = set()
            for status in ["open", "wpt-merged"]:
                unlanded_syncs |= set(upstream.UpstreamSync.load_by_status(self.git_gecko,
                                                                           self.git_wpt,
                                                                           status))

            for sync in unlanded_syncs:
                branch_commits = [commit.sha1 for commit in sync.gecko_commits if
                                  on_integration_branch(commit)]
                if branch_commits:
                    logger.info("Commits from unlanded sync for bug %s (PR %s) will be reapplied" %
                                (sync.bug, sync.pr))
                    commits.extend(branch_commits)

            # All the gecko commits that landed between the base sync point and master
            # We take the base here and then remove upstreamed commits that we are landing
            # as we reach them so that we can get the right diffs for the other PRs
            unlanded_commits = self.git_wpt.iter_commits("%s..origin/master" %
                                                         self.wpt_commits.base.sha1)
            seen_bugs = set()
            for commit in unlanded_commits:
                wpt_commit = sync_commit.WptCommit(self.git_wpt, commit)
                gecko_commit = wpt_commit.metadata.get("gecko-commit")
                if gecko_commit:
                    git_sha = cinnabar(self.git_gecko).hg2git(gecko_commit)
                    commit = sync_commit.GeckoCommit(self.git_gecko, git_sha)
                    bug_number = bug.bug_number_from_url(commit.metadata.get("bugzilla-url"))
                    if on_integration_branch(commit):
                        if bug_number and bug_number not in seen_bugs:
                            logger.info("Commits from landed sync for bug %s will be reapplied" %
                                        bug_number)
                            seen_bugs.add(bug_number)
                        commits.append(commit.sha1)

            commits = set(commits)

            # Order the commits according to the order in which they landed in gecko
            ordered_commits = []
            for commit in self.git_gecko.iter_commits(self.gecko_integration_branch(),
                                                      paths=env.config["gecko"]["path"]["wpt"]):
                if commit.hexsha in commits:
                    ordered_commits.append(commit.hexsha)
                    commits.remove(commit.hexsha)
                if not commits:
                    break

            self._unlanded_gecko_commits = list(reversed(
                [sync_commit.GeckoCommit(self.git_gecko, item) for item in ordered_commits]))
        return self._unlanded_gecko_commits

    def has_metadata_for_sync(self, sync: DownstreamSync) -> bool:
        for item in reversed(list(self.gecko_commits)):
            if (item.metadata.get("wpt-pr") == sync.pr and
                item.metadata.get("wpt-type") == "metadata"):
                return True
        return False

    @property
    def landing_commit(self) -> Any | None:
        head = self.gecko_commits.head
        if (head.metadata.get("wpt-type") == "landing" and
            head.metadata.get("wpt-head") == self.wpt_commits.head.sha1):
            return head
        return None

    @mut()
    def add_pr(self,
               pr_id: int,
               sync: DownstreamSync | UpstreamSync,
               wpt_commits: list[WptCommit],
               copy: bool = True,
               prev_wpt_head: str | None = None,
               ) -> Commit | None:
        if len(wpt_commits) > 1:
            assert all(item.pr() == pr_id for item in wpt_commits)

        # Assume we can always use the author of the first commit
        author = first_non_merge(wpt_commits).author

        git_work_wpt = self.wpt_worktree.get()
        git_work_gecko = self.gecko_worktree.get()

        pr = env.gh_wpt.get_pull(int(pr_id))

        metadata = {
            "wpt-pr": pr_id,
            "wpt-commits": ", ".join(item.sha1 for item in wpt_commits)
        }

        message = b"""Bug %d [wpt PR %d] - %s, a=testonly

Automatic update from web-platform-tests\n%s
"""
        message = message % ((sync and sync.bug) or self.bug,
                             pr.number,
                             pr.title.encode("utf8"),
                             b"\n--\n".join(item.msg for item in wpt_commits) + b"\n--")

        message = sync_commit.try_filter(message)

        upstream_changed = set()
        diffs = wpt_commits[-1].commit.diff(wpt_commits[0].commit.parents[0])
        for diff in diffs:
            new_path = diff.b_path
            if new_path:
                upstream_changed.add(new_path)

        logger.info("Upstream files changed:\n%s" % "\n".join(sorted(upstream_changed)))

        # If this is originally an UpstreamSync and no new changes were introduced to the GH PR
        # then we can safely skip and not need to re-apply these changes. Compare the hash of
        # the upstreamed gecko commits against the final hash in the PR.
        if isinstance(sync, upstream.UpstreamSync):
            commit_is_local = False
            pr_head = sync.pr_head
            if sync.wpt_commits.head.sha1 == pr_head:
                commit_is_local = True
            else:
                # Check if we rebased locally without pushing the rebase;
                # this is a thing we used to do to check the PR would merge
                ref_log = sync.git_wpt.references[sync.branch_name].log()
                commit_is_local = any(entry.newhexsha == pr_head for entry in ref_log)
            if commit_is_local:
                logger.info("Upstream sync doesn't introduce any gecko changes")
                return None

        if copy:
            commit = self.copy_pr(git_work_gecko, git_work_wpt, pr, wpt_commits,
                                  message, author, metadata)
        else:
            commit = self.move_pr(git_work_gecko, git_work_wpt, pr, wpt_commits,
                                  message, author, prev_wpt_head, metadata)

        if commit is not None:
            self.gecko_commits.head = commit  # type: ignore

        return commit

    @mut()
    def copy_pr(self, git_work_gecko, git_work_wpt, pr, wpt_commits, message, author, metadata):
        # Ensure we have anything in a wpt submodule
        git_work_wpt.git.submodule("update", "--init", "--recursive")

        dest_path = os.path.join(git_work_gecko.working_dir,
                                 env.config["gecko"]["path"]["wpt"])
        src_path = git_work_wpt.working_dir

        # Specific paths that should be re-checked out
        keep_paths = {"LICENSE", "resources/testdriver_vendor.js"}
        # file names that are ignored in any part of the tree
        ignore_files = {".git"}

        logger.info("Setting wpt HEAD to %s" % wpt_commits[-1].sha1)
        git_work_wpt.head.reference = wpt_commits[-1].commit
        git_work_wpt.head.reset(index=True, working_tree=True)

        # First remove all files so we handle deletion correctly
        shutil.rmtree(dest_path)

        ignore_paths = defaultdict(set)
        for name in keep_paths:
            src, name = os.path.split(os.path.join(src_path, name))
            ignore_paths[src].add(name)

        def ignore_names(src, names):
            rv = []
            for item in names:
                if item in ignore_files:
                    rv.append(item)
            if src in ignore_paths:
                rv.extend(ignore_paths[src])
            return rv

        shutil.copytree(src_path,
                        dest_path,
                        ignore=ignore_names)

        # Now re-checkout the files we don't want to change
        # checkout-index allows us to ignore files that don't exist
        git_work_gecko.git.checkout_index(*(os.path.join(env.config["gecko"]["path"]["wpt"], item)
                                            for item in keep_paths), force=True, quiet=True)

        allow_empty = False
        if not git_work_gecko.is_dirty(untracked_files=True):
            logger.info("PR %s didn't add any changes" % pr.number)
            allow_empty = True

        git_work_gecko.git.add(env.config["gecko"]["path"]["wpt"],
                               no_ignore_removal=True)

        message = sync_commit.Commit.make_commit_msg(message, metadata)

        commit = git_work_gecko.index.commit(message=message,
                                             author=git.Actor._from_string(author))
        logger.debug("Gecko files changed: \n%s" % "\n".join(list(commit.stats.files.keys())))
        gecko_commit = sync_commit.GeckoCommit(self.git_gecko,
                                               commit.hexsha,
                                               allow_empty=allow_empty)

        return gecko_commit

    @mut()
    def move_pr(self,
                git_work_gecko: Repo,
                git_work_wpt: Repo,
                pr: AttrDict,
                wpt_commits: list[WptCommit],
                message: bytes,
                author: str,
                prev_wpt_head: str,
                metadata: dict[str, str],
                ) -> Commit | None:
        if prev_wpt_head is None:
            if wpt_commits[-1].is_merge:
                base = wpt_commits[-1].sha1 + "^"
            else:
                base = wpt_commits[0].sha1 + "^"
        else:
            base = self.git_wpt.git.merge_base(prev_wpt_head, wpt_commits[-1].sha1)

        head = sync_commit.GeckoCommit(self.git_gecko, git_work_gecko.head.commit)
        if (head.is_downstream and
            head.metadata.get("wpt-pr") == six.ensure_text(str(pr.number))):
            return None

        revish = f"{base}..{wpt_commits[-1].sha1}"
        logger.info("Moving wpt commits %s" % revish)

        return sync_commit.move_commits(self.git_wpt,
                                        revish,
                                        message,
                                        git_work_gecko,
                                        dest_prefix=env.config["gecko"]["path"]["wpt"],
                                        amend=False,
                                        metadata=metadata,
                                        rev_name="pr-%s" % pr.number,
                                        author=first_non_merge(wpt_commits).author,
                                        exclude={"resources/testdriver_vendor.js"},
                                        allow_empty=True)

    @mut()
    def reapply_local_commits(self, gecko_commits_landed):
        # The local commits to apply are everything that hasn't been landed at this
        # point in the process
        commits = [item for item in self.unlanded_gecko_commits()
                   if item.canonical_rev not in gecko_commits_landed]

        landing_commit = self.gecko_commits[-1]
        git_work_gecko = self.gecko_worktree.get()

        logger.debug("Reapplying commits: %s" % " ".join(item.canonical_rev for item in commits))

        if not commits:
            return

        already_applied = landing_commit.metadata.get("reapplied-commits")
        if already_applied:
            already_applied = [item.strip() for item in already_applied.split(",")]
        else:
            already_applied = []
        already_applied_set = set(already_applied)

        unapplied_gecko_commits = [item for item in commits if item.canonical_rev
                                   not in already_applied_set]

        try:
            for i, commit in enumerate(unapplied_gecko_commits):
                def msg_filter(_):
                    msg = landing_commit.msg
                    reapplied_commits = (already_applied +
                                         [commit.canonical_rev for commit in commits[:i + 1]])
                    metadata = {"reapplied-commits": ", ".join(reapplied_commits)}
                    return msg, metadata
                logger.info(f"Reapplying {commit.sha1} - {commit.msg}")
                # Passing in a src_prefix here means that we only generate a patch for the
                # part of the commit that affects wpt, but then we need to undo it by adding
                # the same dest prefix
                commit = commit.move(git_work_gecko,
                                     msg_filter=msg_filter,
                                     src_prefix=env.config["gecko"]["path"]["wpt"],
                                     dest_prefix=env.config["gecko"]["path"]["wpt"],
                                     three_way=True,
                                     amend=True)
                if commit is None:
                    break

        except AbortError as e:
            err_msg = (
                f"Landing wpt failed because reapplying commits failed:\n{e.message}"
            )
            env.bz.comment(self.bug, err_msg)
            raise AbortError(err_msg)

    @mut()
    def add_metadata(self, sync: DownstreamSync) -> None:
        logger.info("Adding metadata from downstream sync")

        if self.has_metadata_for_sync(sync):
            logger.info("Metadata already applied for PR %s" % sync.pr)
            return

        if not sync.metadata_commit or sync.metadata_commit.is_empty():
            logger.info("No metadata commit available for PR %s" % sync.pr)
            return

        worktree = self.gecko_worktree.get()

        success = gitutils.cherry_pick(worktree, sync.metadata_commit.sha1)

        if not success:
            logger.info("Cherry-pick failed, trying again with only test-related changes")
            # Try to reset all metadata files that aren't related to an affected test.
            affected_metadata = {os.path.join(env.config["gecko"]["path"]["meta"], item) + ".ini"
                                 for items in sync.affected_tests_readonly.values()
                                 for item in items}
            checkout = []
            status = gitutils.status(worktree)
            for head_path, data in status.items():
                if data["code"] not in {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}:
                    # Only try to reset merge conflicts
                    continue
                path = data["rename"] if data["rename"] else head_path
                if path not in affected_metadata:
                    logger.debug("Resetting changes to %s" % head_path)
                    if data["code"] == "DU":
                        # Files that were deleted in master should just be removed
                        worktree.git.rm(head_path)
                    else:
                        checkout.append(head_path)
            logger.debug("Resetting changes to %s" % " ".join(checkout))
            try:
                worktree.git.checkout("HEAD", "--", *checkout)
                # Now try to commit again
                worktree.git.commit(c=sync.metadata_commit.sha1, no_edit=True)
                success = True
            except git.GitCommandError as e:
                if gitutils.handle_empty_commit(worktree, e):
                    return
                if sync.skip:
                    return
                success = False

        if not success:
            try:
                logger.info("Cherry-pick had merge conflicts trying to automatically resolve")
                status = gitutils.status(worktree)
                for head_path, data in status.items():
                    if data["code"] in {"DD", "UD", "DU"}:
                        # Deleted by remote or local
                        # Could do better here and have the mergetool handle this case
                        logger.info("Removing %s which was deleted somewhere" % head_path)
                        worktree.git.rm(head_path)
                    if data["code"] in {"UA", "AU"}:
                        logger.info("Adding %s which was added somewhere" % head_path)
                        worktree.git.add(head_path)
                logger.info("Running mergetool")
                worktree.git.mergetool(tool="metamerge",
                                       env={"MOZBUILD_STATE_PATH":
                                            repos.Gecko.get_state_path(env.config,
                                                                       worktree.working_dir)})
                worktree.git.commit(c=sync.metadata_commit.sha1, no_edit=True)
                worktree.git.clean(f=True)
                success = True
            except git.GitCommandError as e:
                if gitutils.handle_empty_commit(worktree, e):
                    return
                if sync.skip:
                    return
                logger.error("Failed trying to use mergetool to resolve conflicts")
                raise

        metadata_commit = sync_commit.GeckoCommit(worktree, worktree.head.commit)
        if metadata_commit.msg.startswith(b"Bug None"):
            # If the metadata commit didn't get a valid bug number for some reason,
            # we want to replace the placeholder bug number with the
            # either the sync or landing bug number, otherwise the push will be
            # rejected
            bug_number = sync.bug or self.bug
            new_message = b"Bug %s%s" % (str(bug_number).encode("utf8"),
                                         metadata_commit.msg[len(b"Bug None"):])
            sync_commit.create_commit(worktree, new_message, amend=True)

    @mut()
    def apply_prs(self, prev_wpt_head: str, landable_commits: LandableCommits) -> None:
        """Main entry point to setting the commits for landing.

        For each upstream PR we want to create a separate commit in the
        gecko repository so that we are preserving a useful subset of the history.
        We also want to prevent divergence from upstream. So for each PR that landed
        upstream since our last sync, we take the following steps:
        1) Copy the state of upstream at the commit where the PR landed over to
           the gecko repo
        2) Reapply any commits that have been made to gecko on the integration branch
           but which are not yet landed upstream on top of the PR
        3) Apply any updated metadata from the downstream sync for the PR.
        """

        last_pr = None
        has_metadata = False
        have_prs = set()
        # Find the last gecko commit containing a PR
        if len(self.gecko_commits):
            head_commit = self.gecko_commits.head
            if TYPE_CHECKING:
                head = cast(GeckoCommit, head_commit)
            else:
                head = head_commit
            if head.is_landing:
                return

            for commit in list(self.gecko_commits):
                assert isinstance(commit, GeckoCommit)
                if commit.metadata.get("wpt-pr") is not None:
                    last_pr = int(commit.metadata["wpt-pr"])
                    has_metadata = commit.metadata.get("wpt-type") == "metadata"
                    have_prs.add(last_pr)

        pr_count_applied = len(have_prs)

        gecko_commits_landed = set()

        def update_gecko_landed(sync: DownstreamSync | UpstreamSync,
                                commits: list[WptCommit]) -> None:
            if isinstance(sync, upstream.UpstreamSync):
                for commit in commits:
                    gecko_commit = commit.metadata.get("gecko-commit")
                    if gecko_commit:
                        gecko_commits_landed.add(gecko_commit)

        unapplied_commits = []
        pr_count_upstream_empty = 0
        last_applied_seen = last_pr is None
        for i, (pr, sync, commits) in enumerate(landable_commits):
            if last_applied_seen:
                unapplied_commits.append((i, (pr, sync, commits, False)))
            else:
                prev_wpt_head = commits[-1].sha1
                try:
                    have_prs.remove(pr)
                except KeyError:
                    if isinstance(sync, downstream.DownstreamSync):
                        # This could be wrong if the changes already landed in gecko for some reason
                        raise AbortError("Expected an existing gecko commit for PR %s, "
                                         "but not found" % (pr,))
                    pr_count_upstream_empty += 1
                    continue
                if pr == last_pr:
                    last_applied_seen = True
                    if not has_metadata:
                        unapplied_commits.append((i, (pr, sync, commits, True)))
            update_gecko_landed(sync, commits)

        if have_prs:
            raise AbortError("Found unexpected gecko commit for PRs %s"
                             % (", ".join(str(item) for item in have_prs),))

        pr_count_unapplied = len(unapplied_commits)
        if pr_count_applied and not has_metadata:
            # If we have seen the commit but not the metadata it will both be in
            # have_prs and unapplied_commits, so avoid double counting
            pr_count_unapplied -= 1

        if (pr_count_applied + pr_count_upstream_empty + pr_count_unapplied !=
            len(landable_commits)):
            raise AbortError("PR counts don't match; got %d applied, %d unapplied %d upstream"
                             "(total %s), expected total %d" %
                             (pr_count_applied,
                              pr_count_unapplied,
                              pr_count_upstream_empty,
                              pr_count_unapplied + pr_count_applied + pr_count_upstream_empty,
                              len(landable_commits)))

        for i, (pr, sync, commits, meta_only) in unapplied_commits:
            logger.info("Applying PR %i of %i" % (i + 1, len(landable_commits)))
            update_gecko_landed(sync, commits)

            # If copy is set then we copy the commits and reapply in-progress upstream
            # syncs. This is currently always disabled, but the intent was to do this for
            # the first commit to ensure that the possible drift from upstream was limited.
            # However there were some difficulties reapplying all the right commits, so it's
            # disabled until this is worked out.
            # To reenable it change the below line to
            # copy = i == 0
            copy = False
            pr_commit: Commit | None = None
            if not meta_only:
                # If we haven't applied it before then create the initial commit
                pr_commit = self.add_pr(pr, sync, commits, prev_wpt_head=prev_wpt_head,
                                        copy=copy)
            prev_wpt_head = commits[-1].sha1
            if pr_commit:
                if copy:
                    self.reapply_local_commits(gecko_commits_landed)
            if isinstance(sync, downstream.DownstreamSync):
                self.add_metadata(sync)

    @mut()
    def update_landing_commit(self) -> GeckoCommit:
        git_work = self.gecko_worktree.get()
        if not self.landing_commit:
            metadata = {
                "wpt-type": "landing",
                "wpt-head": self.wpt_commits.head.sha1
            }
            msg = sync_commit.Commit.make_commit_msg(
                b"""Bug %s - [wpt-sync] Update web-platform-tests to %s, a=testonly

MANUAL PUSH: wpt sync bot
                """ %
                (str(self.bug).encode("utf8"), self.wpt_commits.head.sha1.encode("utf8")),
                metadata)
            sync_commit.create_commit(git_work, msg, allow_empty=True)
        else:
            sync_commit.create_commit(git_work,
                                      self.landing_commit.msg,
                                      allow_empty=True,
                                      amend=True,
                                      no_edit=True)
        rv = self.gecko_commits[-1]
        assert isinstance(rv, GeckoCommit)
        return rv

    @mut()
    def update_bug_components(self) -> None:
        renames = self.wpt_renames()
        if renames is None:
            return

        gecko_work = self.gecko_worktree.get()
        mozbuild_path = bugcomponents.mozbuild_path(gecko_work)
        if not os.path.exists(mozbuild_path):
            return

        bugcomponents.update(gecko_work, renames)

        if gecko_work.is_dirty(path=mozbuild_path):
            gecko_work.git.add(mozbuild_path, all=True)
            self.update_landing_commit()

    @mut()
    def update_metadata(self, log_files: list, update_intermittents: bool = False) -> None:
        """Update the web-platform-tests metadata based on the logs
        generated in a try run.

        :param log_files: List of paths to the raw logs from the try run
        """
        # TODO: this shares a lot of code with downstreaming
        meta_path = env.config["gecko"]["path"]["meta"]

        gecko_work = self.gecko_worktree.get()
        mach = Mach(gecko_work.working_dir)
        logger.info("Updating metadata from %s logs" % len(log_files))
        args = ["--full"]
        if update_intermittents:
            args.append("--update-intermittent")
        args.extend(log_files)
        mach.wpt_update(*args)

        if gecko_work.is_dirty(untracked_files=True, path=meta_path):
            gecko_work.git.add(meta_path, all=True)
            self.update_landing_commit()
            gecko_work.git.reset(hard=True)

    @mut()
    def update_sync_point(self, sync_point: SyncPoint) -> None:
        """Update the in-tree record of the last sync point."""
        new_sha1 = self.wpt_commits.head.sha1
        if sync_point["upstream"] == new_sha1:
            return
        sync_point["upstream"] = new_sha1
        gecko_work = self.gecko_worktree.get()
        with open(os.path.join(gecko_work.working_dir,
                               env.config["gecko"]["path"]["meta"],
                               "mozilla-sync"), "wb") as f:
            sync_point.dump(f)
        if gecko_work.is_dirty():
            gecko_work.index.add([os.path.join(env.config["gecko"]["path"]["meta"],
                                               "mozilla-sync")])
            self.update_landing_commit()

    @mut()
    def next_try_push(self, retry: bool = False) -> TryPush | None:
        if self.status != "open":
            return None

        latest_try_push = self.latest_try_push
        stability = False

        if latest_try_push:
            if latest_try_push.status != "complete":
                return None
            elif latest_try_push.stability and not retry:
                return None

        if retry:
            stability = latest_try_push.stability if latest_try_push is not None else False
        else:
            stability = (latest_try_push is not None and
                         not latest_try_push.infra_fail)

        return trypush.TryPush.create(
            self._lock,
            self,
            hacks=False,
            stability=stability,
            rebuild_count=0,
            try_cls=trypush.TryFuzzyCommit,
            disable_target_task_filter=True,
            artifact=not stability,
            queries=["web-platform-tests !ccov !shippable",
                     "web-platform-tests linux-32 shippable",
                     "web-platform-tests mac !debug shippable"])

    def try_result(self, try_push: TryPush = None,
                   tasks: TryPushTasks | None = None) -> TryPushResult:
        """Determine whether a try push has infra failures, or an acceptable
        level of test passes for the current build"""
        if try_push is None:
            try_push = self.latest_try_push
        if try_push is None:
            raise ValueError("No try push found")
        target_success_rate = 0.5 if not try_push.stability else 0.8

        if try_push.infra_fail and not try_push.accept_failures:
            return TryPushResult.infra_fail
        if tasks is None:
            tasks = try_push.tasks()
        if tasks is None:
            # This can happen if the taskgroup_id is not yet set
            return TryPushResult.pending
        if not tasks.complete(allow_unscheduled=True):
            return TryPushResult.pending
        if tasks.success():
            return TryPushResult.success
        if tasks.failed_builds() and not try_push.accept_failures:
            return TryPushResult.infra_fail
        if (tasks.failure_limit_exceeded(target_success_rate) and
            not try_push.accept_failures):
            return TryPushResult.too_many_failures
        return TryPushResult.acceptable_failures


def push(landing: LandingSync) -> None:
    """Push from git_work_gecko to inbound."""
    success = False

    landing_tree = env.config["gecko"]["landing"]

    old_head = None
    err = None
    assert landing.bug is not None
    while not success:
        try:
            logger.info("Rebasing onto %s" % landing.gecko_integration_branch())
            landing.gecko_rebase(landing.gecko_integration_branch())
        except AbortError as e:
            logger.error(e)
            env.bz.comment(landing.bug, six.ensure_text(str(e)))
            raise e

        if old_head == landing.gecko_commits.head.sha1:
            err = ("Landing push failed and rebase didn't change head:%s" %
                   ("\n%s" % err if err else ""))
            logger.error(err)
            env.bz.comment(landing.bug, err)
            raise AbortError(err)
        old_head = landing.gecko_commits.head.sha1

        if not tree.is_open(landing_tree):
            logger.info("%s is closed" % landing_tree)
            raise RetryableError(AbortError("Tree is closed"))

        try:
            logger.info("Pushing landing")
            landing.git_gecko.remotes.mozilla.push(
                "{}:{}".format(landing.branch_name,
                               landing.gecko_integration_branch().split("/", 1)[1]))
        except git.GitCommandError as e:
            changes = landing.git_gecko.remotes.mozilla.fetch()
            err = "Pushing update to remote failed:\n%s" % e
            if not changes:
                logger.error(err)
                env.bz.comment(landing.bug, err)
                raise AbortError(err)
        else:
            success = True
    # The landing is marked as finished when it reaches central


def unlanded_with_type(git_gecko, git_wpt, wpt_head, prev_wpt_head):
    pr_commits = unlanded_wpt_commits_by_pr(git_gecko,
                                            git_wpt,
                                            wpt_head or prev_wpt_head,
                                            "origin/master")
    for pr, commits in pr_commits:
        if pr is None:
            status = LandableStatus.no_pr
        else:
            sync = load.get_pr_sync(git_gecko, git_wpt, pr, log=False)
            if sync is None:
                status = LandableStatus.no_sync
            elif isinstance(sync, upstream.UpstreamSync):
                status = LandableStatus.upstream
            else:
                assert isinstance(sync, downstream.DownstreamSync)
                status = sync.landable_status
        yield (pr, commits, status)


def load_sync_point(git_gecko: Repo, git_wpt: Repo) -> SyncPoint:
    """Read the last sync point from the batch sync process"""
    pygit2_repo = pygit2_get(git_gecko)
    integration_sha = pygit2_repo.revparse_single(LandingSync.gecko_integration_branch()).id
    blob_id = pygit2_repo[integration_sha].tree["testing/web-platform/meta/mozilla-sync"].id
    mozilla_data = pygit2_repo[blob_id].data
    sync_point = SyncPoint()
    sync_point.loads(mozilla_data)
    return sync_point


def unlanded_wpt_commits_by_pr(git_gecko: Repo,
                               git_wpt: Repo,
                               prev_wpt_head: str,
                               wpt_head: str = "origin/master",
                               ) -> list[tuple[int | None, list[WptCommit]]]:
    revish = f"{prev_wpt_head}..{wpt_head}"

    commits_by_pr: list[tuple[int | None, list[WptCommit]]] = []
    index_by_pr: dict[int | None, int] = {}

    for commit in git_wpt.iter_commits(revish,
                                       reverse=True,
                                       first_parent=True):
        wpt_commit = sync_commit.WptCommit(git_wpt, commit.hexsha)
        pr = wpt_commit.pr()
        extra_commits = []
        if pr not in index_by_pr:
            pr_data: tuple[int | None, list[WptCommit]] = (pr, [])
            # If we have a merge commit, also get the commits merged in
            if len(commit.parents) > 1:
                merged_revish = f"{commit.commit.parents[0].hexsha}..{commit.sha1}"
                for merged_commit in git_wpt.iter_commits(merged_revish,
                                                          reverse=True):
                    if merged_commit.hexsha != commit.sha1:
                        wpt_commit = sync_commit.WptCommit(git_wpt, merged_commit.hexsha)
                        if wpt_commit.pr() == pr:
                            extra_commits.append(wpt_commit)
        else:
            idx = index_by_pr[pr]
            pr_data = commits_by_pr.pop(idx)
            assert pr_data[0] == pr
            index_by_pr = {key: (value if value < idx else value - 1)
                           for key, value in index_by_pr.items()}
        for c in extra_commits + [wpt_commit]:
            pr_data[1].append(c)
        commits_by_pr.append(pr_data)
        index_by_pr[pr] = len(commits_by_pr) - 1

    return commits_by_pr


def landable_commits(git_gecko: Repo,
                     git_wpt: Repo,
                     prev_wpt_head: str,
                     wpt_head: str | None = None,
                     include_incomplete: bool = False
                     ) -> tuple[str, LandableCommits] | None:
    """Get the list of commits that are able to land.

    :param prev_wpt_head: The sha1 of the previous wpt commit landed to gecko.
    :param wpt_head: The sha1 of the latest possible commit to land to gecko,
                     or None to use the head of the master branch
    :param include_incomplete: By default we don't attempt to land anything that
                               hasn't completed a metadata update. This flag disables
                               that and just lands everything up to the specified commit."""
    if wpt_head is None:
        wpt_head = "origin/master"
    pr_commits = unlanded_wpt_commits_by_pr(git_gecko, git_wpt, prev_wpt_head, wpt_head)
    landable_commits = []
    for pr, commits in pr_commits:
        last = False
        if not pr:
            # Assume this was some trivial fixup:
            continue

        first_commit = first_non_merge(commits)
        if not first_commit:
            # If we only have a merge commit just use that; it doesn't come from gecko anyway
            first_commit = commits[-1]

        def upstream_sync(bug_number):
            syncs = upstream.UpstreamSync.for_bug(git_gecko,
                                                  git_wpt,
                                                  bug_number,
                                                  flat=True)
            for sync in syncs:
                if sync.merge_sha == commits[-1].sha1 and not sync.wpt_commits:
                    # TODO: this shouldn't be mutating here
                    with SyncLock("upstream", None) as lock:
                        assert isinstance(lock, SyncLock)
                        with sync.as_mut(lock):
                            # If we merged with a merge commit, the set of commits
                            # here will be empty
                            sync.set_wpt_base(sync_commit.WptCommit(git_wpt,
                                                                    commits[0].sha1 + "~").sha1)

                # Only check the first commit since later ones could be added in the PR
                sync_revs = {item.canonical_rev for item in sync.upstreamed_gecko_commits}
                if any(commit.metadata.get("gecko-commit") in sync_revs for commit in commits):
                    break
            else:
                sync = None
            return sync

        sync = None
        sync = load.get_pr_sync(git_gecko, git_wpt, pr)
        if isinstance(sync, downstream.DownstreamSync):
            if sync and "affected-tests" in sync.data and sync.data["affected-tests"] is None:
                del sync.data["affected-tests"]
        if not include_incomplete:
            if not sync:
                # TODO: schedule a downstream sync for this pr
                logger.info("PR %s has no corresponding sync" % pr)
                last = True
            elif (isinstance(sync, downstream.DownstreamSync) and
                  sync.landable_status not in (LandableStatus.ready, LandableStatus.skip)):
                logger.info(f"PR {pr}: {sync.landable_status.reason_str()}")
                last = True
            if last:
                break
        assert isinstance(sync, (upstream.UpstreamSync, downstream.DownstreamSync))
        landable_commits.append((pr, sync, commits))

    if not landable_commits:
        logger.info("No new commits are landable")
        return None

    wpt_head = landable_commits[-1][2][-1].sha1
    logger.info("Landing up to commit %s" % wpt_head)

    return wpt_head, landable_commits


def current(git_gecko: Repo, git_wpt: Repo) -> LandingSync | None:
    landings = LandingSync.load_by_status(git_gecko, git_wpt, "open")
    if len(landings) > 1:
        raise ValueError("Multiple open landing branches")
    if landings:
        landing = landings.pop()
        assert isinstance(landing, LandingSync)
        return landing
    return None


@entry_point("landing")
def wpt_push(git_gecko: Repo, git_wpt: Repo, commits: list[str],
             create_missing: bool = True) -> None:
    prs = set()
    for commit_sha in commits:
        # This causes the PR to be recorded as a note
        commit = sync_commit.WptCommit(git_wpt, commit_sha)
        pr = commit.pr()
        if pr is not None and not upstream.UpstreamSync.has_metadata(commit.msg):
            prs.add(pr)
    if create_missing:
        for pr in prs:
            from . import update
            sync = load.get_pr_sync(git_gecko, git_wpt, pr)
            if not sync:
                # If we don't have a sync for this PR create one
                # It's easiest just to go via the GH API here
                pr_data = env.gh_wpt.get_pull(pr)
                update.update_pr(git_gecko, git_wpt, pr_data)


@entry_point("landing")
def update_landing(git_gecko: Repo,
                   git_wpt: Repo,
                   prev_wpt_head: Any | None = None,
                   new_wpt_head: Any | None = None,
                   include_incomplete: bool = False,
                   retry: bool = False,
                   allow_push: bool = True,
                   accept_failures: bool = False,
                   ) -> LandingSync | None:
    """Create or continue a landing of wpt commits to gecko.

    :param prev_wpt_head: The sha1 of the previous wpt commit landed to gecko.
    :param wpt_head: The sha1 of the latest possible commit to land to gecko,
                     or None to use the head of the master branch"
    :param include_incomplete: By default we don't attempt to land anything that
                               hasn't completed a metadata update. This flag disables
                               that and just lands everything up to the specified commit.
    :param retry: Create a new try push for the landing even if there's an existing one
    :param allow_push: Allow pushing to gecko if try is complete
    :param accept_failures: Don't fail if an existing try push has too many failures """
    landing = current(git_gecko, git_wpt)
    sync_point = load_sync_point(git_gecko, git_wpt)

    with SyncLock("landing", None) as lock:
        assert isinstance(lock, SyncLock)
        if landing is None:
            update_repositories(git_gecko, git_wpt)
            if prev_wpt_head is None:
                prev_wpt_head = sync_point["upstream"]

            landable = landable_commits(git_gecko, git_wpt,
                                        prev_wpt_head,
                                        wpt_head=new_wpt_head,
                                        include_incomplete=include_incomplete)
            if landable is None:
                return None
            wpt_head, commits = landable
            landing = LandingSync.new(lock, git_gecko, git_wpt, prev_wpt_head, wpt_head)

            # Set the landing to block all the bugs that will land with it
            blocks = [sync.bug for (pr_, sync, commits_) in commits
                      if isinstance(sync, downstream.DownstreamSync) and
                      sync.bug is not None]
            assert landing.bug is not None
            with env.bz.bug_ctx(landing.bug) as bug:
                for bug_id in blocks:
                    bug.add_blocks(bug_id)
        else:
            if prev_wpt_head and landing.wpt_commits.base.sha1 != prev_wpt_head:
                raise AbortError("Existing landing base commit %s doesn't match"
                                 "supplied previous wpt head %s" % (landing.wpt_commits.base.sha1,
                                                                    prev_wpt_head))
            elif new_wpt_head and landing.wpt_commits.head.sha1 != new_wpt_head:
                raise AbortError("Existing landing head commit %s doesn't match"
                                 "supplied wpt head %s" % (landing.wpt_commits.head.sha1,
                                                           new_wpt_head))
            head = landing.gecko_commits.head
            if git_gecko.is_ancestor(head.commit,
                                     git_gecko.rev_parse(
                                         env.config["gecko"]["refs"]["central"])):
                logger.info("Landing reached central")
                with landing.as_mut(lock):
                    landing.finish()
                return None
            elif git_gecko.is_ancestor(head.commit,
                                       git_gecko.rev_parse(landing.gecko_integration_branch())):
                logger.info("Landing is on inbound but not yet on central")
                return None

            landable = landable_commits(git_gecko,
                                        git_wpt,
                                        landing.wpt_commits.base.sha1,
                                        landing.wpt_commits.head.sha1,
                                        include_incomplete=include_incomplete)
            if landable is None:
                raise AbortError("No new commits are landable")
            wpt_head, commits = landable
            assert wpt_head == landing.wpt_commits.head.sha1

        pushed = False

        with landing.as_mut(lock):
            if landing.latest_try_push is None:
                landing.apply_prs(prev_wpt_head, commits)

                landing.update_bug_components()

                landing.update_sync_point(sync_point)

                landing.next_try_push()
            elif retry:
                try:
                    landing.gecko_rebase(landing.gecko_landing_branch())
                except AbortError:
                    message = record_rebase_failure(landing)
                    raise AbortError(message)

                with landing.latest_try_push.as_mut(lock):
                    landing.latest_try_push.status = "complete"  # type: ignore
                landing.next_try_push(retry=True)
            else:
                try_push = landing.latest_try_push
                try_result = landing.try_result()
                if try_push.status == "complete" and (try_result.is_ok() or
                                                      accept_failures):
                    try:
                        landing.gecko_rebase(landing.gecko_landing_branch())
                    except AbortError:
                        message = record_rebase_failure(landing)
                        raise AbortError(message)

                    if landing.next_try_push() is None:
                        push_to_gecko(git_gecko, git_wpt, landing, allow_push)
                        pushed = True
                elif try_result == TryPushResult.pending:
                    logger.info("Existing try push %s is waiting for try results" %
                                try_push.treeherder_url)
                else:
                    logger.info("Existing try push %s requires manual fixup" %
                                try_push.treeherder_url)

        try_notify_downstream(commits, landing_is_complete=pushed)

        if pushed:
            retrigger()
    return landing


def retrigger() -> None:
    try:
        # Avoid circular import
        from . import tasks
        tasks.retrigger.apply_async()
    except OperationalError:
        logger.warning("Failed to retrigger blocked syncs")


@entry_point("landing")
@mut('try_push', 'sync')
def try_push_complete(git_gecko: Repo,
                      git_wpt: Repo,
                      try_push: TryPush,
                      sync: LandingSync,
                      allow_push: bool = True,
                      accept_failures: bool = False,
                      tasks: Any | None = None,
                      ) -> None:
    """Run after all jobs in a try push are complete.

    This function handles updating the metadata based on the try push, or scheduling
    more jobs. In the case that the metadata has been updated successfully, the try
    push is marked as complete. If there's an error e.g. an infrastructure failure
    the try push is not marked as complete; user action is required to complete the
    handling of the try push (either by passing in accept_failures=True to indicate
    that the failure is not significant or by retyring the try push in which case the
    existing one will be marked as complete)."""

    if try_push.status == "complete":
        logger.warning("Called try_push_complete on a completed try push")
        return None

    if accept_failures:
        try_push.accept_failures = True  # type: ignore

    if tasks is None:
        tasks = try_push.tasks()

    if tasks is None:
        logger.error("Taskgroup id is not yet set")
        return None

    try_result = sync.try_result(tasks=tasks)

    if try_result == TryPushResult.pending:
        logger.info("Try push results are pending")
        return None

    if not try_result == TryPushResult.success:
        if try_result.is_failure():
            if try_result == TryPushResult.infra_fail:
                message = record_build_failures(sync, try_push)
                try_push.infra_fail = True  # type: ignore
                raise AbortError(message)
            elif try_result == TryPushResult.too_many_failures and not try_push.stability:
                message = record_too_many_failures(sync, try_push)
                raise AbortError(message)

        if not try_push.stability:
            update_metadata(sync, try_push)
        else:
            retriggered = tasks.retriggered_wpt_states()
            if not retriggered:
                if try_result == TryPushResult.too_many_failures:
                    record_too_many_failures(sync, try_push)
                    try_push.status = "complete"  # type: ignore
                    return None
                num_new_jobs = tasks.retrigger_failures()
                logger.info(f"{num_new_jobs} new tasks scheduled on try for {sync.bug}")
                if num_new_jobs:
                    assert sync.bug is not None
                    env.bz.comment(sync.bug,
                                   ("Retriggered failing web-platform-test tasks on "
                                    "try before final metadata update."))
                    return None

            update_metadata(sync, try_push, tasks)

    try_push.status = "complete"  # type: ignore

    if try_result == TryPushResult.infra_fail:
        record_infra_fail(sync, try_push)
        return None

    update_landing(git_gecko, git_wpt, allow_push=allow_push)


def needinfo_users() -> list[str]:
    needinfo_users = [item.strip() for item in
                      (env.config["gecko"]["needinfo"]
                       .get("landing", "")
                       .split(","))]
    return [item for item in needinfo_users if item]


def record_failure(sync: LandingSync, log_msg: str, bug_msg: str,
                   fixup_msg: Any | None = None) -> str:
    if fixup_msg is None:
        fixup_msg = "Run `wptsync landing` with either --accept-failures or --retry"
    logger.error(f"Bug {sync.bug}:{log_msg}\n{fixup_msg}")
    sync.error = log_msg  # type: ignore
    assert sync.bug is not None
    with env.bz.bug_ctx(sync.bug) as bug:
        bug.add_comment(f"{bug_msg}\nThis requires fixup from a wpt sync admin.")
        bug.needinfo(*needinfo_users())
    return log_msg


def record_build_failures(sync, try_push):
    log_msg = f"build failures in try push {try_push.treeherder_url}"
    bug_msg = f"Landing failed due to build failures in try push {try_push.treeherder_url}"
    return record_failure(sync, log_msg, bug_msg)


def record_too_many_failures(sync: LandingSync, try_push: TryPush) -> str:
    log_msg = f"too many test failures in try push {try_push.treeherder_url}"
    bug_msg = "Landing failed due to too many test failures in try push {}".format(
        try_push.treeherder_url)
    return record_failure(sync, log_msg, bug_msg)


def record_infra_fail(sync, try_push):
    log_msg = "infra failures in try push %s. " % (try_push.treeherder_url)
    bug_msg = "Landing failed due to infra failures in try push {}.".format(
        try_push.treeherder_url)
    return record_failure(sync, log_msg, bug_msg)


def record_rebase_failure(sync):
    log_msg = "rebase failed"
    bug_msg = "Landing failed due to conficts during rebase"
    fixup_msg = "Resolve the conflicts in the worktree and run `wptsync landing`"
    return record_failure(sync, log_msg, bug_msg, fixup_msg)


def update_metadata(sync: LandingSync, try_push: TryPush, tasks: TryPushTasks = None) -> None:
    if tasks is None:
        tasks = try_push.tasks()
    if tasks is None:
        raise AbortError("Try push has no taskgroup id set")
    wpt_tasks = try_push.download_logs(tasks.wpt_tasks)
    log_files = []
    for task in wpt_tasks:
        for run in task.get("status", {}).get("runs", []):
            log = run.get("_log_paths", {}).get("wptreport.json")
            if log:
                log_files.append(log)
    if not log_files:
        logger.warning("No log files found for try push %r" % try_push)
    sync.update_metadata(log_files, update_intermittents=True)


def push_to_gecko(git_gecko: Repo, git_wpt: Repo, sync: LandingSync,
                  allow_push: bool = True) -> None:
    if not allow_push:
        logger.info("Landing in bug %s is ready for push.\n"
                    "Working copy is in %s" % (sync.bug,
                                               sync.gecko_worktree.get().working_dir))
        return

    update_repositories(git_gecko, git_wpt)
    push(sync)


def try_notify_downstream(commits: Any, landing_is_complete: bool = False) -> None:
    for _, sync, _ in commits:
        if sync is not None:
            if isinstance(sync, downstream.DownstreamSync):
                with SyncLock.for_process(sync.process_name) as lock:
                    assert isinstance(lock, SyncLock)
                    with sync.as_mut(lock):
                        try:
                            if not sync.skip:
                                sync.try_notify()
                        except Exception as e:
                            logger.error(str(e))
                        finally:
                            if landing_is_complete:
                                sync.finish()
                                if sync.bug is not None and not sync.results_notified:
                                    env.bz.comment(sync.bug,
                                                   "Test result changes from PR not available.")


@entry_point("landing")
def gecko_push(git_gecko: Repo,
               git_wpt: Repo,
               repository_name: str,
               hg_rev: str,
               raise_on_error: bool = False,
               base_rev: Any | None = None,
               ) -> None:
    rev = git_gecko.rev_parse(cinnabar(git_gecko).hg2git(hg_rev))
    last_sync_point, base_commit = LandingSync.prev_gecko_commit(git_gecko,
                                                                 repository_name)

    if base_rev is None and git_gecko.is_ancestor(rev, base_commit.commit):
        logger.info("Last sync point moved past commit")
        return

    landed_central = repository_name == "mozilla-central"
    revish = f"{base_commit.sha1}..{rev.hexsha}"

    landing_sync = current(git_gecko, git_wpt)
    for commit in git_gecko.iter_commits(revish,
                                         reverse=True):
        gecko_commit = sync_commit.GeckoCommit(git_gecko, commit.hexsha)
        logger.debug("Processing commit %s" % gecko_commit.sha1)
        if landed_central and gecko_commit.is_landing:
            logger.info("Found wptsync landing in commit %s" % gecko_commit.sha1)
            if gecko_commit.bug is None:
                logger.error("Commit %s looked link a landing, but had no bug" %
                             gecko_commit.sha1)
                continue
            syncs = LandingSync.for_bug(git_gecko,
                                        git_wpt,
                                        gecko_commit.bug,
                                        statuses=None,
                                        flat=True)
            if syncs:
                sync = syncs[0]
                logger.info("Found sync %s" % sync.process_name)
                with SyncLock("landing", None) as lock:
                    assert isinstance(lock, SyncLock)
                    with syncs[0].as_mut(lock):
                        sync.finish()
            else:
                logger.error("Failed to find sync for commit")
        elif gecko_commit.is_backout:
            backed_out, _ = gecko_commit.landing_commits_backed_out()
            if backed_out:
                logger.info("Commit %s backs out wpt sync landings" % gecko_commit.sha1)
            for backed_out_commit in backed_out:
                bug = backed_out_commit.bug
                syncs = []
                if bug is not None:
                    syncs = LandingSync.for_bug(git_gecko, git_wpt, bug,
                                                statuses=None, flat=True)
                if syncs:
                    # TODO: should really check if commit is actually part of the sync if there's >1
                    # TODO: reopen landing? But that affects the invariant that there is only one
                    sync = syncs[0]
                    logger.info("Found sync %s" % sync.process_name)
                    with SyncLock("landing", None) as lock:
                        assert isinstance(lock, SyncLock)
                        with sync.as_mut(lock):
                            sync.error = "Landing was backed out"  # type: ignore
                else:
                    logger.error("Failed to find sync for commit")
        elif gecko_commit.is_downstream:
            syncs = []
            bug = gecko_commit.bug
            if bug is not None:
                syncs = LandingSync.for_bug(git_gecko, git_wpt, bug,
                                            statuses=None, flat=True)
            for sync in syncs:
                sync = syncs[0]
                with SyncLock("landing", None) as lock:
                    assert isinstance(lock, SyncLock)
                    with sync.as_mut(lock):
                        sync.finish()

    # TODO: Locking here
    with SyncLock("landing", None) as lock:
        assert isinstance(lock, SyncLock)
        with last_sync_point.as_mut(lock):
            assert last_sync_point.commit is not None
            if not git_gecko.is_ancestor(rev, last_sync_point.commit.commit):
                last_sync_point.commit = rev.hexsha  # type: ignore

    if landing_sync and landing_sync.status == "complete":
        start_next_landing()


def start_next_landing():
    from . import tasks
    tasks.land.apply_async()
