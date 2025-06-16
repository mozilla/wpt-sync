from __future__ import annotations
import enum
import itertools
import traceback
from collections import defaultdict
from typing import Mapping, Optional, Self, overload

import git

from . import bug
from . import log
from .base import (
    BranchRefObject,
    CommitBuilder,
    IdentityMap,
    ProcessData,
    ProcessName,
    ProcessNameIndex,
)
from .commit import GeckoCommit, WptCommit
from .env import Environment
from .errors import AbortError
from .lock import MutGuard, mut, constructor
from .repos import cinnabar
from .worktree import Worktree

from typing import (Any,
                    Iterable,
                    Iterator,
                    Sequence,
                    cast,
                    TYPE_CHECKING)
from git.repo.base import Repo
from os import PathLike
from typing_extensions import Literal
if TYPE_CHECKING:
    from sync.commit import Commit
    from sync.index import Index
    from sync.lock import SyncLock
    from sync.trypush import TryPush




env = Environment()

logger = log.get_logger(__name__)


class CommitFilter:
    """Filter of a range of commits"""

    def __init__(self) -> None:
        self._commits: dict[str, bool] = {}

    def path_filter(self) -> Sequence[PathLike]:
        """Path filter for the commit range,
        returning a list of paths that match. An empty list
        matches all paths."""
        return []

    def filter_commit(self, commit: Commit) -> bool:
        """Per-commit filter.

        :param commit: wpt_commit.Commit object
        :returns: A boolean indicating whether to include the commit"""
        if commit.sha1 not in self._commits:
            self._commits[commit.sha1] = self._filter_commit(commit)
        return self._commits[commit.sha1]

    def _filter_commit(self, commit: Commit) -> bool:
        return True

    def filter_commits(
        self,
        commits: Iterable[Commit],
    ) -> Sequence[Commit]:
        """Filter that applies to the set of commits that were selected
        by the per-commit filter. Useful for e.g. removing backouts
        from a set of commits.

        :param commits: List of wpt_commit.Commit objects
        :returns: List of commits to return"""
        return list(commits)


