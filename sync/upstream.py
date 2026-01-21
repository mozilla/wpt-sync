from __future__ import annotations
import enum
import os
import re
import time
import traceback

import git
from bugsy.errors import BugsyException
from github import GithubException
from mozautomation import commitparser

from . import log
from . import commit as sync_commit
from .base import entry_point
from .commit import GeckoCommit
from .downstream import DownstreamSync
from .errors import AbortError
from .env import Environment
from .gitutils import update_repositories, gecko_repo
from .gh import AttrDict
from .lock import SyncLock, constructor, mut
from .sync import CommitFilter, LandableStatus, SyncProcess, CommitRange
from .repos import cinnabar, pygit2_get

from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
    cast,
    TYPE_CHECKING,
)
from git.repo.base import Repo

if TYPE_CHECKING:
    from sync.base import BranchRefObject, ProcessName
    from sync.commit import Commit

CreateSyncs = Dict[Optional[int], Union[List, "Endpoints"]]
UpdateSyncs = Dict[int, Tuple["UpstreamSync", GeckoCommit]]


env = Environment()

logger = log.get_logger(__name__)


class BackoutCommitFilter(CommitFilter):
    def __init__(self, bug_id: int) -> None:
        self.bug = bug_id
        self.seen: set[str] = set()
        self._commits = {}

    def _filter_commit(self, commit: Commit) -> bool:
        assert isinstance(commit, GeckoCommit)
        if commit.metadata.get("wptsync-skip"):
            return False
        if DownstreamSync.has_metadata(commit.msg):
            return False
        if commit.is_backout:
            commits, _ = commit.wpt_commits_backed_out()
            for backout_commit in commits:
                if backout_commit.sha1 in self.seen:
                    return True
        if commit.bug == self.bug:
            if commit.is_empty(env.config["gecko"]["path"]["wpt"]):
                return False
            self.seen.add(commit.sha1)
            return True
        return False

    def filter_commits(self, commits: Iterable[Commit]) -> Sequence[Commit]:
        return remove_complete_backouts(commits)


