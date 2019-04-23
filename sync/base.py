import itertools
import json
import os
import re
import shutil
import subprocess
import traceback
import weakref
from collections import defaultdict

import enum
import git
from lock import MutGuard, RepoLock, mut, constructor

import bug
import log
import commit as sync_commit
from env import Environment

env = Environment()

invalid_re = re.compile(".*[-_]")

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


class ProcessIndex(object):
    __metaclass__ = IdentityMap

    def __init__(self, repo):
        self.repo = repo
        self.reset()

    @classmethod
    def _cache_key(cls, repo):
        return (repo,)

    def reset(self):
        self._all = set()
        self._by_obj_id = defaultdict(set)
        self._built = False

    def build(self):
        for ref in self.repo.references:
            process_name = ProcessName.from_ref(ref.path)
            if process_name is None:
                continue

            self.insert(process_name)
        self._built = True

    def insert(self, process_name):
        self._all.add(process_name)
        obj_id_key = (process_name.obj_type,
                      process_name.subtype,
                      process_name.obj_id)

        self._by_obj_id[obj_id_key].add(process_name)

    def has(self, process_name):
        if not self._built:
            self.build()
        return process_name in self._all

    def get_by_type(self, obj_type):
        if not self._built:
            self.build()
        rv = set()
        for key, values in self._by_type.iteritems():
            if key[0] == obj_type:
                rv |= values
        return rv

    def get_by_obj(self, obj_type, subtype, obj_id):
        if not self._built:
            self.build()
        return self._by_obj_id[(obj_type, subtype, str(obj_id))]


class ProcessName(object):
    """Representation of a refname, excluding the refs/<type>/ prefix
    that is used to identify a sync operation.  This has the general form
    <obj type>/<subtype>/<obj_id>[/<seq_id>]

    Here <obj type> represents the type of process e.g upstream or downstream,
    <obj_id> is an identifier for the sync,
    typically either a bug number or PR number, and <seq_id> is an optional id
    to cover cases where we might have multiple processes with the same obj_id.
    """
    __metaclass__ = IdentityMap

    def __init__(self, obj_type, subtype, obj_id, seq_id=None):
        assert obj_type is not None
        assert subtype is not None
        assert obj_id is not None

        self._obj_type = obj_type
        self._subtype = subtype
        self._obj_id = str(obj_id)
        self._seq_id = str(seq_id) if seq_id is not None else None
        self._lock = None

    @classmethod
    def _cache_key(cls, obj_type, subtype, obj_id, seq_id=None):
        return (obj_type, subtype, str(obj_id), str(seq_id) if seq_id is not None else None)

    def __str__(self):
        rv = "%s/%s/%s" % (self._obj_type,
                           self._subtype,
                           self._obj_id)
        if self._seq_id is not None:
            rv = "%s/%s" % (rv, self._seq_id)
        return rv

    def key(self):
        return self._cache_key(self._obj_type, self._subtype, self._obj_id, self._seq_id)

    def __eq_(self, other):
        if self is other:
            return True
        if self.__class__ != other.__class__:
            return False
        return ((self._obj_type == other._obj_type) and
                (self._subtype == other._subtype) and
                (self._obj_id == other._obj_id) and
                (self._seq_id == other._seq_id))

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
        return int(self._seq_id) if self._seq_id is not None else 0

    @classmethod
    def from_ref(cls, ref):
        parts = ref.split("/")
        if parts[0] == "refs":
            parts = parts[2:]
        if parts[0] not in ["sync", "try"]:
            return None
        if len(parts) not in (3, 4):
            return None
        return cls(*parts)

    @classmethod
    def with_seq_id(cls, repo, obj_type, subtype, obj_id):
        existing = ProcessIndex(repo).get_by_obj(obj_type, subtype, obj_id)
        existing_no_id = any(item.seq_id is None for item in existing)
        last_id = 0 if existing_no_id else -1
        for process_name in existing:
            if (process_name.seq_id is not None and
                int(process_name.seq_id) > last_id):
                last_id = int(process_name.seq_id)
        seq_id = last_id + 1
        return cls(obj_type, subtype, obj_id, seq_id)


