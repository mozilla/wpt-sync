from __future__ import annotations
import abc
import json
from collections import defaultdict

import git
import pygit2

from . import log
from .base import ProcessName, CommitBuilder, iter_tree, iter_process_names
from .env import Environment
from .repos import pygit2_get

from typing import Any, Callable, Iterator, Optional, Tuple, Union, TYPE_CHECKING
from git.repo.base import Repo
from pygit2.repository import Repository
if TYPE_CHECKING:
    from sync.sync import SyncProcess
ChangeEntry = Tuple[str, str, str]
IndexKey = Tuple[str, ...]
IndexValue = Union[str, ProcessName]


logger = log.get_logger(__name__)
env = Environment()


class Index(metaclass=abc.ABCMeta):
    name: str | None = None
    key_fields: tuple[str, ...]
    unique = False
    value_cls: type = tuple

    # Overridden in subclasses using the constructor
    # This provides a kind of borg pattern where all instances of
    # the class have the same changes data
    changes: dict[Any, Any] | None = None

    def __init__(self, repo: Repo) -> None:
        if self.__class__.changes is None:
            self.reset()
        self.repo = repo
        self.pygit2_repo = pygit2_get(repo)

    def reset(self) -> None:
        constructors: list[Callable[[], Any]] = [list]
        for _ in self.key_fields[:-1]:
            def fn() -> Callable:
                idx = len(constructors) - 1
                constructors.append(lambda: defaultdict(constructors[idx]))
                return constructors[-1]
            fn()
        self.__class__.changes = defaultdict(constructors[-1])

    @classmethod
    def create(cls, repo: Repo) -> Index:
        logger.info("Creating index %s" % cls.name)
        data = {"name": cls.name,
                "fields": list(cls.key_fields),
                "unique": cls.unique}
        meta_path = "index/%s/_metadata" % cls.name
        tree = {meta_path: json.dumps(data, indent=0).encode("utf8")}
        with CommitBuilder(repo,
                           message="Create index %s" % cls.name,
                           ref=env.config["sync"]["ref"]) as commit:
            commit.add_tree(tree)
        return cls(repo)

    @classmethod
    def get_root_path(cls) -> str:
        return "index/%s" % cls.name

    @classmethod
    def get_or_create(cls, repo: Repo) -> Index:
        ref_name = env.config["sync"]["ref"]
        ref = git.Reference(repo, ref_name)
        try:
            ref.commit.tree["index/%s" % cls.name]
        except KeyError:
            return cls.create(repo)
        return cls(repo)

    def get(self, key: IndexKey) -> Any:
        if len(key) > len(self.key_fields):
            raise ValueError

        assert all(isinstance(key_part, str) for key_part in key)

        items = self._read(key)
        if self.unique and len(key) == len(self.key_fields) and len(items) > 1:
            raise ValueError("Got multiple results for unique index")

        rv = set()
        for item in items:
            rv.add(self.load_value(item))

        if self.unique:
            return rv.pop() if rv else None
        return rv

    def _read(self,
              key: IndexKey,
              include_local: bool = True,
              ) -> set[str]:
        path = "{}/{}".format(self.get_root_path(), "/".join(key))
        data = set()
        for obj in iter_blobs(self.pygit2_repo, path):
            data |= self._load_obj(obj)
        if include_local:
            self._update_changes(key, data)
        return data

    def _read_changes(self, key: IndexKey | None) -> dict[IndexKey, list[ChangeEntry]]:
        target = self.changes
        if target is None:
            return {}
        if key:
            for part in key:
                if target is not None:
                    target = target[part]
        else:
            key = ()
        changes: dict[IndexKey, list[ChangeEntry]] = {}
        stack = [(key, target)]
        while stack:
            key, items = stack.pop()
            if isinstance(items, list):
                changes[key] = items
            else:
                if items is not None:
                    for key_part, values in items.items():
                        stack.append((key + (key_part,), values))
        return changes

    def _update_changes(self,
                        key: IndexKey,
                        data: set[str],
                        ) -> None:
        changes = self._read_changes(key)
        for key_changes in changes.values():
            for old_value, new_value, _ in key_changes:
                if new_value is None and old_value in data:
                    data.remove(old_value)
                elif new_value is not None:
                    data.add(new_value)

    def _load_obj(self, obj: pygit2.Blob) -> set[str]:
        rv = json.loads(obj.data)
        if isinstance(rv, list):
            return set(rv)
        return {rv}

    def save(self,
             commit_builder: CommitBuilder | None = None,
             message: str | None = None,
             overwrite: bool = False) -> None:
        changes = self._read_changes(None)
        if not changes:
            return

        if message is None:
            message = "Update index %s\n" % self.name

            for key_changes in changes.values():
                for _, _, msg in key_changes:
                    message += "  %s\n" % msg

        if commit_builder is None:
            # TODO: @overload could help here
            assert message is not None
            commit_builder = CommitBuilder(self.repo,
                                           message,
                                           ref=env.config["sync"]["ref"],
                                           initial_empty=overwrite)
        else:
            assert commit_builder.initial_empty == overwrite
            commit_builder.message += message

        with commit_builder as commit:
            for key, key_changes in changes.items():
                self._update_key(commit, key, key_changes)
        self.reset()

    def insert(self,
               key: IndexKey,
               value: IndexValue,
               ) -> Index:
        if len(key) != len(self.key_fields):
            raise ValueError

        assert all(isinstance(item, str) for item in key)

        value = self.dump_value(value)
        msg = f"Insert key {key} value {value}"
        target = self.changes
        for part in key:
            if target is not None:
                target = target[part]
        assert isinstance(target, list)
        target.append((None, value, msg))
        return self

    def delete(self,
               key: IndexKey,
               value: IndexValue,
               ) -> Index:
        if len(key) != len(self.key_fields):
            raise ValueError

        assert all(isinstance(item, str) for item in key)

        value = self.dump_value(value)
        msg = f"Delete key {key} value {value}"
        target = self.changes
        for part in key:
            if target is not None:
                target = target[part]
        assert isinstance(target, list)
        target.append((value, None, msg))
        return self

    def move(self,
             old_key: IndexKey | None,
             new_key: IndexKey,
             value: IndexValue,
             ) -> Index:
        assert old_key != new_key

        if old_key is not None:
            self.delete(old_key, value)
        if new_key is not None:
            self.insert(new_key, value)
        return self

    def _update_key(self,
                    commit: CommitBuilder,
                    key: IndexKey,
                    key_changes: list[ChangeEntry],
                    ) -> None:
        existing = self._read(key, False)
        new = existing.copy()

        for old_value, new_value, _ in key_changes:
            if new_value is None and old_value is None:
                new = set()
            elif new_value is None and old_value in new:
                new.remove(old_value)
            elif old_value is None:
                new.add(new_value)

        path_suffix = "/".join(key)

        path = f"{self.get_root_path()}/{path_suffix}"

        if new == existing:
            return

        if not new:
            commit.delete([path])
            return

        if self.unique and len(new) > 1:
            raise ValueError(f"Tried to insert duplicate entry for unique index {key}")

        index_value = list(sorted(new))

        commit.add_tree({path: json.dumps(index_value, indent=0).encode("utf8")})

    def dump_value(self, value: IndexValue) -> str:
        if isinstance(value, ProcessName):
            return value.path()
        return value

    def load_value(self, value: str) -> IndexValue:
        return self.value_cls(*(value.split("/")))

    def build(self, *args: Any, **kwargs: Any) -> None:
        # Delete all entries in existing keys
        for key in self.keys():
            assert len(key) == len(self.key_fields)
            target = self.changes
            for part in key:
                if target is not None:
                    target = target[part]
            assert isinstance(target, list)
            target.append((None, None, f"Clear key {key}"))
        entries, errors = self.build_entries(*args, **kwargs)
        for key, value in entries:
            self.insert(key, value)
        self.save(message="Build index %s" % self.name)
        for error in errors:
            logger.warning(error)

    def build_entries(self, *args: Any, **kwargs: Any) -> tuple[list[tuple[IndexKey, ProcessName]], list[str]]:
        raise NotImplementedError

    @classmethod
    def make_key(cls, value: Any) -> IndexKey:
        return (value,)

    def keys(self) -> set[IndexKey]:
        return {key for key, _ in
                iter_tree(self.pygit2_repo, root_path=self.get_root_path())
                if not key[-1] == "_metadata"}


