import json
import weakref
from collections import defaultdict

import git
import pygit2

import log
import commit as sync_commit
from env import Environment
from lock import MutGuard, RepoLock, mut, constructor
from repos import pygit2_get

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

    _cache = weakref.WeakValueDictionary()

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


def iter_process_names(pygit2_repo, kind=["sync", "try"]):
    """Iterator over all ProcessName objects"""
    ref = pygit2_repo.references[env.config["sync"]["ref"]]
    root = pygit2_repo[ref.peel().tree.id]
    stack = []
    for root_path in kind:
        try:
            tree_entry = root[root_path]
        except KeyError:
            continue
        tree = pygit2_repo[tree_entry.id]

        stack.append((root_path, tree))

    while stack:
        path, tree = stack.pop()
        for item in tree:
            item_path = "%s/%s" % (path, item.name)
            if item.type == "tree":
                stack.append((item_path, pygit2_repo[item.id]))
            else:
                process_name = ProcessName.from_path(item_path)
                if process_name is not None:
                    yield process_name


class ProcessNameIndex(object):
    __metaclass__ = IdentityMap

    def __init__(self, repo):
        self.repo = repo
        self.pygit2_repo = pygit2_get(repo)
        self.reset()

    @classmethod
    def _cache_key(cls, repo):
        return (repo,)

    def reset(self):
        self._all = set()
        self._data = defaultdict(
            lambda: defaultdict(
                lambda: defaultdict(set)))
        self._built = False

    def build(self):
        for process_name in iter_process_names(self.pygit2_repo):
            self.insert(process_name)
        self._built = True

    def insert(self, process_name):
        self._all.add(process_name)

        self._data[
            process_name.obj_type][
                process_name.subtype][
                    process_name.obj_id].add(process_name)

    def has(self, process_name):
        if not self._built:
            self.build()
        return process_name in self._all

    def get(self, obj_type, subtype=None, obj_id=None):
        if not self._built:
            self.build()

        target = self._data
        for key in [obj_type, subtype, obj_id]:
            if key is None:
                break
            target = target[key]

        rv = set()
        stack = [target]

        while stack:
            item = stack.pop()
            if isinstance(item, set):
                rv |= item
            else:
                stack.extend(item.itervalues())

        return rv


class ProcessName(object):
    """Representation of a name that is used to identify a sync operation.
    This has the general form <obj type>/<subtype>/<obj_id>[/<seq_id>].

    Here <obj type> represents the type of process e.g upstream or downstream,
    <obj_id> is an identifier for the sync,
    typically either a bug number or PR number, and <seq_id> is an optional id
    to cover cases where we might have multiple processes with the same obj_id.

    """
    __metaclass__ = IdentityMap

    def __init__(self, obj_type, subtype, obj_id, seq_id):
        assert obj_type is not None
        assert subtype is not None
        assert obj_id is not None
        assert seq_id is not None

        self._obj_type = obj_type
        self._subtype = subtype
        self._obj_id = str(obj_id)
        self._seq_id = str(seq_id)

    @classmethod
    def _cache_key(cls, obj_type, subtype, obj_id, seq_id=None):
        return (obj_type, subtype, str(obj_id), str(seq_id) if seq_id is not None else None)

    def __str__(self):
        return "%s/%s/%s/%s" % self.as_tuple()

    def key(self):
        return self._cache_key(self._obj_type, self._subtype, self._obj_id, self._seq_id)

    def __eq__(self, other):
        if self is other:
            return True
        if self.__class__ != other.__class__:
            return False
        return self.as_tuple() == other.as_tuple()

    def __hash__(self):
        return hash(self.key())

    @property
    def obj_type(self):
        return self._obj_type

    @property
    def subtype(self):
        return self._subtype

    @property
    def obj_id(self):
        return self._obj_id

    @property
    def seq_id(self):
        return int(self._seq_id)

    def as_tuple(self):
        return (self.obj_type, self.subtype, self.obj_id, self.seq_id)

    @classmethod
    def from_path(cls, path):
        return cls.from_tuple(path.split("/"))

    @classmethod
    def from_tuple(cls, parts):
        if parts[0] not in ["sync", "try"]:
            return None
        if len(parts) != 4:
            return None
        return cls(*parts)

    @classmethod
    def with_seq_id(cls, repo, obj_type, subtype, obj_id):
        existing = ProcessNameIndex(repo).get(obj_type, subtype, obj_id)
        last_id = -1
        for process_name in existing:
            if (process_name.seq_id is not None and
                int(process_name.seq_id) > last_id):
                last_id = int(process_name.seq_id)
        seq_id = last_id + 1
        return cls(obj_type, subtype, obj_id, seq_id)


