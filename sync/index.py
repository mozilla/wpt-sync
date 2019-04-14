import json

import git

import log
from base import ProcessName, create_commit
from lock import RepoLock


logger = log.get_logger(__name__)


def lock(f):
    def inner(self, *args, **kwargs):
        with RepoLock(self.repo):
            return f(self, *args, **kwargs)
    inner.__name__ = f.__name__
    inner.__doc__ = f.__doc__
    return inner


class Index(object):
    name = None
    key_fields = ()
    unique = False
    value_cls = tuple

    def __init__(self, repo):
        self.repo = repo
        self.ref = git.Reference(repo, "refs/syncs/index/%s" % self.name)

    @classmethod
    def create(cls, repo):
        logger.info("Creating index %s" % cls.name)
        data = {"name": cls.name,
                "fields": list(cls.key_fields),
                "unique": cls.unique}
        tree = {"_metadata": json.dumps(data, indent=0)}
        commit = create_commit(repo, tree, message="Create index %s" % cls.name)
        ref = git.Reference(repo, "refs/syncs/index/%s" % cls.name)
        ref.set_object(commit.sha1)
        assert ref.is_valid()
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
        path = "/".join(key)
        _, items = self._read_path(path)
        if self.unique and len(key) == len(self.key_fields) and len(items) > 1:
            raise ValueError("Got multiple results for unique index")

        rv = set()
        for item in items:
            rv.add(self.load_value(item))

        if self.unique:
            return rv.pop() if rv else None
        return rv

    def _read_path(self, path):
        try:
            root = self.ref.commit.tree[path]
        except KeyError:
            return None, set()

        if isinstance(root, git.Blob):
            return root, self._load_obj(root)

        rv = set()
        for obj in root.traverse():
            if isinstance(obj, git.Blob):
                rv |= self._load_obj(obj)

        return root, rv

    def _load_obj(self, obj):
        rv = json.load(obj.data_stream)
        if isinstance(rv, list):
            return set(rv)
        return set([rv])

    @lock
    def insert(self, key, value):
        if len(key) != len(self.key_fields):
            raise ValueError

        path = "/".join(key)
        existing_tree, existing_items = self._read_path(path)

        if existing_tree and self.unique:
            raise ValueError("Tried to insert duplicate entry for unique index %s" % (key,))
        elif existing_tree:
            index_value = existing_items
            index_value.add(self.dump_value(value))
            index_value = list(sorted(index_value))
        else:
            index_value = self.dump_value(value)

        commit = create_commit(self.repo,
                               {path: json.dumps(index_value, indent=0)},
                               message="Insert key %s" % (key,),
                               initial=self.ref.commit.tree,
                               parents=[self.ref.commit.hexsha])
        self.ref.set_object(commit.sha1)

    @lock
    def delete(self, key, value):
        if len(key) != len(self.key_fields):
            raise ValueError

        path = "/".join(key)
        try:
            _, existing_items = self._read_path(path)
        except KeyError:
            existing_items = None
        if not existing_items:
            return

        value_str = self.dump_value(value)
        if len(existing_items) == 1:
            if existing_items.pop() != value_str:
                return
            tree = {}
            message = "Delete key %s" % (key,),
            delete = [path]
        else:
            index_value = existing_items
            index_value.remove(value_str)
            index_value = list(sorted(index_value))

            tree = {path: json.dumps(index_value, indent=0)}
            message = "Delete key %s value %s" % (key, value)
            delete = None

        commit = create_commit(self.repo,
                               tree,
                               message,
                               delete=delete,
                               initial=self.ref.commit.tree.hexsha,
                               parents=[self.ref.commit.hexsha])
        self.ref.set_object(commit.sha1)

    def dump_value(self, value):
        return str(value)

    def load_value(self, value):
        return self.value_cls(*("/".split(value)))

    @lock
    def clear(self):
        raise NotImplementedError

    def build(self, *args, **kwargs):
        raise NotImplementedError

    def make_key(self, value):
        return (value,)


class TaskGroupIndex(Index):
    name = "taskgroup"
    key_fields = ("taskgroup-id-0", "taskgroup-id-1", "taskgroup-id-2")
    unique = True
    value_cls = ProcessName

    def make_key(self, value):
        return (value[:2], value[2:4], value[4:])


class TryCommitIndex(Index):
    name = "try-commit"
    key_fields = ("commit-0", "commit-1", "commit-2", "commit-3")
    unique = True
    value_cls = ProcessName

    def make_key(self, value):
        return (value[:2], value[2:4], value[4:6], value[6:])