class UpstreamSync(SyncProcess):
    sync_type = "upstream"
    obj_id = "bug"
    statuses = ("open", "wpt-merged", "complete", "incomplete")
    status_transitions = [
        ("open", "wpt-merged"),
        ("open", "complete"),
        ("open", "incomplete"),
        ("incomplete", "open"),
        ("wpt-merged", "complete"),
    ]
    multiple_syncs = True

    def __init__(self, git_gecko: Repo, git_wpt: Repo, process_name: ProcessName) -> None:
        super().__init__(git_gecko, git_wpt, process_name)

        self._upstreamed_gecko_commits: list[GeckoCommit] | None = None
        self._upstreamed_gecko_head: str | None = None

    @classmethod
    @constructor(lambda args: ("upstream", args["bug"]))
    def new(
        cls,
        lock: SyncLock,
        git_gecko: Repo,
        git_wpt: Repo,
        gecko_base: str,
        gecko_head: str,
        wpt_base: str = "origin/master",
        wpt_head: str | None = None,
        bug: str | None = None,
        status: str = "open",
    ) -> UpstreamSync:
        self = super().new(
            lock,
            git_gecko,
            git_wpt,
            gecko_base,
            gecko_head,
            wpt_base=wpt_base,
            wpt_head=wpt_head,
            bug=bug,
            status=status,
        )
        with self.as_mut(lock):
            for commit in self.gecko_commits:
                commit.set_upstream_sync(self)
        return self

    @classmethod
    def from_pr(
        cls, lock: SyncLock, git_gecko: Repo, git_wpt: Repo, pr_id: int, body: str | None
    ) -> UpstreamSync | None:
        gecko_commits = []
        bug = None
        integration_branch = None

        if body is None or not cls.has_metadata(body.encode("utf8", "replace")):
            return None

        commits = env.gh_wpt.get_commits(pr_id)

        for gh_commit in commits:
            commit = sync_commit.WptCommit(git_wpt, gh_commit.sha)
            if cls.has_metadata(commit.msg):
                gecko_commits.append(cinnabar(git_gecko).hg2git(commit.metadata["gecko-commit"]))
                commit_bug = env.bz.id_from_url(commit.metadata["bugzilla-url"])
                if bug is not None and commit_bug != bug:
                    logger.error("Got multiple bug numbers in URL from commits")
                    break
                elif bug is None:
                    bug = commit_bug

                if (
                    integration_branch is not None
                    and commit.metadata["integration_branch"] != integration_branch
                ):
                    logger.warning("Got multiple integration branches from commits")
                elif integration_branch is None:
                    integration_branch = commit.metadata["integration_branch"]
            else:
                break

        if not gecko_commits:
            return None

        assert bug
        gecko_base = git_gecko.rev_parse("%s^" % gecko_commits[0])
        gecko_head = git_gecko.rev_parse(gecko_commits[-1])
        wpt_head = commits[-1].sha
        wpt_base = commits[0].sha

        return cls.new(
            lock, git_gecko, git_wpt, gecko_base, gecko_head, wpt_base, wpt_head, bug, pr_id
        )

    @classmethod
    def has_metadata(cls, message: bytes) -> bool:
        required_keys = ["gecko-commit", "bugzilla-url"]
        metadata = sync_commit.get_metadata(message)
        return all(item in metadata for item in required_keys)

    def gecko_commit_filter(self) -> BackoutCommitFilter:
        return BackoutCommitFilter(self.bug)

    @property
    def landable_status(self) -> LandableStatus:
        return LandableStatus.upstream

    @property
    def bug(self) -> int:
        return int(self.process_name.obj_id)

    @bug.setter
    @mut()
    def bug(self, value: int) -> None:
        raise AttributeError("Can't set attribute")

    @property
    def pr_status(self) -> str:
        return self.data.get("pr-status", "open")

    @pr_status.setter
    def pr_status(self, value: str) -> None:
        self.data["pr-status"] = value

    @property
    def merge_sha(self) -> str:
        return self.data.get("merge-sha", None)

    @merge_sha.setter
    def merge_sha(self, value: str | None) -> None:
        self.data["merge-sha"] = value

    @property
    def remote_branch(self) -> str | None:
        return self.data.get("remote-branch")

    @remote_branch.setter
    @mut()
    def remote_branch(self, value: str | None) -> None:
        if value:
            assert not value.startswith("refs/")
        self.data["remote-branch"] = value

    @mut()
    def get_or_create_remote_branch(self) -> str:
        if not self.remote_branch:
            pygit2_gecko = pygit2_get(self.git_gecko)
            pygit2_wpt = pygit2_get(self.git_wpt)
            if self.branch_name in pygit2_gecko.branches:
                upstream = pygit2_gecko.branches[self.branch_name].upstream
                if upstream:
                    self.remote_branch = upstream.branch_name

        if not self.remote_branch:
            count = 0
            refs = pygit2_wpt.references
            initial_path = path = "refs/remotes/origin/gecko/%s" % self.bug
            while path in refs:
                count += 1
                path = f"{initial_path}-{count}"
            self.remote_branch = path[len("refs/remotes/origin/") :]
        return self.remote_branch

    @property
    def upstreamed_gecko_commits(self) -> list[GeckoCommit]:
        if (
            self._upstreamed_gecko_commits is None
            or self._upstreamed_gecko_head != self.wpt_commits.head.sha1
        ):
            self._upstreamed_gecko_commits = [
                sync_commit.GeckoCommit(
                    self.git_gecko,
                    cinnabar(self.git_gecko).hg2git(wpt_commit.metadata["gecko-commit"]),
                )
                for wpt_commit in self.wpt_commits
                if "gecko-commit" in wpt_commit.metadata
            ]
            self._upstreamed_gecko_head = self.wpt_commits.head.sha1
        return self._upstreamed_gecko_commits

    @mut()
    def update_wpt_commits(self) -> bool:
        if len(self.gecko_commits) == 0:
            return False

        # Find the commits that were already upstreamed. Some gecko commits may not
        # result in an upstream commit, if the patch has no effect. But if we find
        # the last commit that was previously upstreamed then all earlier ones must
        # also match.
        upstreamed_commits = {item.sha1 for item in self.upstreamed_gecko_commits}
        matching_commits = list(self.gecko_commits[:])
        for gecko_commit in reversed(list(self.gecko_commits)):
            if gecko_commit.sha1 in upstreamed_commits:
                break
            matching_commits.pop()

        if len(matching_commits) == len(self.gecko_commits) == len(self.upstreamed_gecko_commits):
            return False

        if len(matching_commits) == 0:
            self.wpt_commits.head = self.wpt_commits.base
        elif len(matching_commits) < len(self.upstreamed_gecko_commits):
            self.wpt_commits.head = self.wpt_commits[len(matching_commits) - 1]

        # Ensure the worktree is clean
        wpt_work = self.wpt_worktree.get()
        wpt_work.git.reset(hard=True)
        wpt_work.git.clean(f=True, d=True, x=True)

        for commit in self.gecko_commits[len(matching_commits) :]:
            commit = self.add_commit(commit)

        assert len(self.wpt_commits) == len(self.upstreamed_gecko_commits)

        return True

    def gecko_landed(self) -> bool:
        if not len(self.gecko_commits):
            return False
        central_commit = self.git_gecko.rev_parse(env.config["gecko"]["refs"]["central"])
        landed = [
            self.git_gecko.is_ancestor(commit.commit, central_commit)
            for commit in self.gecko_commits
        ]
        if not all(item == landed[0] for item in landed):
            logger.warning(
                "Got some commits landed and some not for upstream sync %s" % self.branch_name
            )
            return False
        return landed[0]

    @property
    def repository(self) -> str:
        # Need to check central before landing repos
        head = self.gecko_commits.head
        repo = gecko_repo(self.git_gecko, head.commit)
        if repo is None:
            raise ValueError("Commit %s not part of any repository" % head.sha1)
        return repo

    @mut()
    def add_commit(self, gecko_commit: GeckoCommit) -> tuple[Commit | None, bool]:
        git_work = self.wpt_worktree.get()

        metadata = {"gecko-commit": gecko_commit.canonical_rev}

        if os.path.exists(os.path.join(git_work.working_dir, gecko_commit.canonical_rev + ".diff")):
            # If there's already a patch file here then don't try to create a new one
            # because we'll presumbaly fail again
            raise AbortError("Skipping due to existing patch")
        wpt_commit = gecko_commit.move(
            git_work,
            metadata=metadata,
            msg_filter=commit_message_filter,
            src_prefix=env.config["gecko"]["path"]["wpt"],
        )
        assert not git_work.is_dirty()
        if wpt_commit:
            self.wpt_commits.head = wpt_commit

        return wpt_commit, True

    @mut()
    def create_pr(self) -> int:
        if self.pr:
            return self.pr

        assert self.remote_branch is not None
        assert self.remote_branch in self.git_wpt.remotes.origin.refs
        while not env.gh_wpt.has_branch(self.remote_branch):
            logger.debug("Waiting for branch")
            time.sleep(1)

        commit_summary = self.wpt_commits[0].commit.summary
        if isinstance(commit_summary, bytes):
            commit_summary = commit_summary.decode("utf8", "ignore")

        msg = self.wpt_commits[0].msg.split(b"\n", 1)
        body = msg[1].decode("utf8", "replace") if len(msg) != 1 else ""

        pr_id = env.gh_wpt.create_pull(
            title="[Gecko{}] {}".format(" Bug %s" % self.bug if self.bug else "", commit_summary),
            body=body.strip(),
            base="master",
            head=self.remote_branch,
        )
        self.pr = pr_id
        # TODO: add label to bug
        env.bz.comment(
            self.bug,
            "Created web-platform-tests PR %s for changes under "
            "testing/web-platform/tests" % env.gh_wpt.pr_url(pr_id),
        )
        return pr_id

    @mut()
    def push_commits(self) -> None:
        remote_branch = self.get_or_create_remote_branch()
        logger.info(f"Pushing commits from bug {self.bug} to branch {remote_branch}")
        push_info = self.git_wpt.remotes.origin.push(
            "refs/heads/%s:%s" % (self.branch_name, remote_branch), force=True, set_upstream=True
        )
        for item in push_info:
            if item.flags & item.ERROR:
                raise AbortError(item.summary)

    def push_required(self) -> bool:
        return not (
            self.remote_branch
            and self.remote_branch in self.git_wpt.remotes.origin.refs
            and self.git_wpt.remotes.origin.refs[self.remote_branch].commit.hexsha
            == self.wpt_commits.head.sha1
        )

    @mut()
    def update_github(self) -> None:
        if self.pr:
            state = env.gh_wpt.pull_state(self.pr)
            if not len(self.gecko_commits):
                env.gh_wpt.close_pull(self.pr)
            elif state == "closed":
                pr = env.gh_wpt.get_pull(self.pr)
                if not pr.merged:
                    env.gh_wpt.reopen_pull(self.pr)
                else:
                    # If all the local commits are represented upstream, everything is
                    # fine and close out the sync. Otherwise we have a problem.
                    if len(self.upstreamed_gecko_commits) == len(self.gecko_commits):
                        if self.status not in ("wpt-merged", "complete"):
                            env.bz.comment(self.bug, "Upstream PR merged")

                        self.finish()
                    else:
                        # It's unclear what to do in this case, so mark the sync for manual
                        # fixup
                        self.error = "Upstream PR merged, but additional commits added after merge"
                    return

        if not len(self.gecko_commits):
            return

        if not len(self.upstreamed_gecko_commits):
            return

        if self.push_required():
            self.push_commits()
        if not self.pr:
            self.create_pr()

        self.set_landed_status()

    def set_landed_status(self) -> None:
        """
        Set the status of the check on the GitHub commit upstream. This check
        is used to tell if the code has been landed into Gecko.
        """
        if not self.pr:
            return
        landed_status = "success" if self.gecko_landed() else "failure"
        logger.info("Setting landed status to %s" % landed_status)
        # TODO - Maybe ignore errors setting the status
        if env.gh_wpt.get_status(self.pr, "upstream/gecko") != landed_status:
            env.gh_wpt.set_status(
                self.pr,
                landed_status,
                target_url=env.bz.bugzilla_url(self.bug),
                description="Landed on mozilla-central",
                context="upstream/gecko",
            )

    @mut()
    def try_land_pr(self) -> bool:
        logger.info("Checking if sync for bug %s can land" % self.bug)
        if not self.status == "open":
            logger.info("Sync is %s" % self.status)
            return False
        if not self.gecko_landed():
            logger.info("Commits are not yet landed in gecko")
            return False

        if not self.pr:
            logger.info("No upstream PR created")
            return False

        merge_sha = env.gh_wpt.merge_sha(self.pr)
        if merge_sha:
            logger.info("PR already merged")
            self.merge_sha = merge_sha
            self.finish("wpt-merged")
            return False

        try:
            self.set_landed_status()
        except Exception:
            logger.warning("Failed setting status on PR for bug %s" % self.bug)

        logger.info("Commit are landable; trying to land %s" % self.pr)

        msg = None
        check_status, checks = get_check_status(self.pr)
        if check_status not in [CheckStatus.SUCCESS, CheckStatus.PENDING]:
            details = ["Github PR %s" % env.gh_wpt.pr_url(self.pr)]
            msg = "Can't merge web-platform-tests PR due to failing upstream checks:\n%s" % details
        elif not env.gh_wpt.is_mergeable(self.pr):
            msg = "Can't merge web-platform-tests PR because it has merge conflicts"
        elif not env.gh_wpt.is_approved(self.pr):
            # This should be handled by the pr-bot
            msg = "Can't merge web-platform-tests PR because it is missing approval"
        else:
            try:
                merge_sha = env.gh_wpt.merge_pull(self.pr)
                env.bz.comment(
                    self.bug,
                    "Upstream PR merged by %s" % env.config["web-platform-tests"]["github"]["user"],
                )
            except GithubException as e:
                if isinstance(e.data, dict):
                    err_msg = e.data.get("message", "Unknown GitHub Error")
                else:
                    err_msg = e.data
                msg = "Merging PR %s failed.\nMessage: %s" % (env.gh_wpt.pr_url(self.pr), err_msg)
            except Exception as e:
                msg = "Merging PR %s failed.\nMessage: %s" % (env.gh_wpt.pr_url(self.pr), e)
            else:
                self.merge_sha = merge_sha
                self.finish("wpt-merged")
                return True
        if msg is not None:
            logger.error(msg)
        return False

    @mut()
    def finish(self, status: str = "complete") -> None:
        super().finish(status)
        if status in ("wpt-merged", "complete") and self.remote_branch:
            # Delete the remote branch after a merge
            try:
                self.git_wpt.remotes.origin.push(self.remote_branch, delete=True)
            except git.GitCommandError:
                pass
            else:
                self.remote_branch = None

    @property
    def pr_head(self) -> str | None:
        """
        Retrieves the head of the PR ref: origin/pr/{pr_id}
        :return: The SHA of the head commit.
        """
        if not self.pr:
            logger.error("No PR ID found for %s" % self.process_name)
            return None

        pr_ref = f"origin/pr/{self.pr}"

        if pr_ref not in self.git_wpt.references:
            # PR ref doesn't seem to exist
            logger.error("No ref found for %s" % pr_ref)
            return None

        ref = self.git_wpt.references[pr_ref]
        return ref.commit.hexsha

    @property
    def pr_commits(self) -> CommitRange:
        pr_head_sha = self.pr_head
        if not pr_head_sha:
            raise ValueError(
                "Can't get PR commits as the ref head could not be found for %s" % self.process_name
            )

        pr_head = sync_commit.WptCommit(self.git_wpt, pr_head_sha)

        merge_bases = []

        # Check if the PR Head is reachable from origin/master
        origin_master_sha = self.git_wpt.references["origin/master"].commit.hexsha
        pr_head_reachable = self.git_wpt.is_ancestor(
            pr_head.commit, self.git_wpt.rev_parse("origin/master")
        )

        # If not reachable, then it either hasn't landed yet, it was a Squash + Merge,
        # or a Rebase and merge.
        if not pr_head_reachable:
            merge_bases = self.git_wpt.merge_base(origin_master_sha, pr_head.sha1)
        else:
            if not self.merge_sha:
                raise ValueError(
                    "The merge SHA for %s could not be found in the UpstreamSync"
                    % self.process_name
                )
            merge_commit = sync_commit.WptCommit(self.git_wpt, self.merge_sha)

            # If the commit has two parents, one of them being our pr head, it is a merge commit
            parents = list(merge_commit.commit.parents)
            if len(parents) == 2 and pr_head in parents:
                other_parent = parents[0] if parents[1] == pr_head.commit else parents[1]
                merge_bases = self.git_wpt.merge_base(pr_head.sha1, other_parent)

            # Not a merge commit, so just use the base we have stored
            else:
                merge_bases = [self.wpt_commits.base.commit]

        # Check that we found the merge base
        if len(merge_bases) == 0:
            raise ValueError("Problem determining merge base for %s" % self.process_name)
        else:
            merge_base = merge_bases[0]

        # Create a CommitRange object and return it
        base = sync_commit.WptCommit(self.git_wpt, merge_base)
        head_ref_dict = AttrDict({"commit": pr_head})
        if TYPE_CHECKING:
            # This is a terrible hack.
            head_ref = cast(BranchRefObject, head_ref_dict)
        else:
            head_ref = head_ref_dict
        return CommitRange(self.git_wpt, base, head_ref, sync_commit.WptCommit, CommitFilter())