class VcsRefObject(object):
    """Representation of a named reference to a git object associated with a
    specific process_name.

    This is typically either a tag or a head (i.e. branch), but can be any
    git object."""
    __metaclass__ = IdentityMap

    ref_prefix = None

    def __init__(self, repo, name, commit_cls=sync_commit.Commit):
        self.repo = repo
        self.pygit2_repo = pygit2_get(repo)

        if not self.get_path(name) in self.pygit2_repo.references:
            raise ValueError("No ref found in %s with path %s" %
                             (repo.working_dir, self.get_path(name)))
        self.name = name
        self.commit_cls = commit_cls
        self._lock = None

    def as_mut(self, lock):
        return MutGuard(lock, self)

    @property
    def lock_key(self):
        return (self.name.subtype, self.name.obj_id)

    @classmethod
    def _cache_key(cls, repo, process_name, commit_cls=sync_commit.Commit):
        return (repo, process_name.key())

    def _cache_verify(self, repo, process_name, commit_cls=sync_commit.Commit):
        return commit_cls == self.commit_cls

    @classmethod
    @constructor(lambda args: (args["name"].subtype,
                               args["name"].obj_id))
    def create(cls, lock, repo, name, obj, commit_cls=sync_commit.Commit):
        path = cls.get_path(name)
        logger.debug("Creating ref %s" % path)
        pygit2_repo = pygit2_get(repo)
        if path in pygit2_repo.references:
            raise ValueError("Ref %s exists" % (path,))
        pygit2_repo.references.create(path, pygit2_repo.revparse_single(obj).id)
        return cls(repo, name, commit_cls)

    def __str__(self):
        return self.path

    def delete(self):
        self.pygit2_repo.references[self.path].delete()

    @classmethod
    def get_path(cls, name):
        return "refs/%s/%s" % (cls.ref_prefix, str(name))

    @property
    def path(self):
        return self.get_path(self.name)

    @property
    def ref(self):
        if self.path in self.pygit2_repo.references:
            return git.Reference(self.repo, self.path)
        return None

    @property
    def commit(self):
        ref = self.ref
        if ref is not None:
            commit = self.commit_cls(self.repo, ref.commit)
            return commit

    @commit.setter
    @mut()
    def commit(self, commit):
        if isinstance(commit, sync_commit.Commit):
            sha1 = commit.sha1
        else:
            sha1 = commit
        sha1 = self.pygit2_repo.revparse_single(sha1).id
        self.pygit2_repo.references[self.path].set_target(sha1)


class BranchRefObject(VcsRefObject):
    ref_prefix = "heads"


class CommitBuilder(object):
    def __init__(self, repo, message, ref=None, commit_cls=sync_commit.Commit):
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
        if not ref:
            self.ref = None
        elif hasattr(ref, "path"):
            self.ref = ref.path
        else:
            self.ref = ref

        self._count = 0

        # State set for the life of the context manager
        self.lock = RepoLock(repo)
        self.parents = None
        self.commit = None
        self.index = None
        self.has_changes = False

    def __enter__(self):
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
                self.index.read_tree(ref.peel().tree)
        return self

    def __exit__(self, *args, **kwargs):
        self._count -= 1
        if self._count != 0:
            return

        if not self.has_changes:
            sha1 = self.parents[0]
        else:
            tree_id = self.index.write_tree(self.pygit2_repo)

            sha1 = self.pygit2_repo.create_commit(self.ref,
                                                  self.pygit2_repo.default_signature,
                                                  self.pygit2_repo.default_signature,
                                                  self.message,
                                                  tree_id,
                                                  self.parents)
        self.lock.__exit__(*args, **kwargs)
        self.commit = self.commit_cls(self.repo, sha1)

    def add_tree(self, tree):
        self.has_changes = True
        for path, data in tree.iteritems():
            blob = self.pygit2_repo.create_blob(data)
            index_entry = pygit2.IndexEntry(path, blob, pygit2.GIT_FILEMODE_BLOB)
            self.index.add(index_entry)

    def delete(self, delete):
        self.has_changes = True
        if delete:
            for path in delete:
                self.index.remove(path)

    def get(self):
        return self.commit


