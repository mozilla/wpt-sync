from __future__ import absolute_import
import json
import sys
import weakref
from collections import defaultdict, Mapping

import git
import pygit2
import six
from six import iteritems, itervalues

from . import log
from . import commit as sync_commit
from .env import Environment
from .lock import MutGuard, RepoLock, mut, constructor
from .repos import pygit2_get

MYPY = False
if MYPY:
    from git.refs.reference import Reference
    from git.repo.base import Repo
    from pygit2 import Commit as PyGit2Commit, TreeEntry
    from pygit2.repository import Repository
    from sync.commit import Commit
    from sync.landing import LandingSync
    from sync.lock import SyncLock
    from sync.sync import SyncPointName
    from typing import (Any,
                        DefaultDict,
                        Dict,
                        Iterator,
                        List,
                        Optional,
                        Set,
                        Text,
                        Tuple,
                        Union)

    ProcessNameIndexData = DefaultDict[Text, DefaultDict[Text, DefaultDict[Text, Set]]]
    ProcessNameKey = Tuple[Text, Text, Text, Text]

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

    _cache = weakref.WeakValueDictionary()  # type: weakref.WeakValueDictionary

    def __init__(cls, name, bases, cls_dict):
        if not hasattr(cls, "_cache_key"):
            raise ValueError("Class is missing _cache_key method")
        super(IdentityMap, cls).__init__(name, bases, cls_dict)

    def __call__(cls, *args, **kwargs):
        cache = IdentityMap._cache
        cache_key = cls._cache_key(*args, **kwargs)
        if cache_key is None:
            raise ValueError
        key = (cls, cache_key)
        value = cache.get(key)
        if value is None:
            value = super(IdentityMap, cls).__call__(*args, **kwargs)
            cache[key] = value
        if hasattr(value, "_cache_verify") and not value._cache_verify(*args, **kwargs):
            raise ValueError("Cached instance didn't match non-key arguments")
        return value


def iter_tree(pygit2_repo,  # type: Repository
              root_path="",  # type: Text
              rev=None,  # type: Optional[PyGit2Commit]
              ):
    # type: (...) -> Iterator[Tuple[Tuple[Text, ...], TreeEntry]]
    """Iterator over all paths ins a tree"""
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
            item_path = u"%s/%s" % (path, item.name)
            if isinstance(item, pygit2.Tree):
                stack.append((item_path, item))
            else:
                name = tuple(item for item in item_path[len(root_path):].split(u"/")
                             if item)
                yield name, item


def iter_process_names(pygit2_repo,  # type: Repository
                       kind=["sync", "try"],  # type: List[str]
                       ):
    # type: (...) -> Iterator[ProcessName]
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
            item_path = "%s/%s" % (path, item.name)
            if isinstance(item, pygit2.Tree):
                stack.append((item_path, item))
            else:
                process_name = ProcessName.from_path(item_path)
                if process_name is not None:
                    yield process_name


class ProcessNameIndex(six.with_metaclass(IdentityMap, object)):
    def __init__(self, repo):
        # type: (Repo) -> None
        self.repo = repo
        self.pygit2_repo = pygit2_get(repo)
        self.reset()

    @classmethod
    def _cache_key(cls, repo):
        # type: (Repo) -> Tuple[Repo]
        return (repo,)

    def reset(self):
        # type: () -> None
        self._all = set()  # type: Set[ProcessName]
        self._data = defaultdict(
            lambda: defaultdict(
                lambda: defaultdict(set)))  # type: ProcessNameIndexData
        self._built = False

    def build(self):
        # type: () -> None
        for process_name in iter_process_names(self.pygit2_repo):
            self.insert(process_name)
        self._built = True

    def insert(self, process_name):
        # type: (ProcessName) -> None
        self._all.add(process_name)

        self._data[
            process_name.obj_type][
                process_name.subtype][
                    process_name.obj_id].add(process_name)

    def has(self, process_name):
        # type: (ProcessName) -> bool
        if not self._built:
            self.build()
        return process_name in self._all

    def get(self, obj_type, subtype=None, obj_id=None):
        # type: (Text, Optional[Text], Optional[Text]) -> Set[ProcessName]
        if not self._built:
            self.build()

        target = self._data
        for key in [obj_type, subtype, obj_id]:
            assert isinstance(key, six.string_types)
            if key is None:
                break
            target = target[key]  # type: ignore

        rv = set()  # type: Set[ProcessName]
        stack = [target]

        while stack:
            item = stack.pop()
            if isinstance(item, set):
                rv |= item
            else:
                stack.extend(itervalues(item))  # type: ignore

        return rv


