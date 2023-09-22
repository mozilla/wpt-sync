from __future__ import annotations
import json
import sys
import weakref
from collections import defaultdict
from collections.abc import Mapping

import git
import pygit2

from . import log
from . import commit as sync_commit
from .env import Environment
from .lock import MutGuard, RepoLock, mut, constructor
from .repos import pygit2_get

from typing import (Any,
                    DefaultDict,
                    Iterator,
                    Optional,
                    Set,
                    Tuple,
                    TYPE_CHECKING)
from git.refs.reference import Reference
from git.repo.base import Repo
if TYPE_CHECKING:
    from pygit2.repository import Repository
    from pygit2 import Commit as PyGit2Commit, TreeEntry
    from sync.commit import Commit
    from sync.landing import LandingSync
    from sync.lock import SyncLock
    from sync.sync import SyncPointName

ProcessNameIndexData = DefaultDict[str, DefaultDict[str, DefaultDict[str, Set]]]
ProcessNameKey = Tuple[str, str, str, str]

env = Environment()

logger = log.get_logger(__name__)


class IdentityMap(type):
    """Metaclass for objects that implement an identity map.

    Identity mapped objects return the same object when created
    with the same input data, so that there can only be a single
    object with given properties active at a time.

    Typically one would expect the identity in the identity map
    to be determined by the full set of arguments to the constructor.
    However we have various situations where either global singletons
    are passed in via the constructor (e.g. the repos), or associated
    class data (notably the commit type). To avoid refactoring
    everything when introducing this feature, we instead define the
    following protocol:

    A class implementing this metaclass must define a _cache_key
    method that is called with the constructor arguments. It returns
    some hashable object that is used as a key in the instance cache.
    If an existing instance of the same class exists with the same
    key that is returned, otherwise a new instance is constructed
    and cached.

    A class may also define a _cache_verify property. If this method
    exists it is called after an instance is retrieved from the cache
    and may be used to check that any arguments that are not part of
    the constructor key are consistent between the provided values
    and the instance values. This is clearly a hack; in general
    the code should be refactored so that such associated values are
    determined based on data that forms part of the instance key rather
    than passed in explicitly."""

    _cache: weakref.WeakValueDictionary = weakref.WeakValueDictionary()

    def __init__(cls, name, bases, cls_dict):
        if not hasattr(cls, "_cache_key"):
            raise ValueError("Class is missing _cache_key method")
        super().__init__(name, bases, cls_dict)

    def __call__(cls, *args, **kwargs):
        cache = IdentityMap._cache
        cache_key = cls._cache_key(*args, **kwargs)
        if cache_key is None:
            raise ValueError
        key = (cls, cache_key)
        value = cache.get(key)
        if value is None:
            value = super().__call__(*args, **kwargs)
            cache[key] = value
        if hasattr(value, "_cache_verify") and not value._cache_verify(*args, **kwargs):
            raise ValueError("Cached instance didn't match non-key arguments")
        return value


def iter_tree(pygit2_repo: Repository,
              root_path: str = "",
              rev: PyGit2Commit | None = None,
              ) -> Iterator[tuple[tuple[str, ...], TreeEntry]]:
    """Iterator over all paths in a tree"""
    if rev is None:
        ref_name = env.config["sync"]["ref"]
        ref = pygit2_repo.references[ref_name]
        rev_obj = ref.peel()
    else:
        rev_obj = pygit2_repo[rev.id]

    assert isinstance(rev_obj, pygit2.Commit)
    root_obj = rev_obj.tree

    if root_path:
        root_tree = root_obj[root_path]
    else:
        root_tree = root_obj

    stack = []
    stack.append((root_path, root_tree))

    while stack:
        path, tree = stack.pop()
        assert isinstance(tree, pygit2.Tree)
        for item in tree:
            item_path = f"{path}/{item.name}"
            if isinstance(item, pygit2.Tree):
                stack.append((item_path, item))
            else:
                name = tuple(item for item in item_path[len(root_path):].split("/")
                             if item)
                yield name, item


def iter_process_names(pygit2_repo: Repository,
                       kind: list[str] = ["sync", "try"],
                       ) -> Iterator[ProcessName]:
    """Iterator over all ProcessName objects"""
    ref = pygit2_repo.references[env.config["sync"]["ref"]]
    root = ref.peel().tree
    stack = []
    for root_path in kind:
        try:
            tree = root[root_path]
        except KeyError:
            continue

        stack.append((root_path, tree))

    while stack:
        path, tree = stack.pop()
        for item in tree:
            item_path = f"{path}/{item.name}"
            if isinstance(item, pygit2.Tree):
                stack.append((item_path, item))
            else:
                process_name = ProcessName.from_path(item_path)
                if process_name is not None:
                    yield process_name