class ProcessData(object):
    __metaclass__ = IdentityMap
    obj_type = None

    def __init__(self, repo, process_name):
        assert process_name.obj_type == self.obj_type
        self.repo = repo
        self.pygit2_repo = pygit2_get(repo)
        self.process_name = process_name
        self.ref = git.Reference(repo, env.config["sync"]["ref"])
        self.path = self.get_path(process_name)
        self._data = self._load()
        self._lock = None
        self._updated = set()
        self._deleted = set()
        self._delete = False

    def __repr__(self):
        return "<%s %s>" % (self.__class__.__name__, self.process_name)

    def as_mut(self, lock):
        return MutGuard(lock, self)

    def exit_mut(self):
        message = "Update %s\n\n" % self.path
        with CommitBuilder(self.repo, message=message, ref=self.ref.path) as commit:
            import index
            if self._delete:
                self._delete_data("  Delete %s" % self.path)
                self._delete = False

            elif self._updated or self._deleted:
                message = []
                if self._updated:
                    message.append("  Updated: %s" % (", ".join(self._updated),))
                if self._deleted:
                    message.append("  Deleted: %s" % (", ".join(self._deleted),))
                self._save(self._data, message=" ".join(message), commit_builder=commit)

            self._updated = set()
            self._deleted = set()

            for idx_cls in index.indicies:
                idx = idx_cls(self.repo)
                idx.save(commit_builder=commit)

    @classmethod
    @constructor(lambda args: (args["process_name"].subtype,
                               args["process_name"].obj_id))
    def create(cls, lock, repo, process_name, data, message="Sync data"):
        assert process_name.obj_type == cls.obj_type
        path = cls.get_path(process_name)
        ref = git.Reference(repo, env.config["sync"]["ref"])
        try:
            ref.commit.tree[path]
        except KeyError:
            pass
        else:
            raise ValueError("%s already exists at path %s" % (cls.__name__, path))
        with CommitBuilder(repo, message, ref=ref) as commit:
            commit.add_tree({path: json.dumps(data)})
        ProcessNameIndex(repo).insert(process_name)
        return cls(repo, process_name)

    @classmethod
    def _cache_key(cls, repo, process_name):
        return (repo, process_name.key())

    @classmethod
    def get_path(self, process_name):
        return str(process_name)

    @classmethod
    def load_by_obj(cls, repo, subtype, obj_id):
        process_names = ProcessNameIndex(repo).get(cls.obj_type,
                                                   subtype,
                                                   obj_id)
        return {cls(repo, process_name) for process_name in process_names}

    @classmethod
    def load_by_status(cls, repo, subtype, status):
        import index
        process_names = index.SyncIndex(repo).get((cls.obj_type,
                                                   subtype,
                                                   status))
        rv = set()
        for process_name in process_names:
            rv.add(cls(repo, process_name))
        return rv

    def _save(self, data, message, commit_builder=None):
        if commit_builder is None:
            commit_builder = CommitBuilder(self.repo, message=message, ref=self.ref.path)
        else:
            commit_builder.message += message
        tree = {self.path: json.dumps(data)}
        with commit_builder as commit:
            commit.add_tree(tree)
        return commit.get()

    def _delete_data(self, message, commit_builder=None):
        if commit_builder is None:
            commit_builder = CommitBuilder(self.repo, message=message, ref=self.ref.path)
        with commit_builder as commit:
            commit.delete([self.path])

    def _load(self):
        ref = self.pygit2_repo.references[self.ref.path]
        repo = self.pygit2_repo
        try:
            data = repo[repo[ref.peel().tree.id][self.path].id].data
        except KeyError:
            return {}
        return json.loads(data)

    @property
    def lock_key(self):
        return (self.process_name.subtype, self.process_name.obj_id)

    def __getitem__(self, key):
        return self._data[key]

    def __contains__(self, key):
        return key in self._data

    def get(self, key, default=None):
        return self._data.get(key, default)

    def items(self):
        for key, value in self._data.iteritems():
            yield key, value

    @mut()
    def __setitem__(self, key, value):
        if key not in self._data or self._data[key] != value:
            self._data[key] = value
            self._updated.add(key)

    @mut()
    def __delitem__(self, key):
        if key in self._data:
            del self._data[key]
            self._deleted.add(key)

    @mut()
    def delete(self):
        self._delete = True


class entry_point(object):
    def __init__(self, task):
        self.task = task

    def __call__(self, f):
        def inner(*args, **kwargs):
            logger.info("Called entry point %s.%s" % (f.__module__, f.__name__))
            logger.debug("Called args %r kwargs %r" % (args, kwargs))

            if self.task in env.config["sync"]["enabled"]:
                return f(*args, **kwargs)
            else:
                logger.debug("Skipping disabled task %s" % self.task)

        inner.__name__ = f.__name__
        inner.__doc__ = f.__doc__
        return inner