class ProcessName(six.with_metaclass(IdentityMap, object)):
    """Representation of a name that is used to identify a sync operation.
    This has the general form <obj type>/<subtype>/<obj_id>[/<seq_id>].

    Here <obj type> represents the type of process e.g upstream or downstream,
    <obj_id> is an identifier for the sync,
    typically either a bug number or PR number, and <seq_id> is an optional id
    to cover cases where we might have multiple processes with the same obj_id.

    """

    def __init__(self, obj_type, subtype, obj_id, seq_id):
        # type: (Text, Text, Text, Union[Text, int]) -> None
        assert obj_type is not None
        assert subtype is not None
        assert obj_id is not None
        assert seq_id is not None

        self._obj_type = obj_type
        self._subtype = subtype
        self._obj_id = six.ensure_text(str(obj_id))
        self._seq_id = six.ensure_text(str(seq_id))

    @classmethod
    def _cache_key(cls,
                   obj_type,  # type: Text
                   subtype,  # type: Text
                   obj_id,  # type: Text
                   seq_id,  # type: Union[Text, int]
                   ):
        # type: (...) -> Tuple[Text, Text, Text, Text]
        return (obj_type, subtype, six.ensure_text(str(obj_id)), six.ensure_text(str(seq_id)))

    def __str__(self):
        # type: () -> str
        data = u"%s/%s/%s/%s" % self.as_tuple()
        if sys.version_info[0] == 2:
            data = data.encode("utf8")
        return data

    def key(self):
        # type: () -> Tuple[Text, Text, Text, Text]
        return self._cache_key(self._obj_type, self._subtype, self._obj_id, self._seq_id)

    def path(self):
        # type: () -> Text
        return u"%s/%s/%s/%s" % self.as_tuple()

    def __eq__(self, other):
        # type: (Any) -> bool
        if self is other:
            return True
        if self.__class__ != other.__class__:
            return False
        return self.as_tuple() == other.as_tuple()

    def __hash__(self):
        # type: () -> int
        return hash(self.key())

    @property
    def obj_type(self):
        # type: () -> Text
        return self._obj_type

    @property
    def subtype(self):
        # type: () -> Text
        return self._subtype

    @property
    def obj_id(self):
        # type: () -> Text
        return self._obj_id

    @property
    def seq_id(self):
        # type: () -> int
        return int(self._seq_id)

    def as_tuple(self):
        # type: () -> Tuple[Text, Text, Text, int]
        return (self.obj_type, self.subtype, self.obj_id, self.seq_id)

    @classmethod
    def from_path(cls, path):
        # type: (Text) -> Optional[ProcessName]
        return cls.from_tuple(path.split("/"))

    @classmethod
    def from_tuple(cls, parts):
        # type: (List[Text]) -> Optional[ProcessName]
        if parts[0] not in [u"sync", u"try"]:
            return None
        if len(parts) != 4:
            return None
        return cls(*parts)

    @classmethod
    def with_seq_id(cls, repo, obj_type, subtype, obj_id):
        # type: (Repo, Text, Text, Text) -> ProcessName
        existing = ProcessNameIndex(repo).get(obj_type, subtype, obj_id)
        last_id = -1
        for process_name in existing:
            if (process_name.seq_id is not None and
                int(process_name.seq_id) > last_id):
                last_id = process_name.seq_id
        seq_id = last_id + 1
        return cls(obj_type, subtype, obj_id, six.ensure_text(str(seq_id)))