class ProcessNameIndex(metaclass=IdentityMap):
    def __init__(self, repo: Repo) -> None:
        self.repo = repo
        self.pygit2_repo = pygit2_get(repo)
        self.reset()

    @classmethod
    def _cache_key(cls, repo: Repo) -> tuple[Repo]:
        return (repo,)

    def reset(self) -> None:
        self._all: set[ProcessName] = set()
        self._data: ProcessNameIndexData = defaultdict(
            lambda: defaultdict(
                lambda: defaultdict(set)))
        self._built = False

    def build(self) -> None:
        for process_name in iter_process_names(self.pygit2_repo):
            self.insert(process_name)
        self._built = True

    def insert(self, process_name: ProcessName) -> None:
        self._all.add(process_name)

        self._data[
            process_name.obj_type][
                process_name.subtype][
                    process_name.obj_id].add(process_name)

    def has(self, process_name: ProcessName) -> bool:
        if not self._built:
            self.build()
        return process_name in self._all

    def get(self, obj_type: str, subtype: str | None = None,
            obj_id: str | None = None) -> set[ProcessName]:
        if not self._built:
            self.build()

        target = self._data
        for key in [obj_type, subtype, obj_id]:
            assert isinstance(key, str)
            if key is None:
                break
            target = target[key]  # type: ignore

        rv: set[ProcessName] = set()
        stack = [target]

        while stack:
            item = stack.pop()
            if isinstance(item, set):
                rv |= item
            else:
                stack.extend(item.values())  # type: ignore

        return rv


class ProcessName(metaclass=IdentityMap):
    """Representation of a name that is used to identify a sync operation.
    This has the general form <obj type>/<subtype>/<obj_id>[/<seq_id>].

    Here <obj type> represents the type of process e.g upstream or downstream,
    <obj_id> is an identifier for the sync,
    typically either a bug number or PR number, and <seq_id> is an optional id
    to cover cases where we might have multiple processes with the same obj_id.

    """

    def __init__(self, obj_type: str, subtype: str, obj_id: str,
                 seq_id: str | int) -> None:
        assert obj_type is not None
        assert subtype is not None
        assert obj_id is not None
        assert seq_id is not None

        self._obj_type = obj_type
        self._subtype = subtype
        self._obj_id = str(obj_id)
        self._seq_id = str(seq_id)

    @classmethod
    def _cache_key(cls,
                   obj_type: str,
                   subtype: str,
                   obj_id: str,
                   seq_id: str | int,
                   ) -> tuple[str, str, str, str]:
        return (obj_type, subtype, str(obj_id), str(seq_id))

    def __str__(self) -> str:
        data = "%s/%s/%s/%s" % self.as_tuple()
        if sys.version_info[0] == 2:
            data = data.encode("utf8")
        return data

    def key(self) -> tuple[str, str, str, str]:
        return self._cache_key(self._obj_type, self._subtype, self._obj_id, self._seq_id)

    def path(self) -> str:
        return "%s/%s/%s/%s" % self.as_tuple()

    def __eq__(self, other: Any) -> bool:
        if self is other:
            return True
        if self.__class__ != other.__class__:
            return False
        return self.as_tuple() == other.as_tuple()

    def __hash__(self) -> int:
        return hash(self.key())

    @property
    def obj_type(self) -> str:
        return self._obj_type

    @property
    def subtype(self) -> str:
        return self._subtype

    @property
    def obj_id(self) -> str:
        return self._obj_id

    @property
    def seq_id(self) -> int:
        return int(self._seq_id)

    def as_tuple(self) -> tuple[str, str, str, int]:
        return (self.obj_type, self.subtype, self.obj_id, self.seq_id)

    @classmethod
    def from_path(cls, path: str) -> ProcessName | None:
        return cls.from_tuple(path.split("/"))

    @classmethod
    def from_tuple(cls, parts: list[str]) -> ProcessName | None:
        if parts[0] not in ["sync", "try"]:
            return None
        if len(parts) != 4:
            return None
        return cls(*parts)

    @classmethod
    def with_seq_id(cls, repo: Repo, obj_type: str, subtype: str, obj_id: str) -> ProcessName:
        existing = ProcessNameIndex(repo).get(obj_type, subtype, obj_id)
        last_id = -1
        for process_name in existing:
            if (process_name.seq_id is not None and
                int(process_name.seq_id) > last_id):
                last_id = process_name.seq_id
        seq_id = last_id + 1
        return cls(obj_type, subtype, obj_id, str(seq_id))