def commit_message_filter(msg: bytes) -> tuple[bytes, dict[str, str]]:
    metadata = {}
    m = commitparser.BUG_RE.match(msg)
    if m:
        bug_bytes, bug_number = m.groups()[:2]
        if msg.startswith(bug_bytes):
            prefix = re.compile(rb"^%s[^\w\d\[\(]*" % bug_bytes)
            msg = prefix.sub(b"", msg)
        metadata["bugzilla-url"] = env.bz.bugzilla_url(int(bug_number))

    reviewers = ", ".join(
        item.decode("utf8", "replace") for item in commitparser.parse_reviewers(msg)
    )
    if reviewers:
        metadata["gecko-reviewers"] = reviewers
    msg = commitparser.replace_reviewers(msg, "")
    msg = commitparser.strip_commit_metadata(msg)
    description = msg.splitlines()
    if description:
        summary = description.pop(0)
        summary = summary.rstrip(b"!#$%&(*+,-/:;<=>@[\\^_`{|~").rstrip()
        msg = summary + (b"\n" + b"\n".join(description) if description else b"")

    return msg, metadata


def wpt_commits(
    git_gecko: Repo, first_commit: GeckoCommit, head_commit: GeckoCommit
) -> list[GeckoCommit]:
    # List of syncs that have changed, so we can update them all as appropriate at the end
    revish = f"{first_commit.sha1}..{head_commit.sha1}"
    logger.info("Getting commits in range %s" % revish)
    commits = [
        sync_commit.GeckoCommit(git_gecko, item.hexsha)
        for item in git_gecko.iter_commits(
            revish, paths=env.config["gecko"]["path"]["wpt"], reverse=True, max_parents=1
        )
    ]
    return filter_commits(commits)