class VcsRefObject(six.with_metaclass(IdentityMap, object)):
    """Representation of a named reference to a git object associated with a
    specific process_name.

    This is typically either a tag or a head (i.e. branch), but can be any
    git object."""

    ref_prefix = None  # type: Text

    def __init__(self, repo, name, commit_cls=sync_commit.Commit):
        # type: (Repo, Union[ProcessName, SyncPointName], type) -> None
        self.repo = repo
        self.pygit2_repo = pygit2_get(repo)

        if not self.get_path(name) in self.pygit2_repo.references:
            raise ValueError("No ref found in %s with path %s" %
                             (repo.working_dir, self.get_path(name)))
        self.name = name
        self.commit_cls = commit_cls
        self._lock = None

    def as_mut(self, lock):
        # type: (SyncLock) -> MutGuard
        return MutGuard(lock, self)

    @property
    def lock_key(self):
        # type: () -> Tuple[Text, Text]
        return (self.name.subtype, self.name.obj_id)

    @classmethod
    def _cache_key(cls,
                   repo,  # type: Repo
                   process_name,  # type: Union[ProcessName, SyncPointName]
                   commit_cls=sync_commit.Commit,  # type: type
                   ):
        # type: (...) -> Tuple[Repo, Union[ProcessNameKey, Tuple[Text, Text]]]
        return (repo, process_name.key())

    def _cache_verify(self, repo, process_name, commit_cls=sync_commit.Commit):
        # type: (Repo, Union[ProcessName, SyncPointName], type) -> bool
        return commit_cls == self.commit_cls

    @classmethod
    @constructor(lambda args: (args["name"].subtype,
                               args["name"].obj_id))
    def create(cls, lock, repo, name, obj, commit_cls=sync_commit.Commit):
        # type: (SyncLock, Repo, ProcessName, Text, type) -> VcsRefObject
        path = cls.get_path(name)
        logger.debug("Creating ref %s" % path)
        pygit2_repo = pygit2_get(repo)
        if path in pygit2_repo.references:
            raise ValueError("Ref %s exists" % (path,))
        pygit2_repo.references.create(path, pygit2_repo.revparse_single(obj).id)
        return cls(repo, name, commit_cls)

    def __str__(self):
        # type: () -> str
        return six.ensure_str(self.path)

    def delete(self):
        # type: () -> None
        self.pygit2_repo.references[self.path].delete()

    @classmethod
    def get_path(cls, name):
        # type: (Union[ProcessName, SyncPointName]) -> Text
        return u"refs/%s/%s" % (cls.ref_prefix, name.path())

    @property
    def path(self):
        # type: () -> Text
        return self.get_path(self.name)

    @property
    def ref(self):
        # type: () -> Reference
        if self.path in self.pygit2_repo.references:
            return git.Reference(self.repo, self.path)
        return None

    @property
    def commit(self):
        # type: () -> Optional[Commit]
        ref = self.ref
        if ref is not None:
            commit = self.commit_cls(self.repo, ref.commit)
            return commit
        return None

    @commit.setter  # type: ignore
    @mut()
    def commit(self, commit):
        # type: (Union[Commit, Text]) -> None
        if isinstance(commit, sync_commit.Commit):
            sha1 = commit.sha1
        else:
            sha1 = commit
        sha1 = self.pygit2_repo.revparse_single(sha1).id
        self.pygit2_repo.references[self.path].set_target(sha1)


class BranchRefObject(VcsRefObject):
    ref_prefix = "heads"


class CommitBuilder(object):
    def __init__(self,
                 repo,  # type: Repo
                 message,  # type: Text
                 ref=None,  # type: Optional[Text]
                 commit_cls=sync_commit.Commit,  # type: type
                 initial_empty=False  # type: bool
                 ):
        # type: (...) -> None
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
        self.parents = None  # type: Optional[List[Text]]
        self.commit = None
        self.index = None  # type: pygit2.Index
        self.has_changes = False

    def __enter__(self):
        # type: () -> CommitBuilder
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

    def __exit__(self, *args, **kwargs):
        # type: (*Any, **Any) -> None
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

    def add_tree(self, tree):
        # type: (Dict[Text, bytes]) -> None
        self.has_changes = True
        for path, data in iteritems(tree):
            blob = self.pygit2_repo.create_blob(data)
            index_entry = pygit2.IndexEntry(path, blob, pygit2.GIT_FILEMODE_BLOB)
            self.index.add(index_entry)

    def delete(self, delete):
        # type: (List[Text]) -> None
        self.has_changes = True
        if delete:
            for path in delete:
                self.index.remove(path)

    def get(self):
        # type: () -> Optional[Any]
        return self.commit