class SyncPointName(object):
    """Like a process name but for pointers that aren't associated with a
    specific sync object, but with a general process e.g. the last update point
    for an upstream sync."""
    __metaclass__ = IdentityMap

    def __init__(self, subtype, obj_id):
        self._obj_type = "sync"
        self._subtype = subtype
        self._obj_id = str(obj_id)
        self._refs = []

        self._lock = None

    @property
    def obj_type(self):
        return self._obj_type

    @property
    def subtype(self):
        return self._subtype

    @property
    def obj_id(self):
        return self._obj_id

    @classmethod
    def _cache_key(cls, subtype, obj_id):
        return (subtype, str(obj_id))

    def key(self):
        return (self._subtype, self._obj_id)

    def __str__(self):
        return "%s/%s/%s" % (self._obj_type,
                             self._subtype,
                             self._obj_id)


class VcsRefObject(object):
    """Representation of a named reference to a git object associated with a
    specific process_name.

    This is typically either a tag or a head (i.e. branch), but can be any
    git object."""
    __metaclass__ = IdentityMap

    ref_prefix = None

    def __init__(self, repo, process_name, commit_cls=sync_commit.Commit):
        if not git.Reference(repo, self.process_path(process_name)).is_valid():
            raise ValueError("No ref found in %s with path %s" %
                             (repo.working_dir, self.process_path(process_name)))
        self.repo = repo
        self._ref = str(process_name)
        self.process_name = process_name
        self.commit_cls = commit_cls
        self._lock = None

    def as_mut(self, lock):
        return MutGuard(lock, self)

    @property
    def lock_key(self):
        return (self.process_name.subtype, self.process_name.obj_id)

    @classmethod
    def _cache_key(cls, repo, process_name, commit_cls=sync_commit.Commit):
        return (repo, process_name.key())

    def _cache_verify(self, repo, process_name, commit_cls=sync_commit.Commit):
        return commit_cls == self.commit_cls

    @classmethod
    @constructor(lambda args: (args["process_name"].subtype,
                               args["process_name"].obj_id))
    def create(cls, lock, repo, process_name, obj, commit_cls=sync_commit.Commit):
        path = cls.process_path(process_name)
        if git.Reference(repo, cls.process_path(process_name)).is_valid():
            raise ValueError("Ref %s already exists" % path)

        return cls._create(lock, repo, process_name, obj, commit_cls)

    def __str__(self):
        return self._ref

    def delete(self):
        self.repo.git.update_ref("-d", self.path)

    @classmethod
    def process_path(cls, process_name):
        return "refs/%s/%s" % (cls.ref_prefix, str(process_name))

    @property
    def path(self):
        return "refs/%s/%s" % (self.ref_prefix, self._ref)

    @property
    def ref(self):
        ref = git.Reference(self.repo, self.path)
        if ref.is_valid():
            return ref
        return None

    @classmethod
    @constructor(lambda args: (args["process_name"].subtype,
                               args["process_name"].obj_id))
    def _create(cls, lock, repo, process_name, obj, commit_cls=sync_commit.Commit):
        path = cls.process_path(process_name)
        logger.debug("Creating ref %s" % path)
        ref = git.Reference(repo, path)
        if ref.is_valid():
            raise ValueError("Ref %s exists" % (ref.path,))
        ref.set_object(obj)
        ProcessIndex(repo).insert(process_name)
        return cls(repo, process_name, commit_cls)

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
            commit = commit.commit
        ref = git.Reference(self.repo, self.path)
        ref.set_object(commit)


class BranchRefObject(VcsRefObject):
    ref_prefix = "heads"


class DataRefObject(VcsRefObject):
    ref_prefix = "syncs"