class VcsRefObject(metaclass=IdentityMap):
    """Representation of a named reference to a git object associated with a
    specific process_name.

    This is typically either a tag or a head (i.e. branch), but can be any
    git object."""

    ref_prefix: str | None = None

    def __init__(self, repo: Repo, name: ProcessName | SyncPointName,
                 commit_cls: type = sync_commit.Commit) -> None:
        self.repo = repo
        self.pygit2_repo = pygit2_get(repo)

        if not self.get_path(name) in self.pygit2_repo.references:
            raise ValueError("No ref found in %s with path %s" %
                             (repo.working_dir, self.get_path(name)))
        self.name = name
        self.commit_cls = commit_cls
        self._lock = None

    def as_mut(self, lock: SyncLock) -> MutGuard:
        return MutGuard(lock, self)

    @property
    def lock_key(self) -> tuple[str, str]:
        return (self.name.subtype, self.name.obj_id)

    @classmethod
    def _cache_key(cls,
                   repo: Repo,
                   process_name: ProcessName | SyncPointName,
                   commit_cls: type = sync_commit.Commit,
                   ) -> tuple[Repo, ProcessNameKey | tuple[str, str]]:
        return (repo, process_name.key())

    def _cache_verify(self, repo: Repo, process_name: ProcessName | SyncPointName,
                      commit_cls: type = sync_commit.Commit) -> bool:
        return commit_cls == self.commit_cls

    @classmethod
    @constructor(lambda args: (args["name"].subtype,
                               args["name"].obj_id))
    def create(cls,
               lock: SyncLock,
               repo: Repo,
               name: ProcessName, obj: str,
               commit_cls: type = sync_commit.Commit,
               force: bool = False) -> VcsRefObject:
        path = cls.get_path(name)
        logger.debug("Creating ref %s" % path)
        pygit2_repo = pygit2_get(repo)
        if path in pygit2_repo.references:
            if not force:
                raise ValueError(f"Ref {path} exists")
        pygit2_repo.references.create(path,
                                      pygit2_repo.revparse_single(obj).id,
                                      force=force)
        return cls(repo, name, commit_cls)

    def __str__(self) -> str:
        return self.path

    def delete(self) -> None:
        self.pygit2_repo.references[self.path].delete()

    @classmethod
    def get_path(cls, name: ProcessName | SyncPointName) -> str:
        return f"refs/{cls.ref_prefix}/{name.path()}"

    @property
    def path(self) -> str:
        return self.get_path(self.name)

    @property
    def ref(self) -> Reference | None:
        if self.path in self.pygit2_repo.references:
            return git.Reference(self.repo, self.path)
        return None

    @property
    def commit(self) -> Commit | None:
        ref = self.ref
        if ref is not None:
            commit = self.commit_cls(self.repo, ref.commit)
            return commit
        return None

    @commit.setter  # type: ignore
    @mut()
    def commit(self, commit: Commit | str) -> None:
        if isinstance(commit, sync_commit.Commit):
            sha1 = commit.sha1
        else:
            sha1 = commit
        sha1 = self.pygit2_repo.revparse_single(sha1).id
        self.pygit2_repo.references[self.path].set_target(sha1)


class BranchRefObject(VcsRefObject):
    ref_prefix = "heads"


