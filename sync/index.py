import json
from collections import defaultdict

import git

import log
from base import ProcessName, CommitBuilder


logger = log.get_logger(__name__)


class Index(object):
    name = None
    key_fields = ()
    unique = False
    value_cls = tuple

    # Overridden in subclasses using the constructor
    changes = None

    def __init__(self, repo):
        if self.__class__.changes is None:
            constructors = [list]
            for _ in self.key_fields[:-1]:
                def fn():
                    idx = len(constructors) - 1
                    constructors.append(lambda: defaultdict(constructors[idx]))
                    return constructors[-1]
                fn()
            self.changes = defaultdict(constructors[-1])

        self.repo = repo
        self.ref = git.Reference(repo, "refs/syncs/index/%s" % self.name)

    @classmethod
    def create(cls, repo):
        logger.info("Creating index %s" % cls.name)
        data = {"name": cls.name,
                "fields": list(cls.key_fields),
                "unique": cls.unique}
        tree = {"_metadata": json.dumps(data, indent=0)}
        with CommitBuilder(repo,
                           message="Create index %s" % cls.name,
                           ref="refs/syncs/index/%s" % cls.name) as commit:
            commit.add_tree(tree)
        return cls(repo)

    @classmethod
    def get_or_create(cls, repo):
        ref_name = "refs/syncs/index/%s" % cls.name
        if not git.Reference(repo, ref_name).is_valid():
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
        path = "/".join(key)
        try:
            root = self.ref.commit.tree[path]
        except KeyError:
            data = set()
        else:
            if isinstance(root, git.Blob):
                data = self._load_obj(root)
            else:
                data = set()
                for obj in root.traverse():
                    if isinstance(obj, git.Blob):
                        items = self._load_obj(obj)
                    data |= items
        if include_local:
            self._update_changes(key, data)
        return data

    def _read_changes(self, key):
        target = self.changes
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
        rv = json.load(obj.data_stream)
        if isinstance(rv, list):
            return set(rv)
        return set([rv])

    def save(self):
        message = "Update index %s\n\n" % self.name
        changes = self._read_changes(None)

        for key_changes in changes.itervalues():
            for _, _, msg in key_changes:
                message += "%s\n" % msg
        with CommitBuilder(self.repo,
                           message,
                           ref=self.ref.path,
                           initial=self.ref.commit.tree) as commit:
            for key, key_changes in changes.iteritems():
                self._update_key(commit, key, key_changes)
        self.__class__.changes = None

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
            if new_value is None and old_value in existing:
                new.remove(old_value)
            if old_value is None:
                new.add(new_value)

        path = "/".join(key)

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

    def clear(self):
        raise NotImplementedError

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
            self.insert(self.make_key(try_push.taskgroup_id),
                        try_push.process_name)


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
            self.insert(self.make_key(try_push.try_rev),
                        try_push.process_name)


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
                sync.process_name.obj_id)

    def build(self, git_gecko, git_wpt, **kwargs):
        from base import ProcessName
        from downstrea import DownstreamSync
        from upstream import UpstreamSync
        from landing import LandingSync

        for ref in git_gecko.references:
            process_name = ProcessName.from_ref(ref.path)
            sync_cls = None
            if process_name.subtype == "upstream":
                sync_cls = UpstreamSync
            elif process_name.subtype == "downstream":
                sync_cls = DownstreamSync
            elif process_name.subtype == "landing":
                sync_cls = LandingSync

            if sync_cls:
                sync = sync_cls(git_gecko, git_wpt, process_name)
                self.insert(self.make_key(sync), process_name)


class PrIdIndex(Index):
    name = "pr-id"
    key_fields = ("pr-id",)
    unique = True
    value_cls = ProcessName

    @classmethod
    def make_key(cls, sync):
        return (str(sync.pr),)

    def build(self, git_gecko, git_wpt, **kwargs):
        from base import ProcessName
        from upstream import UpstreamSync

        for ref in git_gecko.references:
            process_name = ProcessName.from_ref(ref.path)
            if process_name.subtype == "upstream":
                sync = UpstreamSync(git_gecko, git_wpt, process_name)
                self.insert(self.make_key(sync), process_name)
            elif process_name.subtype == "downstream":
                self.insert((process_name.obj_id,), process_name)


class BugIdIndex(Index):
    name = "bug-id"
    key_fields = ("bug-id", "status")
    unique = False
    value_cls = ProcessName

    @classmethod
    def make_key(cls, sync):
        return (sync.bug, sync.status)

    def build(self, git_gecko, git_wpt, **kwargs):
        from base import ProcessName
        from downstrea import DownstreamSync
        from upstream import UpstreamSync
        from landing import LandingSync

        for ref in git_gecko.references:
            process_name = ProcessName.from_ref(ref.path)
            sync_cls = None
            if process_name.subtype == "upstream":
                sync_cls = UpstreamSync
            elif process_name.subtype == "downstream":
                sync_cls = DownstreamSync
            elif process_name.subtype == "landing":
                sync_cls = LandingSync

            if sync_cls:
                sync = sync_cls(git_gecko, git_wpt, process_name)
                self.insert(self.make_key(sync), process_name)

indicies = {item.name: item for item in globals()
            if type(item) == type(Index) and
            issubclass(item, Index) and
            item != index}