class CommitRange:
    # This class should probably be generic in the commit subtype it returns
    # but trying that ended up with lots of errors, so for now it just specifies
    # Commit, meaning that users need to cast to use properies of subtypes
    """Range of commits in a specific repository.

    This is assumed to be between a tag and a branch sharing a single process_name
    i.e. the tag represents the base commit of the branch.
    TODO:  Maybe just store the base branch name in the tag rather than making it
           an actual pointer since that works better with rebases.
    """

    def __init__(
        self,
        repo: Repo,
        base: str | Commit,
        head_ref: BranchRefObject,
        commit_cls: type,
        commit_filter: CommitFilter,
    ) -> None:
        self.repo = repo

        # This ended up a little confused because these used to both be
        # VcsRefObjects, but now the base is stored as a ref not associated
        # with a process_name. This should be refactored.
        self._base_commit: Commit | None = None
        self._base = base
        self._head_ref = head_ref
        self.commit_cls = commit_cls
        self.commit_filter = commit_filter

        # Cache for the commits in this range
        self._commits: Sequence[Commit] = []
        self._head_sha: str | None = None
        self._base_sha: str | None = None

        self._lock = None

    def as_mut(self, lock: SyncLock) -> MutGuard:
        return MutGuard(lock, self, [self._head_ref])

    @property
    def lock_key(self) -> tuple[str, str]:
        return (self._head_ref.name.subtype, self._head_ref.name.obj_id)

    @overload
    def __getitem__(
        self, index: int
    ) -> Commit:
        pass

    @overload  # noqa: F811
    def __getitem__(
        self, index: slice
    ) -> Sequence[Commit]:
        pass

    def __getitem__(
        self,  # noqa: F811
        index: int | slice,
    ) -> Commit | Sequence[Commit]:
        return self.commits[index]

    def __iter__(self) -> Iterator[Commit]:
        yield from self.commits

    def __len__(self) -> int:
        return len(self.commits)

    def __contains__(self, other_commit: Any) -> bool:
        for commit in self:
            if commit == other_commit:
                return True
        return False

    @property
    def commits(self) -> Sequence[Commit]:
        if self._commits:
            if self.head.sha1 == self._head_sha and self.base.sha1 == self._base_sha:
                return self._commits
        revish = f"{self.base.sha1}..{self.head.sha1}"
        commits: list[Commit] = []
        for git_commit in self.repo.iter_commits(
            revish, reverse=True, paths=self.commit_filter.path_filter()
        ):
            commit = self.commit_cls(self.repo, git_commit)
            if not self.commit_filter.filter_commit(commit):
                continue
            commits.append(commit)
        commits = list(self.commit_filter.filter_commits(commits))
        self._commits = commits
        self._head_sha = self.head.sha1
        self._base_sha = self.base.sha1
        return self._commits

    @property
    def files_changed(self) -> set[str]:
        # We avoid using diffs because that's harder to get right in the face of merges
        files = set()
        for commit in self.commits:
            commit_files = self.repo.git.show(commit.sha1, name_status=True, format="")
            for item in commit_files.splitlines():
                parts = item.split("\t")
                for part in parts[1:]:
                    files.add(part.strip())
        return files

    @property
    def base(self) -> Commit:
        if self._base_commit is None:
            self._base_commit = self.commit_cls(self.repo, self._base)
        assert self._base_commit is not None
        return self._base_commit

    @base.setter
    @mut()
    def base(self, value: str) -> None:
        # Note that this doesn't actually update the stored value of the base
        # anywhere, unlike the head setter which will update the associated ref
        self._commits = []
        self._base_sha = value
        self._base = self.commit_cls(self.repo, value)
        self._base_commit = None

    @property
    def head(self) -> Commit:
        head_commit = self._head_ref.commit
        assert head_commit is not None
        if TYPE_CHECKING:
            cast(Commit, head_commit)
        return head_commit

    @head.setter
    @mut()
    def head(self, value: Commit) -> None:
        self._head_ref.commit = value


@enum.unique
class LandableStatus(enum.Enum):
    ready = 0
    no_pr = 1
    upstream = 2
    no_sync = 3
    error = 4
    missing_try_results = 5
    skip = 6

    def reason_str(self) -> str:
        return {
            LandableStatus.ready: "Ready",
            LandableStatus.no_pr: "No PR",
            LandableStatus.upstream: "From gecko",
            LandableStatus.no_sync: "No sync created",
            LandableStatus.error: "Error",
            LandableStatus.missing_try_results: "Incomplete try results",
            LandableStatus.skip: "Skip",
        }.get(self, "Unknown")


class SyncPointName(metaclass=IdentityMap):
    """Like a process name but for pointers that aren't associated with a
    specific sync object, but with a general process e.g. the last update point
    for an upstream sync."""

    def __init__(self, subtype: str, obj_id: str) -> None:
        self._obj_type = "sync"
        self._subtype = subtype
        self._obj_id = str(obj_id)

        self._lock = None

    @property
    def obj_type(self) -> str:
        return self._obj_type

    @property
    def subtype(self) -> str:
        return self._subtype

    @property
    def obj_id(self) -> str:
        return self._obj_id

    @classmethod
    def _cache_key(cls, subtype: str, obj_id: str) -> tuple[str, str]:
        return (subtype, str(obj_id))

    def key(self) -> tuple[str, str]:
        return (self._subtype, self._obj_id)

    def __str__(self) -> str:
        return f"{self._obj_type}/{self._subtype}/{self._obj_id}"

    def path(self) -> str:
        return f"{self._obj_type}/{self._subtype}/{self._obj_id}"


class SyncData(ProcessData):
    obj_type = "sync"