def filter_commits(commits: list[GeckoCommit]) -> list[GeckoCommit]:
    rv = []
    for commit in commits:
        if (
            commit.metadata.get("wptsync-skip")
            or DownstreamSync.has_metadata(commit.msg)
            or (commit.is_backout and not commit.wpt_commits_backed_out()[0])
        ):
            continue
        rv.append(commit)
    return rv


def remove_complete_backouts(commits: Iterable[Commit]) -> Sequence[Commit]:
    """Given a list of commits, remove any commits for which a backout exists
    in the list"""
    commits_remaining: set[str] = set()
    for commit in commits:
        assert isinstance(commit, GeckoCommit)
        if commit.is_backout:
            backed_out_commits, _ = commit.wpt_commits_backed_out()
            backed_out = {item.sha1 for item in backed_out_commits}
            intersecion = backed_out.intersection(commits_remaining)
            if len(intersecion) > 0:
                commits_remaining -= intersecion
                continue
        commits_remaining.add(commit.sha1)

    return [item for item in commits if item.sha1 in commits_remaining]


class Endpoints:
    def __init__(self, first: GeckoCommit) -> None:
        self._first: GeckoCommit = first
        self._second: GeckoCommit | None = None

    @property
    def base(self) -> GeckoCommit:
        return GeckoCommit(self._first.repo, self._first.commit.parents[0])

    @property
    def head(self) -> GeckoCommit:
        if self._second is not None:
            return self._second
        return self._first

    @head.setter
    def head(self, value: GeckoCommit) -> None:
        self._second = value

    def __repr__(self) -> str:
        return f"<Endpoints {self.base}:{self.head}>"