class ProcessData(six.with_metaclass(IdentityMap, object)):
    obj_type = None  # type: Text

    def __init__(self, repo, process_name):
        # type: (Repo, ProcessName) -> None
        assert process_name.obj_type == self.obj_type
        self.repo = repo
        self.pygit2_repo = pygit2_get(repo)
        self.process_name = process_name
        self.ref = git.Reference(repo, env.config["sync"]["ref"])
        self.path = self.get_path(process_name)
        self._data = self._load()
        self._lock = None
        self._updated = set()  # type: Set[Text]
        self._deleted = set()  # type: Set[Text]
        self._delete = False

    def __repr__(self):
        # type: () -> str
        return six.ensure_str("<%s %s>" % (self.__class__.__name__, self.process_name))

    def __hash__(self):
        # type: () -> int
        return hash(self.process_name)

    def __eq__(self, other):
        # type: (Any) -> bool
        if type(self) != type(other):
            return False
        return self.repo == other.repo and self.process_name == other.process_name

    def as_mut(self, lock):
        # type: (SyncLock) -> MutGuard
        return MutGuard(lock, self)

    def exit_mut(self):
        # type: () -> None
        message = u"Update %s\n\n" % self.path
        with CommitBuilder(self.repo, message=message, ref=self.ref.path) as commit:
            from . import index
            if self._delete:
                self._delete_data("Delete %s" % self.path)
                self._delete = False

            elif self._updated or self._deleted:
                message_parts = []
                if self._updated:
                    message_parts.append("Updated: %s\n" % (", ".join(self._updated),))
                if self._deleted:
                    message_parts.append("Deleted: %s\n" % (", ".join(self._deleted),))
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
               lock,  # type: SyncLock
               repo,  # type: Repo
               process_name,  # type: ProcessName
               data,  # type: Dict[Text, Any]
               message=u"Sync data",  # type: Text
               ):
        # type: (...) -> ProcessData
        assert process_name.obj_type == cls.obj_type
        path = cls.get_path(process_name)
        ref = git.Reference(repo, env.config["sync"]["ref"])
        try:
            ref.commit.tree[path]
        except KeyError:
            pass
        else:
            raise ValueError("%s already exists at path %s" % (cls.__name__, path))
        with CommitBuilder(repo, message, ref=ref.path) as commit:
            commit.add_tree({path: json.dumps(data).encode("utf8")})
        ProcessNameIndex(repo).insert(process_name)
        return cls(repo, process_name)

    @classmethod
    def _cache_key(cls, repo, process_name):
        # type: (Repo, ProcessName) -> Tuple[Repo, ProcessNameKey]
        return (repo, process_name.key())

    @classmethod
    def get_path(self, process_name):
        # type: (ProcessName) -> Text
        return process_name.path()

    @classmethod
    def load_by_obj(cls,
                    repo,  # type: Repo
                    subtype,  # type: Text
                    obj_id,  # type: int
                    seq_id=None  # Type: Optional[int]
                    ):
        # type: (...) -> Set[ProcessData]
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

    def _save(self, data, message, commit_builder=None):
        # type: (Dict[Text, Any], Text, CommitBuilder) -> Optional[Any]
        if commit_builder is None:
            commit_builder = CommitBuilder(self.repo, message=message, ref=self.ref.path)
        else:
            commit_builder.message += message
        tree = {self.path: json.dumps(data).encode("utf8")}
        with commit_builder as commit:
            commit.add_tree(tree)
        return commit.get()

    def _delete_data(self, message, commit_builder=None):
        # type: (Text, Optional[CommitBuilder]) -> None
        if commit_builder is None:
            commit_builder = CommitBuilder(self.repo, message=message, ref=self.ref.path)
        with commit_builder as commit:
            commit.delete([self.path])

    def _load(self):
        # type: () -> Dict[Text, Any]
        ref = self.pygit2_repo.references[self.ref.path]
        repo = self.pygit2_repo
        try:
            data = repo[repo[ref.peel().tree.id][self.path].id].data
        except KeyError:
            return {}
        return json.loads(data)

    @property
    def lock_key(self):
        # type: () -> Tuple[Text, Text]
        return (self.process_name.subtype, self.process_name.obj_id)

    def __getitem__(self, key):
        # type: (Text) -> Any
        return self._data[key]

    def __contains__(self, key):
        # type: (Text) -> bool
        return key in self._data

    def get(self, key, default=None):
        # type: (Text, Any) -> Any
        return self._data.get(key, default)

    def items(self):
        # type: () -> Iterator[Tuple[Text, Any]]
        for key, value in iteritems(self._data):
            yield key, value

    @mut()
    def __setitem__(self, key, value):
        # type: (Text, Any) -> None
        if key not in self._data or self._data[key] != value:
            self._data[key] = value
            self._updated.add(key)

    @mut()
    def __delitem__(self, key):
        # type: (Text) -> None
        if key in self._data:
            del self._data[key]
            self._deleted.add(key)

    @mut()
    def delete(self):
        # type: () -> None
        self._delete = True


class FrozenDict(Mapping):
    def __init__(self, **kwargs):
        # type: (**Any) -> None
        self._data = {}
        for key, value in iteritems(kwargs):
            self._data[six.ensure_text(key)] = value

    def __getitem__(self, key):
        # type: (Text) -> Any
        return self._data[key]

    def __contains__(self, key):
        # type: (Any) -> bool
        return key in self._data

    def copy(self, **kwargs):
        # type: (**Any) -> FrozenDict
        new_data = self._data.copy()
        for key, value in iteritems(kwargs):
            new_data[six.ensure_text(key)] = value
        return self.__class__(**new_data)

    def __iter__(self):
        #  type: () -> Iterator[Text]
        for item in self._data:
            yield item

    def __len__(self):
        #  type: () -> int
        return len(self._data)

    def as_dict(self):
        # type: () -> Dict[Text, Any]
        return self._data.copy()


class entry_point(object):
    def __init__(self, task):
        self.task = task

    def __call__(self, f):
        def inner(*args, **kwargs):
            # type: (*Any, **Any) -> Optional[LandingSync]
            logger.info("Called entry point %s.%s" % (f.__module__, f.__name__))
            logger.debug("Called args %r kwargs %r" % (args, kwargs))

            if self.task in env.config["sync"]["enabled"]:
                return f(*args, **kwargs)

            logger.debug("Skipping disabled task %s" % self.task)
            return None

        inner.__name__ = f.__name__
        inner.__doc__ = f.__doc__
        return inner