class TaskGroupIndex(Index):
    name = "taskgroup"
    key_fields = ("taskgroup-id-0", "taskgroup-id-1", "taskgroup-id-2")
    unique = True
    value_cls = ProcessName

    @classmethod
    def make_key(cls, value: str) -> IndexKey:
        return (value[:2],
                value[2:4],
                value[4:])

    def build_entries(self, *args: Any, **kwargs: Any) -> tuple[list[tuple[IndexKey, ProcessName]], list[str]]:
        from . import trypush
        entries = []
        for try_push in trypush.TryPush.load_all(self.repo):
            if try_push.taskgroup_id is not None:
                entries.append((self.make_key(try_push.taskgroup_id),
                                try_push.process_name))
        return entries, []


class TryCommitIndex(Index):
    name = "try-commit"
    key_fields = ("commit-0", "commit-1", "commit-2", "commit-3")
    unique = True
    value_cls = ProcessName

    @classmethod
    def make_key(cls, value: str) -> IndexKey:
        return (value[:2],
                value[2:4],
                value[4:6],
                value[6:])

    def build_entries(self, *args: Any, **kwargs: Any) -> tuple[list[tuple[IndexKey, ProcessName]], list[str]]:
        entries = []
        from . import trypush
        for try_push in trypush.TryPush.load_all(self.repo):
            if try_push.try_rev:
                entries.append((self.make_key(try_push.try_rev),
                                try_push.process_name))
        return entries, []