def updates_for_backout(
    git_gecko: Repo,
    git_wpt: Repo,
    commit: GeckoCommit,
) -> tuple[CreateSyncs, UpdateSyncs]:
    backed_out_commits, bugs = commit.wpt_commits_backed_out()
    backed_out_commit_shas = {item.sha1 for item in backed_out_commits}

    create_syncs: CreateSyncs = {None: []}
    update_syncs: UpdateSyncs = {}

    for backed_out_commit in backed_out_commits:
        syncs: list[UpstreamSync] = []
        backed_out_bug = backed_out_commit.bug
        if backed_out_bug:
            syncs = UpstreamSync.for_bug(
                git_gecko, git_wpt, backed_out_bug, statuses={"open", "incomplete"}, flat=True
            )
            if len(syncs) not in (0, 1):
                raise ValueError(
                    "Lookup of upstream syncs for bug %s returned syncs: %r" % (len(syncs), syncs)
                )
        if syncs:
            sync = syncs.pop()
            if commit in sync.gecko_commits:
                # This commit was already processed for this sync
                backed_out_commit_shas.remove(backed_out_commit.sha1)
                continue
            if backed_out_commit in sync.upstreamed_gecko_commits:
                backed_out_commit_shas.remove(backed_out_commit.sha1)
                assert sync.bug is not None
                update_syncs[sync.bug] = (sync, commit)

    if backed_out_commit_shas:
        # This backout covers something other than known open syncs, so we need to
        # create a new sync especially for it
        # TODO: we should check for this already existing before we process the backout
        # Need to create a bug for this backout
        backout_bug = None
        for bug in bugs:
            open_bug_syncs = UpstreamSync.for_bug(
                git_gecko, git_wpt, bug, statuses={"open", "incomplete"}, flat=False
            )
            if bug not in update_syncs and not open_bug_syncs:
                backout_bug = bug
                break
        if backout_bug is None:
            # TODO: Turn create_syncs into a class
            new_no_bug = create_syncs[None]
            assert isinstance(new_no_bug, list)
            new_no_bug.append(Endpoints(commit))
        else:
            create_syncs[backout_bug] = Endpoints(commit)
    return create_syncs, update_syncs