class CommitBuilder(object):
    def __init__(self, repo, message, initial=None, ref=None, commit_cls=sync_commit.Commit):
        self.repo = repo
        self.message = message
        self.commit_cls = commit_cls
        self.initial = initial
        self.ref = ref
        self.parents = None
        self.lock = RepoLock(repo)
        self._commit = None

    def __enter__(self):
        assert self._commit is None
        self.lock.__enter__()
        # First we create an empty index
        if self.ref is not None:
            ref = git.Reference(self.repo, self.ref)
            if ref.is_valid():
                self.parents = [ref.commit.hexsha]

        if self.initial is None:
            read_tree_args = ()
            read_tree_kwargs = {"empty": True}
        else:
            read_tree_args = (self.initial,)
            read_tree_kwargs = {}
        self.repo.git.read_tree(*read_tree_args, **read_tree_kwargs)
        return self

    def __exit__(self, *args, **kwargs):
        tree_id = self.repo.git.write_tree()
        args = ["-m", self.message]
        if self.parents is not None:
            for item in self.parents:
                args.extend(["-p", item])
        args.append(tree_id)
        sha1 = self.repo.git.commit_tree(*args)
        self.repo.git.read_tree(empty=True)
        if self.ref:
            ref = git.Reference(self.repo, self.ref)
            ref.set_object(sha1)
        self.lock.__exit__(*args, **kwargs)
        self._commit = self.commit_cls(self.repo, sha1)

    def add_tree(self, tree):
        for path, data in tree.iteritems():
            proc = self.repo.git.hash_object(w=True, path=path, stdin=True, as_process=True,
                                             istream=subprocess.PIPE)
            stdout, stderr = proc.communicate(data)
            if proc.returncode is not 0:
                raise git.GitCommandError(["git", "hash-object", "-w", "--path=%s" % path],
                                          proc.returncode, stderr, stdout)
            self.repo.git.update_index("100644", stdout, path, add=True, cacheinfo=True)

    def delete(self, delete):
        if delete:
            for path in delete:
                self.repo.git.rm(path, cached=True)

    def get(self):
        return self._commit


class ProcessData(object):
    __metaclass__ = IdentityMap
    path = "data"
    obj_type = None

    def __init__(self, repo, process_name):
        self.process_name = process_name
        self.repo = repo
        self.ref = DataRefObject(repo, process_name)
        self._data = self._load()
        self._lock = None
        self._updated = set()
        self._deleted = set()
        self._index_changes = []

    def __repr__(self):
        return "<%s %s>" % (self.__class__.__name__, self.process_name)

    def as_mut(self, lock):
        return MutGuard(lock, self, [self.ref])

    def exit_mut(self):
        import index
        if self._updated or self._deleted:
            message = []
            if self._updated:
                message.append("Updated: %s" % (", ".join(self._updated),))
            if self._deleted:
                message.append("Deleted: %s" % (", ".join(self._deleted),))
            self._save(self._data, message=" ".join(message))
            self._updated = set()
            self._deleted = set()
        for idx in index.indicies.values():
            idx.save()

    @classmethod
    @constructor(lambda args: (args["process_name"].subtype,
                               args["process_name"].obj_id))
    def create(cls, lock, repo, process_name, data, message="Sync data"):
        with CommitBuilder(repo, message) as commit:
            commit.add_tree({cls.path: json.dumps(data)})
        DataRefObject.create(lock, repo, process_name, commit.get().commit)
        return cls(repo, process_name)

    def _save(self, data, message):
        commit = self.create_commit(self.repo,
                                    self.path,
                                    self._data,
                                    ref=self.ref.path,
                                    message=message)
        self.ref.ref.set_commit(commit.sha1)

    def _load(self):
        return json.load(self.ref.ref.commit.tree[self.path].data_stream)

    @staticmethod
    def create_commit(repo, path, data, message, ref=None):
        tree = {path: json.dumps(data)}
        with CommitBuilder(repo, message=message, ref=ref) as commit:
            commit.add_tree(tree)
        return commit.get()

    @property
    def lock_key(self):
        return (self.process_name.subtype, self.process_name.obj_id)

    @classmethod
    def _cache_key(cls, repo, process_name):
        return (repo, process_name.key())

    @classmethod
    def load_by_obj(cls, repo, subtype, obj_id):
        process_names = ProcessIndex(repo).get_by_obj(cls.obj_type,
                                                      subtype,
                                                      obj_id)
        rv = set()
        for process_name in process_names:
            rv.add(cls(repo, process_name))
        return rv

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


