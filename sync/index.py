import json
from collections import defaultdict

import git

import env
import log
from base import ProcessName, CommitBuilder, iter_process_names
from repos import pygit2_get


logger = log.get_logger(__name__)
env = env.Environment()


class Index(object):
    name = None
    key_fields = ()
    unique = False
    value_cls = tuple

    # Overridden in subclasses using the constructor
    # This provides a kind of borg pattern where all instances of
    # the class have the same changes data
    changes = None

    def __init__(self, repo):
        if self.__class__.changes is None:
            self.reset()
        self.repo = repo
        self.pygit2_repo = pygit2_get(repo)

    def reset(self):
        constructors = [list]
        for _ in self.key_fields[:-1]:
            def fn():
                idx = len(constructors) - 1
                constructors.append(lambda: defaultdict(constructors[idx]))
                return constructors[-1]
            fn()
        self.__class__.changes = defaultdict(constructors[-1])

    @classmethod
    def create(cls, repo):
        logger.info("Creating index %s" % cls.name)
        data = {"name": cls.name,
                "fields": list(cls.key_fields),
                "unique": cls.unique}
        meta_path = "index/%s/_metadata" % cls.name
        tree = {meta_path: json.dumps(data, indent=0)}
        with CommitBuilder(repo,
                           message="Create index %s" % cls.name,
                           ref=env.config["sync"]["ref"]) as commit:
            commit.add_tree(tree)
        return cls(repo)

    @classmethod
    def get_root_path(cls):
        return "index/%s" % cls.name

    @classmethod
    def get_or_create(cls, repo):
        ref_name = env.config["sync"]["ref"]
        ref = git.Reference(repo, ref_name)
        try:
            ref.commit.tree["index/%s" % cls.name]
        except KeyError:
            return cls.create(repo)
        return cls(repo)

    def get(self, key):
        if len(key) > len(self.key_fields):
            raise ValueError
        items = self._read(key)
        if self.unique and len(key) == len(self.key_fields) and len(items) > 1:
            raise ValueError("Got multiple results for unique index")

        rv = set()
        for item in items:
            rv.add(self.load_value(item))

        if self.unique:
            return rv.pop() if rv else None
        return rv

    def _read(self, key, include_local=True):
        path = "%s/%s" % (self.get_root_path(), "/".join(key))
        data = set()
        for obj in iter_blobs(self.pygit2_repo, path):
            data |= self._load_obj(obj)
        if include_local:
            self._update_changes(key, data)
        return data

    def _read_changes(self, key):
        target = self.changes
        if target is None:
            return {}
        if key:
            for part in key:
                target = target[part]
        else:
            key = ()
        changes = {}
        stack = [(key, target)]
        while stack:
            key, items = stack.pop()
            if isinstance(items, list):
                changes[key] = items
            else:
                for key_part, values in items.iteritems():
                    stack.append((key + (key_part,), values))
        return changes

    def _update_changes(self, key, data):
        changes = self._read_changes(key)
        for key_changes in changes.itervalues():
            for old_value, new_value, _ in key_changes:
                if new_value is None and old_value in data:
                    data.remove(old_value)
                elif new_value is not None:
                    data.add(new_value)

    def _load_obj(self, obj):
        rv = json.loads(obj.data)
        if isinstance(rv, list):
            return set(rv)
        return set([rv])

    def save(self, commit_builder=None, message=None, overwrite=False):
        changes = self._read_changes(None)
        if not changes:
            return

        if commit_builder is None:
            commit_builder = CommitBuilder(self.repo,
                                           message,
                                           ref=env.config["sync"]["ref"],
                                           initial_empty=overwrite)
        else:
            assert commit_builder.initial_empty == overwrite

        if message is None:
            message = "Update index %s\n" % self.name

            for key_changes in changes.itervalues():
                for _, _, msg in key_changes:
                    message += "  %s\n" % msg
            commit_builder.message += message

        with commit_builder as commit:
            for key, key_changes in changes.iteritems():
                self._update_key(commit, key, key_changes)
        self.reset()

    def insert(self, key, value):
        if len(key) != len(self.key_fields):
            raise ValueError

        value = self.dump_value(value)
        msg = "Insert key %s value %s" % (key, value)
        target = self.changes
        for part in key:
            target = target[part]
        target.append((None, value, msg))
        return self

    def delete(self, key, value):
        if len(key) != len(self.key_fields):
            raise ValueError

        value = self.dump_value(value)
        msg = "Delete key %s value %s" % (key, value)
        target = self.changes
        for part in key:
            target = target[part]
        target.append((value, None, msg))
        return self

    def move(self, old_key, new_key, value):
        assert old_key != new_key

        if old_key is not None:
            self.delete(old_key, value)
        if new_key is not None:
            self.insert(new_key, value)
        return self

    def _update_key(self, commit, key, key_changes):
        existing = self._read(key, False)
        new = existing.copy()

        for old_value, new_value, _ in key_changes:
            if new_value is None and old_value in new:
                new.remove(old_value)
            if old_value is None:
                new.add(new_value)

        path_suffix = "/".join(key)

        path = "%s/%s" % (self.get_root_path(), path_suffix)

        if new == existing:
            return

        if not new:
            commit.delete([path])
            return

        if self.unique and len(new) > 1:
            raise ValueError("Tried to insert duplicate entry for unique index %s" % (key,))

        index_value = list(sorted(new))

        commit.add_tree({path: json.dumps(index_value, indent=0)})

    def dump_value(self, value):
        return str(value)

    def load_value(self, value):
        return self.value_cls(*(value.split("/")))

    def build(self, *args, **kwargs):
        raise NotImplementedError

    @classmethod
    def make_key(cls, value):
        return (value,)