def updated_syncs_for_push(
    git_gecko: Repo,
    git_wpt: Repo,
    first_commit: GeckoCommit,
    head_commit: GeckoCommit,
) -> tuple[CreateSyncs, UpdateSyncs] | None:
    # TODO: Check syncs with pushes that no longer exist on autoland
    all_commits = wpt_commits(git_gecko, first_commit, head_commit)
    if not all_commits:
        logger.info("No new commits affecting wpt found")
        return None
    else:
        logger.info("Got %i commits since the last sync point" % len(all_commits))

    commits = remove_complete_backouts(all_commits)

    if not commits:
        logger.info("No commits remain after removing backout pairs")
        return None

    create_syncs: CreateSyncs = {None: []}
    update_syncs: UpdateSyncs = {}

    for commit in commits:
        assert isinstance(commit, GeckoCommit)
        if commit.upstream_sync(git_gecko, git_wpt) is not None:
            # This commit was already processed e.g. by a manual invocation, so skip
            continue
        if commit.is_backout:
            create, update = updates_for_backout(git_gecko, git_wpt, commit)
            create_syncs.update(create)
            update_syncs.update(update)
        if commit.is_downstream or commit.is_landing:
            continue
        else:
            bug = commit.bug
            if bug is None:
                continue
            sync: SyncProcess | None = None
            if bug in update_syncs:
                sync, _ = update_syncs[bug]
            else:
                statuses = ["open", "incomplete"]
                syncs = UpstreamSync.for_bug(git_gecko, git_wpt, bug, statuses=statuses, flat=True)
                if len(syncs) not in (0, 1):
                    logger.warning(
                        "Lookup of upstream syncs for bug %s returned syncs: %r"
                        % (len(syncs), syncs)
                    )
                    # Try to pick the most recent sync
                    for status in statuses:
                        status_syncs = [s for s in syncs if s.status == status]
                        if status_syncs:
                            status_syncs.sort(key=lambda x: int(x.process_name.obj_id))
                            sync = status_syncs.pop()
                            break
                if syncs:
                    sync = syncs[0]

            if sync:
                assert isinstance(sync, UpstreamSync)
                if commit not in sync.gecko_commits:
                    update_syncs[bug] = (sync, commit)
                elif sync.pr is None:
                    head = sync.gecko_commits.head
                    assert isinstance(head, GeckoCommit)
                    update_syncs[bug] = (sync, head)
            else:
                if bug is None:
                    create_syncs[None].append(Endpoints(commit))
                elif bug in create_syncs:
                    bug_endpoints = create_syncs[bug]
                    assert isinstance(bug_endpoints, Endpoints)
                    bug_endpoints.head = commit
                else:
                    create_syncs[bug] = Endpoints(commit)

    return create_syncs, update_syncs


def create_syncs(
    lock: SyncLock,
    git_gecko: Repo,
    git_wpt: Repo,
    create_endpoints: dict[int | None, list | Endpoints],
) -> list[UpstreamSync]:
    rv = []
    for bug, endpoints in create_endpoints.items():
        if bug is not None:
            assert isinstance(endpoints, Endpoints)
            endpoints = [endpoints]
        assert isinstance(endpoints, list)
        for endpoint in endpoints:
            if bug is None:
                # TODO: Loading the commits doesn't work in this case, because we depend on the bug
                commit = sync_commit.GeckoCommit(git_gecko, endpoint.head)
                bug = env.bz.new(
                    "Upstream commit %s to web-platform-tests" % commit.canonical_rev,
                    "",
                    "Testing",
                    "web-platform-tests",
                    whiteboard="[wptsync upstream]",
                )
            sync = UpstreamSync.new(
                lock,
                git_gecko,
                git_wpt,
                bug=bug,
                gecko_base=endpoint.base.sha1,
                gecko_head=endpoint.head.sha1,
                wpt_base="origin/master",
                wpt_head="origin/master",
            )
            rv.append(sync)
    return rv


def update_sync_heads(
    lock: SyncLock,
    syncs_by_bug: dict[int, tuple[UpstreamSync, GeckoCommit]],
) -> list[UpstreamSync]:
    rv = []
    for bug, (sync, commit) in syncs_by_bug.items():
        if sync.status not in ("open", "incomplete"):
            # TODO: Create a new sync with a non-zero seq-id in this case
            raise ValueError(
                "Tried to modify a closed sync for bug %s with commit %s"
                % (bug, commit.canonical_rev)
            )
        with sync.as_mut(lock):
            sync.gecko_commits.head = commit
            for gecko_commit in sync.gecko_commits:
                assert isinstance(gecko_commit, GeckoCommit)
                gecko_commit.set_upstream_sync(sync)
        rv.append(sync)
    return rv