class CommitFilter(object):
    """Filter of a range of commits"""
    def path_filter(self):
        """Path filter for the commit range,
        returning a list of paths that match or None to
        match all paths."""
        return None

    def filter_commit(self, commit):
        """Per-commit filter.

        :param commit: wpt_commit.Commit object
        :returns: A boolean indicating whether to include the commit"""
        return True

    def filter_commits(self, commits):
        """Filter that applies to the set of commits that were selected
        by the per-commit filter. Useful for e.g. removing backouts
        from a set of commits.

        :param commits: List of wpt_commit.Commit objects
        :returns: List of commits to return"""
        return commits


class CommitRange(object):
    """Range of commits in a specific repository.

    This is assumed to be between a tag and a branch sharing a single process_name
    i.e. the tag represents the base commit of the branch.
    TODO:  Maybe just store the base branch name in the tag rather than making it
           an actual pointer since that works better with rebases.
    """
    def __init__(self, repo, base, head_ref, commit_cls, commit_filter=None):
        self.repo = repo

        # This ended up a little confused because these used to both be
        # VcsRefObjects, but now the base is stored as a ref not associated
        # with a process_name. This should be refactored.
        self._base = commit_cls(repo, base)
        self._head_ref = head_ref
        self.commit_cls = commit_cls
        self.commit_filter = commit_filter

        # Cache for the commits in this range
        self._commits = None
        self._head_sha = None
        self._base_sha = None

        self._lock = None

    def as_mut(self, lock):
        return MutGuard(lock, self, [self._head_ref])

    @property
    def lock_key(self):
        return (self._head_ref.process_name.subtype,
                self._head_ref.process_name.obj_id)

    def __getitem__(self, index):
        return self.commits[index]

    def __iter__(self):
        for item in self.commits:
            yield item

    def __len__(self):
        return len(self.commits)

    def __contains__(self, other_commit):
        for commit in self:
            if commit == other_commit:
                return True
        return False

    @property
    def base(self):
        return self._base

    @property
    def head(self):
        return self._head_ref.commit

    @property
    def commits(self):
        if self._commits:
            if (self.head.sha1 == self._head_sha and
                self.base.sha1 == self._base_sha):
                return self._commits
        revish = "%s..%s" % (self.base.sha1, self.head.sha1)
        commits = []
        for git_commit in self.repo.iter_commits(revish,
                                                 reverse=True,
                                                 paths=self.commit_filter.path_filter()):
            commit = self.commit_cls(self.repo, git_commit)
            if not self.commit_filter.filter_commit(commit):
                continue
            commits.append(commit)
        commits = self.commit_filter.filter_commits(commits)
        self._commits = commits
        self._head_sha = self.head.sha1
        self._base_sha = self.base.sha1
        return self._commits

    @property
    def files_changed(self):
        # We avoid using diffs because that's harder to get right in the face of merges
        files = set()
        for commit in self.commits:
            commit_files = self.repo.git.show(commit.sha1, name_status=True, format="")
            for item in commit_files.splitlines():
                parts = item.split("\t")
                for part in parts[1:]:
                    files.add(part.strip())
        return files

    @base.setter
    @mut()
    def base(self, value):
        # Note that this doesn't actually update the stored value of the base
        # anywhere, unlike the head setter which will update the associated ref
        self._commits = None
        self._base_sha = value
        self._base = self.commit_cls(self.repo, value)

    @head.setter
    @mut()
    def head(self, value):
        self._head_ref.commit = value