class SyncProcess(metaclass=IdentityMap):
    obj_type: str = "sync"
    sync_type: str = "*"
    # Either "bug" or "pr"
    obj_id: str | None = None
    statuses: tuple[str, ...] = ()
    status_transitions: list[tuple[str, str]] = []
    # Can multiple syncs have the same obj_id
    multiple_syncs: bool = False

    def __init__(self, git_gecko: Repo, git_wpt: Repo, process_name: ProcessName) -> None:
        self._lock: SyncLock | None = None

        assert process_name.obj_type == self.obj_type
        assert process_name.subtype == self.sync_type

        self.git_gecko = git_gecko
        self.git_wpt = git_wpt

        self.process_name = process_name

        self.data = SyncData(git_gecko, process_name)

        self.gecko_commits = CommitRange(
            git_gecko,
            self.data["gecko-base"],
            BranchRefObject(git_gecko, self.process_name, commit_cls=GeckoCommit),
            commit_cls=GeckoCommit,
            commit_filter=self.gecko_commit_filter(),
        )
        self.wpt_commits = CommitRange(
            git_wpt,
            self.data["wpt-base"],
            BranchRefObject(git_wpt, self.process_name, commit_cls=WptCommit),
            commit_cls=WptCommit,
            commit_filter=self.wpt_commit_filter(),
        )

        self.gecko_worktree = Worktree(git_gecko, process_name)

        self.wpt_worktree = Worktree(git_wpt, process_name)

        # Hold onto indexes for the lifetime of the SyncProcess object
        self._indexes = {ProcessNameIndex(git_gecko)}

    @classmethod
    def _cache_key(cls, git_gecko: Repo, git_wpt: Repo,
                   process_name: ProcessName) -> tuple[str, str, str, str]:
        return process_name.key()

    def as_mut(self, lock: SyncLock) -> MutGuard:
        return MutGuard(
            lock,
            self,
            [
                self.data,
                self.gecko_commits,
                self.wpt_commits,
                self.gecko_worktree,
                self.wpt_worktree,
            ],
        )

    @property
    def lock_key(self) -> tuple[str, str]:
        return (self.process_name.subtype, self.process_name.obj_id)

    def __repr__(self) -> str:
        return "<{} {} {}>".format(
            self.__class__.__name__, self.sync_type, self.process_name
        )

    @classmethod
    def for_pr(
        cls,
        git_gecko: Repo,
        git_wpt: Repo,
        pr_id: str | int,
    ) -> Optional[Self]:
        from . import index

        idx = index.PrIdIndex(git_gecko)
        process_name = idx.get((str(pr_id),))
        if process_name and process_name.subtype == cls.sync_type:
            return cls(git_gecko, git_wpt, process_name)
        return None

    @overload  # noqa: F811
    @classmethod
    def for_bug(
        cls,
        git_gecko: Repo,
        git_wpt: Repo,
        bug: int,
        statuses: Iterable[str] | None,
        flat: Literal[True],
    ) -> list[Self]:
        pass

    @overload  # noqa: F811
    @classmethod
    def for_bug(
        cls,
        git_gecko: Repo,
        git_wpt: Repo,
        bug: int,
        statuses: Iterable[str] | None,
        flat: Literal[False],
    ) -> Mapping[str, set[Self]]:
        pass

    @classmethod
    def for_bug(
        cls,
        git_gecko: Repo,
        git_wpt: Repo,
        bug: int,
        statuses: Optional[Iterable[str]] = None,
        flat: bool = False,
    ) -> Mapping[str, set[Self]] | list[Self]:
        """Get the syncs for a specific bug.

        :param bug: The bug number for which to find syncs.
        :param statuses: An optional list of sync statuses to include.
                         Defaults to all statuses.
        :param flat: Return a flat list of syncs instead of a dictionary.

        :returns: By default a dictionary of {status: [syncs]}, but if flat
                  is true, just returns a list of matching syncs.
        """
        from . import index

        bug_str = str(bug)
        statuses = set(statuses) if statuses is not None else set(cls.statuses)
        rv = defaultdict(set)
        idx_key = (bug_str,)
        if len(statuses) == 1:
            idx_key == (bug_str, list(statuses)[0])
        idx = index.BugIdIndex(git_gecko)

        process_names = idx.get(idx_key)
        for process_name in process_names:
            if process_name.subtype == cls.sync_type:
                sync = cls(git_gecko, git_wpt, process_name)
                if sync.status in statuses:
                    rv[sync.status].add(sync)
        if flat:
            return list(itertools.chain.from_iterable(rv.values()))
        return rv

    @classmethod
    def load_by_obj(cls, git_gecko: Repo, git_wpt: Repo, obj_id: int,
                    seq_id: int | None = None) -> set[Self]:
        process_names = ProcessNameIndex(git_gecko).get(
            cls.obj_type, cls.sync_type, str(obj_id)
        )
        if seq_id is not None:
            process_names = {item for item in process_names if item.seq_id == seq_id}
        return {cls(git_gecko, git_wpt, process_name) for process_name in process_names}

    @classmethod
    def load_by_status(cls, git_gecko: Repo, git_wpt: Repo, status: str) -> set[Self]:
        from . import index

        idx = index.SyncIndex(git_gecko)
        key = (cls.obj_type, cls.sync_type, status)
        process_names = idx.get(key)
        rv = set()
        for process_name in process_names:
            rv.add(cls(git_gecko, git_wpt, process_name))
        return rv

    # End of getter methods

    @classmethod
    def prev_gecko_commit(
        cls,
        git_gecko: Repo,
        repository_name: str,
        base_rev: str | None = None,
    ) -> tuple[BranchRefObject, GeckoCommit]:
        """Get the last gecko commit processed by a sync process.

        :param str repository_name: The name of the gecko branch being processed
        :param base_rev: The SHA1 for a commit to use as the previous commit, overriding
                         the stored value
        :returns: Tuple of (LastSyncPoint, GeckoCommit) for the previous gecko commit. In
                  case the base_rev override is passed in the LastSyncPoint may be pointing
                  at a different commit to the returned gecko commit"""

        last_sync_point = cls.last_sync_point(git_gecko, repository_name)

        assert last_sync_point.commit is not None
        logger.info("Last sync point was %s" % last_sync_point.commit.sha1)

        if base_rev is None:
            prev_commit = last_sync_point.commit
            # TODO: Would be good if we could link the repo class to the commit type
            # in the type system
            assert isinstance(prev_commit, GeckoCommit)
        else:
            prev_commit = GeckoCommit(git_gecko, cinnabar(git_gecko).hg2git(base_rev))
        return last_sync_point, prev_commit

    @classmethod
    def last_sync_point(cls, git_gecko: Repo, repository_name: str) -> BranchRefObject:
        assert "/" not in repository_name
        name = SyncPointName(cls.sync_type, repository_name)

        return BranchRefObject(git_gecko, name, commit_cls=GeckoCommit)

    @property
    def landable_status(self) -> LandableStatus:
        raise NotImplementedError

    def _output_data(self) -> list[str]:
        rv = [
            "{}{}".format("*" if self.error else " ", self.process_name.path()),
            "gecko range: {}..{}".format(
                self.gecko_commits.base.sha1, self.gecko_commits.head.sha1
            ),
            "wpt range: {}..{}".format(
                self.wpt_commits.base.sha1, self.wpt_commits.head.sha1
            ),
        ]
        if self.error:
            rv.extend(
                ["ERROR:", self.error["message"] or "", self.error["stack"] or ""]
            )
        landable_status = self.landable_status
        if landable_status:
            rv.append("Landable status: %s" % landable_status.reason_str())

        for key, value in sorted(self.data.items()):
            if key != "error":
                rv.append(f"{key}: {value}")

        try_pushes = self.try_pushes()
        if try_pushes:
            try_pushes = sorted(try_pushes, key=lambda x: x.process_name.seq_id)
            rv.append("Try pushes:")
            for try_push in try_pushes:
                rv.append(f"  {try_push.status} {try_push.treeherder_url}")
        return rv

    def output(self) -> str:
        return "\n".join(self._output_data())

    def __ne__(self, other: Any) -> bool:
        return not self == other

    def set_wpt_base(self, ref: str) -> None:
        # This is kind of an appaling hack
        try:
            self.git_wpt.commit(ref)
        except Exception:
            raise ValueError
        self.data["wpt-base"] = ref
        self.wpt_commits._base = WptCommit(self.git_wpt, ref)

    @staticmethod
    def gecko_integration_branch() -> str:
        return env.config["gecko"]["refs"][env.config["gecko"]["landing"]]

    @staticmethod
    def gecko_landing_branch() -> str:
        return env.config["gecko"]["refs"]["central"]

    def gecko_commit_filter(self) -> CommitFilter:
        return CommitFilter()

    def wpt_commit_filter(self) -> CommitFilter:
        return CommitFilter()

    @property
    def branch_name(self) -> str:
        return self.process_name.path()

    @property
    def status(self) -> str:
        return self.data["status"]

    @status.setter
    @mut()
    def status(self, value: str) -> None:
        if value not in self.statuses:
            raise ValueError("Unrecognised status %s" % value)
        current = self.status
        if current == value:
            return
        if (current, value) not in self.status_transitions:
            raise ValueError(
                f"Tried to change status from {current} to {value}"
            )

        from . import index

        index.SyncIndex(self.git_gecko).delete(
            index.SyncIndex.make_key(self), self.process_name
        )
        index.BugIdIndex(self.git_gecko).delete(
            index.BugIdIndex.make_key(self), self.process_name
        )
        self.data["status"] = value
        index.SyncIndex(self.git_gecko).insert(
            index.SyncIndex.make_key(self), self.process_name
        )
        index.BugIdIndex(self.git_gecko).insert(
            index.BugIdIndex.make_key(self), self.process_name
        )

    @property
    def bug(self) -> int | None:
        if self.obj_id == "bug":
            return int(self.process_name.obj_id)
        else:
            bug = self.data.get("bug")
            if bug:
                return int(bug)
            return None

    @bug.setter
    @mut()
    def bug(self, value: int) -> None:
        from . import index

        if self.obj_id == "bug":
            raise AttributeError("Can't set attribute")
        old_key = None
        if self.data.get("bug"):
            old_key = index.BugIdIndex.make_key(self)
        self.data["bug"] = value
        new_key = index.BugIdIndex.make_key(self)
        index.BugIdIndex(self.git_gecko).move(old_key, new_key, self.process_name)

    @property
    def pr(self) -> int | None:
        if self.obj_id == "pr":
            return int(self.process_name.obj_id)
        else:
            pr = self.data.get("pr")
            if pr is not None:
                return int(pr)
            return None

    @pr.setter
    @mut()
    def pr(self, value: int) -> None:
        from . import index

        if self.obj_id == "pr":
            raise AttributeError("Can't set attribute")
        old_key = None
        if self.data.get("pr"):
            old_key = index.PrIdIndex.make_key(self)
        self.data["pr"] = value
        new_key = index.PrIdIndex.make_key(self)
        index.PrIdIndex(self.git_gecko).move(old_key, new_key, self.process_name)

    @property
    def seq_id(self) -> int:
        return self.process_name.seq_id

    @property
    def last_pr_check(self) -> dict[str, str]:
        return self.data.get("last-pr-check", {})

    @last_pr_check.setter
    @mut()
    def last_pr_check(self, value: dict[str, str]) -> None:
        if value is not None:
            self.data["last-pr-check"] = value
        else:
            del self.data["last-pr-check"]


    def _get_error(self) -> dict[str, str | None] | None:
        return self.data.get("error")

    @mut()
    def _set_error(self, value: str | None) -> None:
        def encode(item: str | None) -> str | None:
            if item is None:
                return item
            if isinstance(item, str):
                return item
            if isinstance(item, str):
                return item.encode("utf8", "replace")
            return repr(item)

        if value is not None:
            if isinstance(value, (str, str)):
                message = value
                stack = None
            else:
                message = str(value)
                stack = traceback.format_exc()
            error = {"message": encode(message), "stack": encode(stack)}
            self.data["error"] = error
            self.set_bug_data("error")
        else:
            del self.data["error"]
            self.set_bug_data(None)

    # Defining it like this makes mypy happy if we override the setter in a subclass
    error = property(_get_error, _set_error)

    def try_pushes(self, status: str | None = None) -> list[TryPush]:
        from . import trypush

        try_pushes = trypush.TryPush.load_by_obj(
            self.git_gecko, self.sync_type, int(self.process_name.obj_id)
        )

        # I tried cast(Set[TryPush], try_pushes) here but it didn't work

        if status is not None:
            try_pushes_for_status: set[TryPush] = set()
            for item in try_pushes:
                assert isinstance(item, trypush.TryPush)
                if item.status == status:
                    try_pushes_for_status.add(item)
        else:
            try_pushes_for_status = try_pushes
        return list(sorted(try_pushes_for_status, key=lambda x: x.process_name.seq_id))

    def latest_busted_try_pushes(self) -> list[TryPush]:
        try_pushes = self.try_pushes(status="complete")
        busted = []
        for push in reversed(try_pushes):
            if push.infra_fail:
                busted.append(push)
            else:
                break
        return busted

    @property
    def latest_try_push(self) -> TryPush | None:
        try_pushes = self.try_pushes()
        if try_pushes:
            try_pushes = sorted(try_pushes, key=lambda x: x.process_name.seq_id)
            return try_pushes[-1]
        return None

    def wpt_renames(self) -> dict[str, str]:
        renames = {}
        diff_blobs = self.wpt_commits.head.commit.diff(
            self.git_wpt.merge_base(self.data["wpt-base"], self.wpt_commits.head.sha1)[0]
        )
        for item in diff_blobs:
            if item.rename_from:
                renames[item.rename_from] = item.rename_to
        return renames

    @classmethod
    @constructor(
        lambda args: (
            args["cls"].sync_type,
            args["bug"] if args["cls"].obj_id == "bug" else str(args["pr"]),
        )
    )
    def new(
        cls,
        lock: SyncLock,
        git_gecko: Repo,
        git_wpt: Repo,
        gecko_base: str,
        gecko_head: str,
        wpt_base: str = "origin/master",
        wpt_head: str | None = None,
        bug: int | None = None,
        pr: int | None = None,
        status: str = "open",
    ) -> SyncProcess:
        # TODO: this object creation is extremely non-atomic :/
        from . import index

        if cls.obj_id == "bug":
            assert bug is not None
            obj_id = str(bug)
        elif cls.obj_id == "pr":
            assert pr is not None
            obj_id = str(pr)
        else:
            raise ValueError("Invalid cls.obj_id: %s" % cls.obj_id)

        if wpt_head is None:
            wpt_head = wpt_base

        data = {
            "gecko-base": gecko_base,
            "wpt-base": wpt_base,
            "pr": pr,
            "bug": bug,
            "status": status,
        }

        # This is pretty ugly
        process_name = ProcessName.with_seq_id(
            git_gecko, cls.obj_type, cls.sync_type, obj_id
        )
        if not cls.multiple_syncs and process_name.seq_id != 0:
            raise ValueError(
                "Tried to create new {} sync for {} {} but one already exists".format(
                    cls.obj_id, cls.sync_type, obj_id
                )
            )

        commit_builder = CommitBuilder(git_gecko,
                                       f"Create sync {process_name}\n",
                                       env.config["sync"]["ref"])

        refs = [BranchRefObject.create(lock,
                                       git_gecko,
                                       process_name,
                                       gecko_head,
                                       GeckoCommit,
                                       force=True),
                BranchRefObject.create(lock,
                                       git_wpt,
                                       process_name,
                                       wpt_head,
                                       WptCommit,
                                       force=True)]

        try:
            # This will commit all the data in a single commit when we exit the "with" block
            with commit_builder:
                SyncData.create(lock,
                                git_gecko,
                                process_name,
                                data,
                                commit_builder=commit_builder)
                sync_idx_key = (process_name, status)
                idxs: list[tuple[Any, type[Index]]] = [(sync_idx_key, index.SyncIndex)]
                if cls.obj_id == "bug":
                    idxs.append(((process_name, status), index.BugIdIndex))
                elif cls.obj_id == "pr":
                    idxs.append((process_name, index.PrIdIndex))
                for (key, idx_cls) in idxs:
                    idx = idx_cls(git_gecko)
                    idx.insert(idx.make_key(key), process_name).save(commit_builder)
        except Exception:
            for ref in refs:
                ref.delete()
            raise

        return cls(git_gecko, git_wpt, process_name)

    @mut()
    def finish(self, status: str = "complete") -> None:
        # TODO: cancel related try pushes &c.
        logger.info(f"Marking sync {self.process_name} as {status}")
        self.status = status
        self.error = None
        for worktree in [self.gecko_worktree, self.wpt_worktree]:
            worktree.delete()
        for repo in [self.git_gecko, self.git_wpt]:
            repo.git.worktree("prune")

    @mut()
    def gecko_rebase(self, new_base_ref: str, abort_on_fail: bool = False) -> None:
        new_base = GeckoCommit(self.git_gecko, new_base_ref)
        git_worktree = self.gecko_worktree.get()
        set_new_base = True
        try:
            git_worktree.git.rebase(new_base.sha1)
        except git.GitCommandError as e:
            if abort_on_fail:
                set_new_base = False
                try:
                    git_worktree.git.rebase(abort=True)
                except git.GitCommandError:
                    pass
            raise AbortError("Rebasing onto latest gecko failed:\n%s" % e)
        finally:
            if set_new_base:
                self.data["gecko-base"] = new_base_ref
                self.gecko_commits.base = new_base.sha1

    @mut()
    def wpt_rebase(self, ref: str) -> None:
        assert ref in self.git_wpt.references
        git_worktree = self.wpt_worktree.get()
        git_worktree.git.rebase(ref)
        self.set_wpt_base(ref)

    @mut()
    def set_bug_data(self, status: str | None = None) -> None:
        if self.bug:
            whiteboard = env.bz.get_whiteboard(self.bug)
            if not whiteboard:
                return
            current_subtype, current_status = bug.get_sync_data(whiteboard)
            if current_subtype != self.sync_type or current_status != status:
                new_whiteboard = bug.set_sync_data(whiteboard, self.sync_type, status)
                env.bz.set_whiteboard(self.bug, new_whiteboard)

    @mut()
    def delete(self) -> None:
        from . import index

        for worktree in [self.gecko_worktree, self.wpt_worktree]:
            worktree.delete()

        assert self._lock is not None
        for try_push in self.try_pushes():
            with try_push.as_mut(self._lock):
                try_push.delete()

        for git_repo, commit_cls in [
            (self.git_wpt, WptCommit),
            (self.git_gecko, GeckoCommit),
        ]:
            BranchRefObject(git_repo, self.process_name, commit_cls=commit_cls).delete()

        for idx_cls in [index.SyncIndex, index.PrIdIndex, index.BugIdIndex]:
            key = idx_cls.make_key(self)
            idx = idx_cls(self.git_gecko)
            idx.delete(key, self.process_name).save()

        self.data.delete()