def update_modified_sync(git_gecko: Repo, git_wpt: Repo, sync: UpstreamSync) -> None:
    assert sync._lock is not None
    if len(sync.gecko_commits) == 0:
        # In the case that there are no gecko commits, we presumably had a backout
        # In this case we don't touch the wpt commits, but just mark the PR
        # as closed. That's pretty counterintuitive, but it turns out that GitHub
        # will only let you reopen a closed PR if you don't change the branch head in
        # the meantime. So we carefully avoid touching the wpt side until something
        # relands and we have a chance to reopen the PR
        logger.info("Sync has no commits, so marking as incomplete")
        sync.status = "incomplete"
        if not sync.pr:
            logger.info("Sync was already fully applied upstream, not creating a PR")
            return
    else:
        sync.status = "open"
        try:
            sync.update_wpt_commits()
        except AbortError:
            # If we got a merge conflict and the PR doesn't exist yet then try
            # recreating the commits on top of the current sync point in order that
            # we get a PR and it's visible that it fails
            if not sync.pr:
                logger.info(
                    "Applying to origin/master failed; retrying with the current sync point"
                )
                from .landing import load_sync_point

                sync_point = load_sync_point(git_gecko, git_wpt)
                sync.set_wpt_base(sync_point["upstream"])
                try:
                    sync.update_wpt_commits()
                except AbortError:
                    # Reset the base to origin/master
                    sync.set_wpt_base("origin/master")
                    with env.bz.bug_ctx(sync.bug) as bug:
                        bug.add_comment(
                            "Failed to create upstream wpt PR due to "
                            "merge conflicts. This requires fixup from a wpt sync "
                            "admin."
                        )
                        needinfo_users = [
                            item.strip()
                            for item in (
                                env.config["gecko"]["needinfo"].get("upstream", "").split(",")
                            )
                        ]
                        needinfo_users = [item for item in needinfo_users if item]
                        bug.needinfo(*needinfo_users)
                    raise

    sync.update_github()


def update_sync_prs(
    lock: SyncLock,
    git_gecko: Repo,
    git_wpt: Repo,
    create_endpoints: dict[int | None, list | Endpoints],
    update_syncs: dict[int, tuple[UpstreamSync, GeckoCommit]],
    raise_on_error: bool = False,
) -> tuple[set[UpstreamSync], set]:
    pushed_syncs = set()
    failed_syncs = set()

    to_push = create_syncs(lock, git_gecko, git_wpt, create_endpoints)
    to_push.extend(update_sync_heads(lock, update_syncs))

    for sync in to_push:
        with sync.as_mut(lock):
            try:
                update_modified_sync(git_gecko, git_wpt, sync)
            except Exception as e:
                sync.error = e
                if raise_on_error:
                    raise
                traceback.print_exc()
                logger.error(e)
                failed_syncs.add((sync, e))
            else:
                sync.error = None
                pushed_syncs.add(sync)

    return pushed_syncs, failed_syncs


def try_land_syncs(lock: SyncLock, syncs: set[UpstreamSync]) -> set[UpstreamSync]:
    landed_syncs = set()
    for sync in syncs:
        with sync.as_mut(lock):
            if sync.try_land_pr():
                landed_syncs.add(sync)
    return landed_syncs


@entry_point("upstream")
@mut("sync")
def update_sync(
    git_gecko: Repo,
    git_wpt: Repo,
    sync: UpstreamSync,
    raise_on_error: bool = True,
    repo_update: bool = True,
) -> tuple[set[UpstreamSync], set[UpstreamSync], set]:
    if sync.status in ("wpt-merged", "complete"):
        logger.info("Nothing to do for sync with status %s" % sync.status)
        return set(), set(), set()

    if repo_update:
        update_repositories(git_gecko, git_wpt)
    assert isinstance(sync, UpstreamSync)
    assert sync._lock is not None
    update_syncs = {sync.bug: (sync, cast(GeckoCommit, sync.gecko_commits.head))}
    pushed_syncs, failed_syncs = update_sync_prs(
        sync._lock, git_gecko, git_wpt, {}, update_syncs, raise_on_error=raise_on_error
    )

    if sync not in failed_syncs:
        landed_syncs = try_land_syncs(sync._lock, {sync})
    else:
        landed_syncs = set()

    return pushed_syncs, failed_syncs, landed_syncs


@entry_point("upstream")
def gecko_push(
    git_gecko: Repo,
    git_wpt: Repo,
    repository_name: str,
    hg_rev: str,
    raise_on_error: bool = False,
    base_rev: Any | None = None,
) -> tuple[set[UpstreamSync], set[UpstreamSync], set] | None:
    rev = git_gecko.rev_parse(cinnabar(git_gecko).hg2git(hg_rev))
    last_sync_point, prev_commit = UpstreamSync.prev_gecko_commit(git_gecko, repository_name)

    assert last_sync_point.commit is not None
    if base_rev is None and git_gecko.is_ancestor(rev, last_sync_point.commit.commit):
        logger.info("Last sync point moved past commit")
        return None

    with SyncLock("upstream", None) as lock:
        assert isinstance(lock, SyncLock)
        updated = updated_syncs_for_push(
            git_gecko, git_wpt, prev_commit, sync_commit.GeckoCommit(git_gecko, rev)
        )

        if updated is None:
            return set(), set(), set()

        create_endpoints, update_syncs = updated

        pushed_syncs, failed_syncs = update_sync_prs(
            lock, git_gecko, git_wpt, create_endpoints, update_syncs, raise_on_error=raise_on_error
        )

        landable_syncs = {
            item
            for item in UpstreamSync.load_by_status(git_gecko, git_wpt, "open")
            if item.error is None
        }
        if TYPE_CHECKING:
            landable = cast(Set[UpstreamSync], landable_syncs)
        else:
            landable = landable_syncs
        landed_syncs = try_land_syncs(lock, landable)

        # TODO
        if not git_gecko.is_ancestor(rev, last_sync_point.commit.commit):
            with last_sync_point.as_mut(lock):
                last_sync_point.commit = rev.hexsha

    return pushed_syncs, landed_syncs, failed_syncs