class Worktree(object):
    """Wrapper for accessing a git worktree for a specific process.

    To access the worktree call .get()
    """

    def __init__(self, repo, process_name):
        self.repo = repo
        self._worktree = None
        self.process_name = process_name
        self.path = os.path.join(env.config["root"],
                                 env.config["paths"]["worktrees"],
                                 os.path.basename(repo.working_dir),
                                 process_name.subtype,
                                 process_name.obj_id)
        self._lock = None

    def as_mut(self, lock):
        return MutGuard(lock, self)

    @property
    def lock_key(self):
        return (self.process_name.subtype, self.process_name.obj_id)

    @mut()
    def get(self):
        """Return the worktree.

        On first access, the worktree is reset to the current HEAD. Subsequent
        access doesn't perform the same check, so it's possible to retain state
        within a specific process."""
        # TODO: We can get the worktree to only checkout the paths we actually
        # need.
        # To do this we have to
        # * Enable sparse checkouts by setting core.sparseCheckouts
        # * Add the worktree with --no-checkout
        # * Add the list of paths to check out under $REPO/worktrees/info/sparse-checkout
        # * Go to the worktree and check it out
        if self._worktree is None:
            if not os.path.exists(self.path):
                self.repo.git.worktree("prune")
                self.repo.git.worktree("add",
                                       os.path.abspath(self.path),
                                       str(self.process_name))
            self._worktree = git.Repo(self.path)
        # TODO: In general the worktree should be on the right branch, but it would
        # be good to check. In the specific case of landing, we move the wpt worktree
        # around various commits, so it isn't necessarily on the correct branch
        return self._worktree

    @mut()
    def delete(self):
        if os.path.exists(self.path):
            logger.info("Deleting worktree at %s" % self.path)
            try:
                shutil.rmtree(self.path)
            except Exception:
                logger.warning("Failed to remove worktree %s:%s" %
                               (self.path, traceback.format_exc()))
            else:
                logger.debug("Removed worktree %s" % (self.path,))
        self.repo.git.worktree("prune")


@enum.unique
class LandableStatus(enum.Enum):
    ready = 0
    no_pr = 1
    upstream = 2
    no_sync = 3
    error = 4
    missing_try_results = 5
    skip = 6

    def reason_str(self):
        return {LandableStatus.ready: "Ready",
                LandableStatus.no_pr: "No PR",
                LandableStatus.upstream: "From gecko",
                LandableStatus.no_sync: "No sync created",
                LandableStatus.error: "Error",
                LandableStatus.missing_try_results: "Incomplete try results",
                LandableStatus.skip: "Skip"}.get(self, "Unknown")