class CommitBuilder:
    def __init__(self,
                 repo: Repo,
                 message: str,
                 ref: str | None = None,
                 commit_cls: type = sync_commit.Commit,
                 initial_empty: bool = False
                 ) -> None:
        """Object to be used as a context manager for commiting changes to the repo.

        This class provides low-level access to the git repository in order to
        make commits without requiring a checkout. It also enforces locking so that
        only one process may make a commit at a time.

        In order to use the object, one initalises it and then invokes it as a context
        manager e.g.

        with CommitBuilder(repo, "Some commit message" ref=ref) as commit_builder:
            # Now we have acquired the lock so that the commit ref points to is fixed
            commit_builder.add_tree({"some/path": "Some file data"})
            commit_bulder.delete(["another/path"])
        # On exiting the context, the commit is created, the ref updated to point at the
        # new commit and the lock released

        # To get the created commit we call get
        commit = commit_builder.get()

        The class may be used reentrantly. This is to support a pattern where a method
        may be called either with an existing commit_builder instance or create a new
        instance, and in either case use a with block.

        In order to improve the performance of the low-level access here, we use libgit2
        to access the repository.
        """
        # Class state
        self.repo = repo
        self.pygit2_repo = pygit2_get(repo)
        self.message = message if message is not None else ""
        self.commit_cls = commit_cls
        self.initial_empty = initial_empty
        if not ref:
            self.ref = None
        else:
            self.ref = ref

        self._count = 0

        # State set for the life of the context manager
        self.lock = RepoLock(repo)
        self.parents: list[str] | None = None
        self.commit = None
        self.index: pygit2.Index = None
        self.has_changes = False

    def __enter__(self) -> CommitBuilder:
        self._count += 1
        if self._count != 1:
            return self

        self.lock.__enter__()

        # First we create an empty index
        self.index = pygit2.Index()

        if self.ref is not None:
            try:
                ref = self.pygit2_repo.lookup_reference(self.ref)
            except KeyError:
                self.parents = []
            else:
                self.parents = [ref.peel().id]
                if not self.initial_empty:
                    self.index.read_tree(ref.peel().tree)
        else:
            self.parents = []
        return self

    def __exit__(self, *args: Any, **kwargs: Any) -> None:
        self._count -= 1
        if self._count != 0:
            return

        if not self.has_changes:
            if self.parents:
                sha1 = self.parents[0]
            else:
                return None
        else:
            tree_id = self.index.write_tree(self.pygit2_repo)

            sha1 = self.pygit2_repo.create_commit(self.ref,
                                                  self.pygit2_repo.default_signature,
                                                  self.pygit2_repo.default_signature,
                                                  self.message.encode("utf8"),
                                                  tree_id,
                                                  self.parents)
        self.lock.__exit__(*args, **kwargs)
        self.commit = self.commit_cls(self.repo, sha1)

    def add_tree(self, tree: dict[str, bytes]) -> None:
        self.has_changes = True
        for path, data in tree.items():
            blob = self.pygit2_repo.create_blob(data)
            index_entry = pygit2.IndexEntry(path, blob, pygit2.GIT_FILEMODE_BLOB)
            self.index.add(index_entry)

    def delete(self, delete: list[str]) -> None:
        self.has_changes = True
        if delete:
            for path in delete:
                self.index.remove(path)

    def get(self) -> Any | None:
        return self.commit