@enum.unique
class CheckStatus(enum.Enum):
    SUCCESS = "success"
    PENDING = "pending"
    FAILURE = "failure"


def get_check_status(pr_id: int) -> tuple[CheckStatus, Mapping[str, Mapping[str, Any]]]:
    checks = env.gh_wpt.get_check_runs(pr_id)
    if commit_checks_pass(checks):
        status = CheckStatus.SUCCESS
    elif not commit_checks_complete(checks):
        status = CheckStatus.PENDING
    else:
        status = CheckStatus.FAILURE
    return status, checks


def commit_checks_pass(checks: Mapping[str, Mapping[str, Any]]) -> bool:
    """Boolean indicating whether all required check runs pass"""
    return all(
        item["required"] is False
        or (item["status"] == "completed" and item["conclusion"] in ("success", "neutral"))
        for item in checks.values()
    )


def commit_checks_complete(checks: Mapping[str, Mapping[str, Any]]) -> bool:
    """Boolean indicating whether all check runs are complete"""
    return all(item["status"] == "completed" for item in checks.values())


@entry_point("upstream")
@mut("sync")
def commit_check_changed(git_gecko: Repo, git_wpt: Repo, sync: UpstreamSync) -> Optional[bool]:
    landed = False
    if sync.status != "open":
        return True

    if sync.pr is None:
        logger.error(f"No PR found for sync {sync}")
        return None

    check_status, checks = get_check_status(sync.pr)

    if not checks:
        logger.error("No checks found for pr %s" % sync.pr)
        return None

    # Record the overall status and commit so we only notify once per commit
    this_pr_check = {"state": check_status.value, "sha": next(iter(checks.values()))["head_sha"]}
    last_pr_check = sync.last_pr_check
    sync.last_pr_check = this_pr_check

    if check_status == CheckStatus.SUCCESS:
        sync.error = None
        if sync.gecko_landed():
            landed = sync.try_land_pr()
        elif this_pr_check != last_pr_check:
            env.bz.comment(
                sync.bug,
                "Upstream web-platform-tests status checks passed, "
                "PR will merge once commit reaches central.",
            )
    elif check_status == CheckStatus.FAILURE and last_pr_check != this_pr_check:
        details = ["Github PR %s" % env.gh_wpt.pr_url(sync.pr)]
        for name, check_run in checks.items():
            if check_run["conclusion"] not in ("success", "neutral"):
                details.append("* {} ({})".format(name, check_run["url"]))
        msg = "Can't merge web-platform-tests PR due to failing upstream checks:\n%s" % "\n".join(
            details
        )
        try:
            with env.bz.bug_ctx(sync.bug) as bug:
                bug["comment"] = msg
            # Do this as a seperate operation
            with env.bz.bug_ctx(sync.bug) as bug:
                commit_author = sync.gecko_commits[0].email
                if commit_author:
                    bug.needinfo(commit_author.decode("utf8"))
        except BugsyException:
            msg = traceback.format_exc()
            logger.warning("Failed to update bug:\n%s" % msg)
            # Sometimes needinfos fail because emails addresses in bugzilla don't
            # match the commits. That's non-fatal, but record the exception here in
            # case something more unexpected happens
            sync.error = "Checks failed"
        else:
            logger.info("Some upstream web-platform-tests status checks still pending.")
    return landed


@entry_point("upstream")
@mut("sync")
def update_pr(
    git_gecko: Repo,
    git_wpt: Repo,
    sync: UpstreamSync,
    action: str,
    merge_sha: str | None = None,
    base_sha: str | None = None,
    merged_by: str | None = None,
) -> None:
    """Update the sync status for a PR event on github

    :param action string: Either a PR action or a PR status
    :param merge_sha string: SHA of the new head if the PR merged or None if it didn't"""

    if action == "closed":
        if not merge_sha and sync.pr_status != "closed":
            env.bz.comment(sync.bug, "Upstream PR was closed without merging")
            sync.pr_status = "closed"
        else:
            assert merge_sha is not None
            sync.merge_sha = merge_sha
            if not sync.wpt_commits and base_sha:
                sync.set_wpt_base(base_sha)
            if sync.status not in ("complete", "wpt-merged"):
                env.bz.comment(sync.bug, "Upstream PR merged by %s" % merged_by)
                sync.finish("wpt-merged")
    elif action == "reopened" or action == "open":
        sync.pr_status = "open"