class TaskGroupIndex(Index):
    name = "taskgroup"
    key_fields = ("taskgroup-id-0", "taskgroup-id-1", "taskgroup-id-2")
    unique = True
    value_cls = ProcessName

    @classmethod
    def make_key(cls, value):
        return (value[:2], value[2:4], value[4:])

    def build(self, *args, **kwargs):
        import trypush
        for try_push in trypush.TryPush.load_all(self.repo):
            if try_push.taskgroup_id is not None:
                self.insert(self.make_key(try_push.taskgroup_id),
                            try_push.process_name)
        self.save(message="Build index %s" % self.name, overwrite=True)


class TryCommitIndex(Index):
    name = "try-commit"
    key_fields = ("commit-0", "commit-1", "commit-2", "commit-3")
    unique = True
    value_cls = ProcessName

    @classmethod
    def make_key(cls, value):
        return (value[:2], value[2:4], value[4:6], value[6:])

    def build(self, *args, **kwargs):
        import trypush
        for try_push in trypush.TryPush.load_all(self.repo):
            if try_push.try_rev:
                self.insert(self.make_key(try_push.try_rev),
                            try_push.process_name)
        self.save(message="Build index %s" % self.name, overwrite=True)


class SyncIndex(Index):
    name = "sync-id-status"
    key_fields = ("objtype", "subtype", "status", "obj_id")
    unique = False
    value_cls = ProcessName

    @classmethod
    def make_key(cls, sync):
        return (sync.process_name.obj_type,
                sync.process_name.subtype,
                sync.status,
                str(sync.process_name.obj_id))

    def build(self, git_gecko, git_wpt, **kwargs):
        from downstream import DownstreamSync
        from upstream import UpstreamSync
        from landing import LandingSync

        corrupt = []

        for process_name in iter_process_names(self.pygit2_repo, kind=["sync"]):
            sync_cls = None
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
                    corrupt.append(process_name)
                    continue
                self.insert(self.make_key(sync), process_name)
        self.save(message="Build index %s" % self.name, overwrite=True)
        for item in corrupt:
            logger.warning("Corrupt process %s" % item)


class PrIdIndex(Index):
    name = "pr-id"
    key_fields = ("pr-id",)
    unique = True
    value_cls = ProcessName

    @classmethod
    def make_key(cls, sync):
        return (str(sync.pr),)

    def build(self, git_gecko, git_wpt, **kwargs):
        from downstream import DownstreamSync

        corrupt = []

        for process_name in iter_process_names(self.pygit2_repo, kind=["sync"]):
            if process_name.subtype == "downstream":
                try:
                    sync = DownstreamSync(git_gecko, git_wpt, process_name)
                except ValueError:
                    corrupt.append(process_name)
                    continue
                self.insert(self.make_key(sync), process_name)
            elif process_name.subtype == "downstream":
                self.insert((process_name.obj_id,), process_name)
        self.save(message="Build index %s" % self.name, overwrite=True)

        for item in corrupt:
            logger.warning("Corrupt process %s" % item)


class BugIdIndex(Index):
    name = "bug-id"
    key_fields = ("bug-id", "status")
    unique = False
    value_cls = ProcessName

    @classmethod
    def make_key(cls, sync):
        return (str(sync.bug), sync.status)

    def build(self, git_gecko, git_wpt, **kwargs):
        from downstream import DownstreamSync
        from upstream import UpstreamSync
        from landing import LandingSync

        corrupt = []

        for process_name in iter_process_names(self.pygit2_repo, kind=["sync"]):
            sync_cls = None
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
                    corrupt.append(process_name)
                    continue
                self.insert(self.make_key(sync), process_name)
        self.save(message="Build index %s" % self.name, overwrite=True)

        for item in corrupt:
            logger.warning("Corrupt process %s" % item)


def iter_blobs(repo, path):
    """Iterate over all blobs under a path

    :param repo: pygit2 repo
    :param path: path to use as the root, or None for the root path
    """
    ref = repo.references[env.config["sync"]["ref"]]
    root = repo[ref.peel().tree.id]
    if path is not None:
        if path not in root:
            return
        root_entry = root[path]
        root = repo[root_entry.id]
        if root_entry.type == "blob":
            yield root
            return

    stack = [root]
    while stack:
        tree = stack.pop()
        for item in tree:
            if item.type == "blob":
                yield repo[item.id]
            else:
                stack.append(repo[item.id])


indicies = {item for item in globals().values()
            if type(item) == type(Index) and
            issubclass(item, Index) and
            item != Index}