class SyncProcess(object):
    __metaclass__ = IdentityMap

    obj_type = "sync"
    sync_type = "*"
    obj_id = None  # Either "bug" or "pr"
    statuses = ()
    status_transitions = []
    multiple_syncs = False  # Can multiple syncs have the same obj_id

    def __init__(self, git_gecko, git_wpt, process_name):
        self._lock = None

        assert process_name.obj_type == self.obj_type
        assert process_name.subtype == self.sync_type

        self.git_gecko = git_gecko
        self.git_wpt = git_wpt

        self.process_name = process_name

        self.data = ProcessData(git_gecko, process_name)

        self.gecko_commits = CommitRange(git_gecko,
                                         self.data["gecko-base"],
                                         BranchRefObject(git_gecko,
                                                         self.process_name,
                                                         commit_cls=sync_commit.GeckoCommit),
                                         commit_cls=sync_commit.GeckoCommit,
                                         commit_filter=self.gecko_commit_filter())
        self.wpt_commits = CommitRange(git_wpt,
                                       self.data["wpt-base"],
                                       BranchRefObject(git_wpt,
                                                       self.process_name,
                                                       commit_cls=sync_commit.WptCommit),
                                       commit_cls=sync_commit.WptCommit,
                                       commit_filter=self.wpt_commit_filter())

        self.gecko_worktree = Worktree(git_gecko, process_name)

        self.wpt_worktree = Worktree(git_wpt, process_name)

        # Hold onto indexes for the lifetime of the SyncProcess object
        self._indexes = {ProcessIndex(git_gecko)}

    @classmethod
    def _cache_key(cls, git_gecko, git_wpt, process_name):
        return process_name.key()

    def as_mut(self, lock):
        return MutGuard(lock, self, [self.data,
                                     self.gecko_commits,
                                     self.wpt_commits,
                                     self.gecko_worktree,
                                     self.wpt_worktree])

    @property
    def lock_key(self):
        return (self.process_name.subtype, self.process_name.obj_id)

    def __repr__(self):
        return "<%s %s %s>" % (self.__class__.__name__,
                               self.sync_type,
                               self.process_name)

    @classmethod
    def for_pr(cls, git_gecko, git_wpt, pr_id):
        import index
        idx = index.PrIdIndex.get_or_create(git_gecko)
        process_name = idx.get((str(pr_id),))
        if process_name:
            return cls(git_gecko, git_wpt, process_name)

    @classmethod
    def for_bug(cls, git_gecko, git_wpt, bug, statuses=None, flat=False):
        """Get the syncs for a specific bug.

        :param bug: The bug number for which to find syncs.
        :param statuses: An optional list of sync statuses to include.
                         Defaults to all statuses.
        :param flat: Return a flat list of syncs instead of a dictionary.

        :returns: By default a dictionary of {status: [syncs]}, but if flat
                  is true, just returns a list of matching syncs.
        """
        import index
        statuses = set(statuses) if statuses is not None else set(cls.statuses)
        rv = defaultdict(set)
        idx_key = (bug,)
        if len(statuses) == 1:
            idx_key == (bug, list(statuses)[0])
        idx = index.BugIdIndex.get_or_create(git_gecko)

        process_names = idx.get(idx_key)
        for process_name in process_names:
            if process_name.subtype == cls.sync_type:
                sync = cls(git_gecko, git_wpt, process_name)
                if sync.status in statuses:
                    rv[sync.status].add(sync)
        if flat:
            rv = list(itertools.chain.from_iterable(rv.itervalues()))
        return rv

    @classmethod
    def load_by_obj(cls, git_gecko, git_wpt, obj_id):
        process_names = ProcessIndex(git_gecko).get_by_obj(cls.obj_type,
                                                           cls.sync_type,
                                                           obj_id)
        rv = set()
        for process_name in process_names:
            rv.add(cls(git_gecko, git_wpt, process_name))
        return rv

    @classmethod
    def load_by_status(cls, git_gecko, git_wpt, status):
        import index
        idx = index.SyncIndex.get_or_create(git_gecko)
        key = (cls.obj_type, cls.sync_type, status)
        process_names = idx.get(key)
        rv = set()
        for process_name in process_names:
            rv.add(cls(git_gecko, git_wpt, process_name))
        return rv

    # End of getter methods

    @classmethod
    def prev_gecko_commit(cls, git_gecko, repository_name, base_rev=None, default=None):
        """Get the last gecko commit processed by a sync process.

        :param str repository_name: The name of the gecko branch being processed
        :param base_rev: The SHA1 for a commit to use as the previous commit, overriding
                         the stored value
        :returns: Tuple of (LastSyncPoint, GeckoCommit) for the previous gecko commit. In
                  case the base_rev override is passed in the LastSyncPoint may be pointing
                  at a different commit to the returned gecko commit"""

        last_sync_point = cls.last_sync_point(git_gecko, repository_name,
                                              default=default)

        logger.info("Last sync point was %s" % last_sync_point.commit.sha1)

        if base_rev is None:
            prev_commit = last_sync_point.commit
        else:
            prev_commit = sync_commit.GeckoCommit(git_gecko,
                                                  git_gecko.cinnabar.hg2git(base_rev))
        return last_sync_point, prev_commit

    @classmethod
    def last_sync_point(cls, git_gecko, repository_name, default=None):
        assert "/" not in repository_name
        name = SyncPointName(cls.sync_type,
                             repository_name)

        return BranchRefObject(git_gecko,
                               name,
                               commit_cls=sync_commit.GeckoCommit)

    @property
    def landable_status(self):
        return None

    def _output_data(self):
        rv = ["%s%s" % ("*" if self.error else " ",
                        str(self.process_name)),
              "gecko range: %s..%s" % (self.gecko_commits.base.sha1,
                                       self.gecko_commits.head.sha1),
              "wpt range: %s..%s" % (self.wpt_commits.base.sha1,
                                     self.wpt_commits.head.sha1)]
        if self.error:
            rv.extend(["ERROR:",
                       self.error["message"],
                       self.error["stack"]])
        landable_status = self.landable_status
        if landable_status:
            rv.append("Landable status: %s" % landable_status.reason_str())

        for key, value in sorted(self.data.items()):
            if key != "error":
                rv.append("%s: %s" % (key, value))
        return rv

    def output(self):
        return "\n".join(self._output_data())

    def __eq__(self, other):
        if not hasattr(other, "process_name"):
            return False
        for attr in ["obj_type", "subtype", "obj_id", "seq_id"]:
            if getattr(self.process_name, attr) != getattr(other.process_name, attr):
                return False
        return True

    def __ne__(self, other):
        return not self == other

    def set_wpt_base(self, ref):
        # This is kind of an appaling hack
        try:
            self.git_wpt.commit(ref)
        except Exception:
            raise ValueError
        self.data["wpt-base"] = ref
        self.wpt_commits._base = sync_commit.WptCommit(self.git_wpt, ref)

    @staticmethod
    def gecko_integration_branch():
        return env.config["gecko"]["refs"]["mozilla-inbound"]

    @staticmethod
    def gecko_landing_branch():
        return env.config["gecko"]["refs"]["central"]

    def gecko_commit_filter(self):
        return CommitFilter()

    def wpt_commit_filter(self):
        return CommitFilter()

    @property
    def branch_name(self):
        return str(self.process_name)

    @property
    def status(self):
        return self.data.get("status")

    @status.setter
    @mut()
    def status(self, value):
        if value not in self.statuses:
            raise ValueError("Unrecognised status %s" % value)
        current = self.status
        if current == value:
            return
        if (current, value) not in self.status_transitions:
            raise ValueError("Tried to change status from %s to %s" % (current, value))

        import index
        index.SyncIndex(self.git_gecko).delete(index.SyncIndex.make_key(self),
                                               self.process_name)
        index.BugIdIndex(self.git_gecko).delete(index.BugIdIndex.make_key(self),
                                                self.process_name)
        self.data["status"] = value
        index.SyncIndex(self.git_gecko).insert(index.SyncIndex.make_key(self),
                                               self.process_name)
        index.BugIdIndex(self.git_gecko).insert(index.BugIdIndex.make_key(self),
                                                self.process_name)

    @property
    def bug(self):
        if self.obj_id == "bug":
            return self.process_name.obj_id
        else:
            return self.data.get("bug")

    @bug.setter
    @mut()
    def bug(self, value):
        import index
        if self.obj_id == "bug":
            raise AttributeError("Can't set attribute")
        old_key = None
        if self.data.get("bug"):
            old_key = index.BugIdIndex.make_key(self)
        self.data["bug"] = value
        new_key = index.BugIdIndex.make_key(self)
        index.BugIdIndex(self.git_gecko).move(old_key, new_key, self.process_name)

    @property
    def pr(self):
        if self.obj_id == "pr":
            return self.process_name.obj_id
        else:
            return self.data.get("pr")

    @pr.setter
    @mut()
    def pr(self, value):
        import index
        if self.obj_id == "pr":
            raise AttributeError("Can't set attribute")
        old_key = None
        if self.data.get("pr"):
            old_key = index.PrIdIndex.make_key(self)
        self.data["pr"] = value
        new_key = index.PrIdIndex.make_key(self)
        index.PrIdIndex(self.git_gecko).move(old_key,
                                             new_key,
                                             self.process_name)

    @property
    def seq_id(self):
        return self.process_name.seq_id

    @property
    def last_pr_check(self):
        return self.data.get("last-pr-check", {})

    @last_pr_check.setter
    @mut()
    def last_pr_check(self, value):
        if value is not None:
            self.data["last-pr-check"] = value
        else:
            del self.data["last-pr-check"]

    @property
    def error(self):
        return self.data.get("error")

    @error.setter
    @mut()
    def error(self, value):
        def encode(item):
            if item is None:
                return item
            if isinstance(item, str):
                return item
            if isinstance(item, unicode):
                return item.encode("utf8", "replace")
            return repr(item)

        if value is not None:
            if isinstance(value, (str, unicode)):
                message = value
                stack = None
            else:
                message = value.message
                stack = traceback.format_exc()
            error = {
                "message": encode(message),
                "stack": encode(stack)
            }
            self.data["error"] = error
            self.set_bug_data("error")
        else:
            del self.data["error"]
            self.set_bug_data(None)

    def try_pushes(self, status=None):
        import trypush
        try_pushes = trypush.TryPush.load_by_obj(self.git_gecko,
                                                 self.sync_type,
                                                 self.process_name.obj_id)
        if status is not None:
            try_pushes = {item for item in try_pushes if item.status == status}
        return list(sorted(try_pushes, key=lambda x: x.process_name.seq_id))

    def latest_busted_try_pushes(self):
        try_pushes = self.try_pushes(status="complete")
        busted = []
        for push in reversed(try_pushes):
            if push.infra_fail and not push.expired():
                busted.append(push)
            else:
                break
        return busted

    @property
    def latest_try_push(self):
        try_pushes = self.try_pushes()
        if try_pushes:
            try_pushes = sorted(try_pushes, key=lambda x: x.ref.process_name.seq_id)
            return try_pushes[-1]

    def wpt_renames(self):
        renames = {}
        diff_blobs = self.wpt_commits.head.commit.diff(
            self.git_wpt.merge_base(self.data["wpt-base"], self.wpt_commits.head.sha1))
        for item in diff_blobs:
            if item.rename_from:
                renames[item.rename_from] = item.rename_to
        return renames

    @classmethod
    @constructor(lambda args: (args['cls'].sync_type,
                               args['bug']
                               if args['cls'].obj_id == "bug"
                               else str(args['pr'])))
    def new(cls, lock, git_gecko, git_wpt, gecko_base, gecko_head,
            wpt_base="origin/master", wpt_head=None,
            bug=None, pr=None, status="open"):
        import index
        if cls.obj_id == "bug":
            assert bug is not None
            obj_id = bug
        elif cls.obj_id == "pr":
            assert pr is not None
            obj_id = pr
        else:
            raise ValueError("Invalid cls.obj_id: %s" % cls.obj_id)

        if wpt_head is None:
            wpt_head = wpt_base

        data = {"gecko-base": gecko_base,
                "wpt-base": wpt_base,
                "pr": pr,
                "bug": bug,
                "status": status}

        # This is pretty ugly
        process_name = ProcessName.with_seq_id(git_gecko,
                                               cls.obj_type,
                                               cls.sync_type,
                                               obj_id)
        if not cls.multiple_syncs and process_name.seq_id != 0:
            raise ValueError("Tried to create new %s sync for %s %s but one already exists" % (
                cls.obj_id, cls.sync_type, obj_id))
        ProcessData.create(lock, git_gecko, process_name, data)
        BranchRefObject.create(lock, git_gecko, process_name, gecko_head, sync_commit.GeckoCommit)
        BranchRefObject.create(lock, git_wpt, process_name, wpt_head, sync_commit.WptCommit)

        rv = cls(git_gecko, git_wpt, process_name)

        idx = index.SyncIndex.get_or_create(git_gecko)
        idx.insert(idx.make_key(rv), process_name).save()

        if cls.obj_id == "bug":
            bug_idx = index.BugIdIndex.get_or_create(git_gecko)
            bug_idx.insert(bug_idx.make_key(rv), process_name).save()
        elif cls.obj_id == "pr":
            pr_idx = index.PrIdIndex.get_or_create(git_gecko)
            pr_idx.insert(pr_idx.make_key(rv), process_name).save()
        return rv

    @mut()
    def finish(self, status="complete"):
        # TODO: cancel related try pushes &c.
        logger.info("Marking sync %s as %s" % (self.process_name, status))
        self.status = status
        for worktree in [self.gecko_worktree, self.wpt_worktree]:
            worktree.delete()
        for repo in [self.git_gecko, self.git_wpt]:
            repo.git.worktree("prune")

    @mut()
    def gecko_rebase(self, new_base):
        new_base = sync_commit.GeckoCommit(self.git_gecko, new_base)
        git_worktree = self.gecko_worktree.get()
        git_worktree.git.rebase(new_base.sha1)
        self.gecko_commits.base = new_base

    @mut()
    def wpt_rebase(self, ref):
        assert ref in self.git_wpt.refs
        git_worktree = self.wpt_worktree.get()
        git_worktree.git.rebase(ref)
        self.set_wpt_base(ref)

    @mut()
    def set_bug_data(self, status=None):
        if self.bug:
            whiteboard = env.bz.get_whiteboard(self.bug)
            if not whiteboard:
                return
            current_subtype, current_status = bug.get_sync_data(whiteboard)
            if current_subtype != self.sync_type or current_status != status:
                new_whiteboard = bug.set_sync_data(whiteboard, self.sync_type, status)
                env.bz.set_whiteboard(self.bug, new_whiteboard)

    @mut()
    def delete(self):
        for worktree in [self.gecko_worktree, self.wpt_worktree]:
            worktree.delete()
        self.process_name.delete()


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