class ProcessData(metaclass=IdentityMap):
    obj_type: str = ""

    def __init__(self, repo: Repo, process_name: ProcessName) -> None:
        assert process_name.obj_type == self.obj_type
        self.repo = repo
        self.pygit2_repo = pygit2_get(repo)
        self.process_name = process_name
        self.ref = git.Reference(repo, env.config["sync"]["ref"])
        self.path = self.get_path(process_name)
        self._data = self._load()
        self._lock = None
        self._updated: set[str] = set()
        self._deleted: set[str] = set()
        self._delete = False

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} {self.process_name}>"

    def __hash__(self) -> int:
        return hash(self.process_name)

    def __eq__(self, other: Any) -> bool:
        if type(self) is not type(other):
            return False
        return self.repo == other.repo and self.process_name == other.process_name

    def as_mut(self, lock: SyncLock) -> MutGuard:
        return MutGuard(lock, self)

    def exit_mut(self) -> None:
        message = "Update %s\n\n" % self.path
        with CommitBuilder(self.repo, message=message, ref=self.ref.path) as commit:
            from . import index
            if self._delete:
                self._delete_data("Delete %s" % self.path)
                self._delete = False

            elif self._updated or self._deleted:
                message_parts = []
                if self._updated:
                    message_parts.append("Updated: {}\n".format(", ".join(self._updated)))
                if self._deleted:
                    message_parts.append("Deleted: {}\n".format(", ".join(self._deleted)))
                self._save(self._data, message=" ".join(message_parts), commit_builder=commit)

            self._updated = set()
            self._deleted = set()

            for idx_cls in index.indicies:
                idx = idx_cls(self.repo)
                idx.save(commit_builder=commit)

    @classmethod
    @constructor(lambda args: (args["process_name"].subtype,
                               args["process_name"].obj_id))
    def create(cls,
               lock: SyncLock,
               repo: Repo,
               process_name: ProcessName,
               data: dict[str, Any],
               message: str = "Sync data",
               commit_builder: Optional[CommitBuilder] = None
               ) -> ProcessData:
        assert process_name.obj_type == cls.obj_type
        path = cls.get_path(process_name)

        ref = git.Reference(repo, env.config["sync"]["ref"])

        try:
            ref.commit.tree[path]
        except KeyError:
            pass
        else:
            raise ValueError(f"{cls.__name__} already exists at path {path}")

        if commit_builder is None:
            commit_builder = CommitBuilder(repo, message, ref=ref.path)
        else:
            assert commit_builder.ref == ref.path

        with commit_builder as commit:
            commit.add_tree({path: json.dumps(data).encode("utf8")})
        ProcessNameIndex(repo).insert(process_name)
        return cls(repo, process_name)

    @classmethod
    def _cache_key(cls, repo: Repo, process_name: ProcessName) -> tuple[Repo, ProcessNameKey]:
        return (repo, process_name.key())

    @classmethod
    def get_path(self, process_name: ProcessName) -> str:
        return process_name.path()

    @classmethod
    def load_by_obj(cls,
                    repo: Repo,
                    subtype: str,
                    obj_id: int,
                    seq_id=None  # Type: Optional[int]
                    ) -> set[ProcessData]:
        process_names = ProcessNameIndex(repo).get(cls.obj_type,
                                                   subtype,
                                                   str(obj_id))
        if seq_id is not None:
            process_names = {item for item in process_names
                             if item.seq_id == seq_id}
        return {cls(repo, process_name) for process_name in process_names}

    @classmethod
    def load_by_status(cls, repo, subtype, status):
        from . import index
        process_names = index.SyncIndex(repo).get((cls.obj_type,
                                                   subtype,
                                                   status))
        rv = set()
        for process_name in process_names:
            rv.add(cls(repo, process_name))
        return rv

    def _save(self, data: dict[str, Any], message: str,
              commit_builder: CommitBuilder | None = None) -> Any | None:
        if commit_builder is None:
            commit_builder = CommitBuilder(self.repo, message=message, ref=self.ref.path)
        else:
            commit_builder.message += message
        tree = {self.path: json.dumps(data).encode("utf8")}
        with commit_builder as commit:
            commit.add_tree(tree)
        return commit.get()

    def _delete_data(self, message: str, commit_builder: CommitBuilder | None = None) -> None:
        if commit_builder is None:
            commit_builder = CommitBuilder(self.repo, message=message, ref=self.ref.path)
        with commit_builder as commit:
            commit.delete([self.path])

    def _load(self) -> dict[str, Any]:
        ref = self.pygit2_repo.references[self.ref.path]
        repo = self.pygit2_repo
        try:
            data = repo[repo[ref.peel().tree.id][self.path].id].data
        except KeyError:
            return {}
        return json.loads(data)

    @property
    def lock_key(self) -> tuple[str, str]:
        return (self.process_name.subtype, self.process_name.obj_id)

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def items(self) -> Iterator[tuple[str, Any]]:
        yield from self._data.items()

    @mut()
    def __setitem__(self, key: str, value: Any) -> None:
        if key not in self._data or self._data[key] != value:
            self._data[key] = value
            self._updated.add(key)

    @mut()
    def __delitem__(self, key: str) -> None:
        if key in self._data:
            del self._data[key]
            self._deleted.add(key)

    @mut()
    def delete(self) -> None:
        self._delete = True


class FrozenDict(Mapping):
    def __init__(self, **kwargs: Any) -> None:
        self._data = {}
        for key, value in kwargs.items():
            self._data[key] = value

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __contains__(self, key: Any) -> bool:
        return key in self._data

    def copy(self, **kwargs: Any) -> FrozenDict:
        new_data = self._data.copy()
        for key, value in kwargs.items():
            new_data[key] = value
        return self.__class__(**new_data)

    def __iter__(self) -> Iterator[str]:
        yield from self._data

    def __len__(self) -> int:
        return len(self._data)

    def as_dict(self) -> dict[str, Any]:
        return self._data.copy()


class entry_point:
    def __init__(self, task):
        self.task = task

    def __call__(self, f):
        def inner(*args: Any, **kwargs: Any) -> LandingSync | None:
            logger.info(f"Called entry point {f.__module__}.{f.__name__}")
            logger.debug(f"Called args {args!r} kwargs {kwargs!r}")

            if self.task in env.config["sync"]["enabled"]:
                return f(*args, **kwargs)

            logger.debug("Skipping disabled task %s" % self.task)
            return None

        inner.__name__ = f.__name__
        inner.__doc__ = f.__doc__
        return inner