class SyncIndex(Index):
    name = "sync-id-status"
    key_fields = ("objtype", "subtype", "status", "obj_id")
    unique = False
    value_cls = ProcessName

    @classmethod
    def make_key(cls,
                 value: Union[SyncProcess, Tuple[ProcessName, str]],
                 ) -> IndexKey:
        if isinstance(value, tuple):
            process_name, status = value
        else:
            process_name = value.process_name
            status = value.status

        return (process_name.obj_type,
                process_name.subtype,
                status,
                str(process_name.obj_id))

    def build_entries(self, git_gecko: Repo, git_wpt: Repo, **kwargs: Any) -> tuple[list[tuple[IndexKey, ProcessName]], list[str]]:
        from .downstream import DownstreamSync
        from .upstream import UpstreamSync
        from .landing import LandingSync

        entries = []
        errors = []

        for process_name in iter_process_names(self.pygit2_repo, kind=["sync"]):
            sync_cls: Optional[type[UpstreamSync] | type[DownstreamSync] | type[LandingSync]] = None
            if process_name.subtype == "upstream":
                sync_cls = UpstreamSync
            elif process_name.subtype == "downstream":
                sync_cls = DownstreamSync
            elif process_name.subtype == "landing":
                sync_cls = LandingSync

            if sync_cls:
                try:
                    sync = sync_cls(git_gecko, git_wpt, process_name)
                except ValueError:
                    errors.append("Corrupt process %s" % process_name)
                    continue
                entries.append((self.make_key(sync), process_name))
        return entries, errors


class PrIdIndex(Index):
    name = "pr-id"
    key_fields = ("pr-id",)
    unique = True
    value_cls = ProcessName

    @classmethod
    def make_key(cls, value: Union[SyncProcess, ProcessName]) -> IndexKey:
        if isinstance(value, ProcessName):
            pr_id = value.obj_id
        else:
            pr_id = str(value.pr)
            assert pr_id is not None
        return (pr_id,)

    def build_entries(self, git_gecko: Repo, git_wpt: Repo, **kwargs: Any) -> tuple[list[tuple[IndexKey, ProcessName]], list[str]]:
        from .downstream import DownstreamSync
        from .upstream import UpstreamSync

        entries = []
        errors = []

        for process_name in iter_process_names(self.pygit2_repo, kind=["sync"]):
            cls: Optional[type[UpstreamSync] | type[DownstreamSync]] = None
            if process_name.subtype == "downstream":
                cls = DownstreamSync
            elif process_name.subtype == "upstream":
                cls = UpstreamSync
            if not cls:
                continue
            try:
                sync = cls(git_gecko, git_wpt, process_name)
            except ValueError:
                errors.append("Corrupt process %s" % process_name)
                continue
            if sync.pr:
                entries.append((self.make_key(sync), process_name))
        return entries, errors


class BugIdIndex(Index):
    name = "bug-id"
    key_fields = ("bug-id", "status")
    unique = False
    value_cls = ProcessName

    @classmethod
    def make_key(cls,
                 value: Union[SyncProcess, Tuple[ProcessName, str]],
                 ) -> IndexKey:
        if isinstance(value, tuple):
            process_name, status = value
            bug = process_name.obj_id
        else:
            assert value.bug is not None
            bug = str(value.bug)
            status = value.status
        return (bug, status)

    def build_entries(self, git_gecko: Repo, git_wpt: Repo, **kwargs: Any) -> tuple[list[tuple[IndexKey, ProcessName]], list[str]]:
        from .downstream import DownstreamSync
        from .upstream import UpstreamSync
        from .landing import LandingSync

        entries = []
        errors = []

        for process_name in iter_process_names(self.pygit2_repo, kind=["sync"]):
            sync_cls: Optional[type[UpstreamSync] | type[DownstreamSync] | type[LandingSync]] = None
            if process_name.subtype == "upstream":
                sync_cls = UpstreamSync
            elif process_name.subtype == "downstream":
                sync_cls = DownstreamSync
            elif process_name.subtype == "landing":
                sync_cls = LandingSync
            else:
                assert False

            try:
                sync = sync_cls(git_gecko, git_wpt, process_name)
            except ValueError as e:
                errors.append(f"Corrupt process {process_name}:\n{e}")
                continue
            entries.append((self.make_key(sync), process_name))
        return entries, errors


def iter_blobs(repo: Repository, path: str) -> Iterator[pygit2.Blob]:
    """Iterate over all blobs under a path

    :param repo: pygit2 repo
    :param path: path to use as the root, or None for the root path
    """
    ref = repo.references[env.config["sync"]["ref"]]
    root = ref.peel(None).tree
    if path is not None:
        if path not in root:
            return
        root_entry = root[path]
        if isinstance(root_entry, pygit2.Blob):
            yield root_entry
            return

    stack = [root_entry]
    while stack:
        tree = stack.pop()
        assert isinstance(tree, pygit2.Tree)
        for item in tree:
            if isinstance(item, pygit2.Blob):
                yield item
            else:
                stack.append(item)


indicies = {item for item in globals().values()
            if type(item) is type(Index) and
            issubclass(item, Index) and
            item != Index}
