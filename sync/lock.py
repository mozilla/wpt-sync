import inspect
import os

import filelock

import log
from env import Environment

env = Environment()

logger = log.get_logger(__name__)


"""
Locking system for the wpt sync.

There are two principal kinds of lock defined here; the RepoLock that's
used to protect against concurrent writes to the underlying storage, and
the SyncLock, which is used to ensure that multiples processes don't try
to update the same sync at the same time.

Since the sync is run from multiple processes without shared memory,
and each process is single threaded, the locks must be based on files.

A RepoLock is pretty straightforward; only one process is allowed to
acquire the RepoLock at a time. Like all locks it's intended to be used
as a context manager, and there's a repo_lock decoration for functions
and methods that must acquire the lock for their entire body.

For the SyncLock, things are more complex. Processes are put into the
following groups:
* All upstream syncs
* All landing syncs
* Downstream syncs for each pr

Within each group operations that update data must be sequenced i.e.
there is a lock that must be obtained for each group when writing.
That lock is represented by the SyncLock object and it has an
associated "subtype" which is the kind of sync (upstream, downstream,
or landing) and object id which is the PR id for downstream syncs
or None otherwise.

In order to allow for read-only access to sync objects and to ensure
that we don't have data races, functions and methods that cause mutation
to the underlying data are annotated with the @mut() decorator. When
applied to a class method this does the following

* Ensures that the instance has a property called _lock
* Ensures that the _lock property is a acquired SyncLock with the
  right attributes for mutation of this class.

In order to set the properties, classes with mutable state must provide
an as_mut() method that takes the current lock as an argument. This returns
a context manager object that ensures mutation methods are accessible for
the defined context. This is clearer with an example:

# Ensure we have the lock for upstream syns
with SyncLock("upstream", None) as lock:
   sync = UpstreamSync.for_bug(git_gecko, git_wpt, bug)

   # Reading the sync is possible here
   print sync.status

   # But writing will fail unless we are in a mut block
   with sync.as_mut(lock):
       sync.update_wpt_commits()

The @mut decorator can also be used on functions to check that arguments
passed into those functions are available for mutation; in this case the
decorator takes the names of the arguments to check e.g.

@mut("sync")
def some_update_fn(git_gecko, git_wpt, sync):
    # Sync must be mutable here or we would fail
    sync.update_wpt_commits()

Constructing new objects is also a form of mutation, but because an object
doesn't exist yet a slightly different approach is required. The
@constructor decorator marks a classmethod that creates new instances and
can only be called with an appropriate lock. But in this case the lock is
passed as the first argument to the function, and the lock checking is done
by providing a function argument to the decorator that takes the arguments
to the constructor and returns a (subtype, obj_id) pair used to check for
lock validity e.g.

@classmethod
@constructor(lambda kwargs: (kwargs["process_name"].subtype,
                             kwargs["process_name"].obj_id))
def create(cls, lock, git_gecko, git_wpt, process_name):
    pass

All objects that can cause mutation of the underlying sync data must
implement this locking system. In order to do so, the object must
provide the following methods and properties:

property _lock - None or a SyncLock representing the currenly held lock
method lock_key - Returns the (subtype, obj_id) pair for the current
                  instance, used to ensure the lock is valid
method as_mut - Used to make the object mutable. Returns a MutGuard
                wrapping the current object.

"""


class LockError(Exception):
    pass


class Lock(object):
    locks = {}

    def __init__(self, *args):
        self.path = self.lock_path(*args)
        self.lock = filelock.FileLock(self.path)

    def __enter__(self):
        if self.path in self.locks:
            # If this is already locked by the current process
            # then locking again is a no-op
            return self.locks[self.path]
        self.locks[self.path] = self
        self.lock.acquire()
        return self

    def __exit__(self, *args, **kwargs):
        if self.locks[self.path] != self:
            return
        del self.locks[self.path]
        self.lock.release()

    @staticmethod
    def lock_path(*args):
        """Return a path to the file representing the current lock"""
        raise NotImplementedError


class RepoLock(Lock):
    def __init__(self, repo):
        super(RepoLock, self).__init__(repo)

    @staticmethod
    def lock_path(repo):
        return os.path.join(
            env.config["root"],
            env.config["paths"]["locks"],
            "%s.lock" % (repo.working_dir.replace(os.path.sep, "_"),))


class ProcessLock(Lock):
    obj_types = None
    lock_type = None
    lock_per_type = set()
    lock_per_obj = set()

    locks = {}

    def __init__(self, sync_type, obj_id):
        assert sync_type in self.lock_per_obj | self.lock_per_type

        if sync_type in self.lock_per_type:
            if obj_id is not None:
                raise ValueError("%s must be locked over all objects" % sync_type)
        elif obj_id is None:
            raise ValueError("%s must be locked over each object" % sync_type)
        self.sync_type = sync_type
        self.obj_id = obj_id
        super(ProcessLock, self).__init__(self.lock_type, sync_type, obj_id)

    @classmethod
    def for_process(cls, process_name):
        """Get the SyncLock for the provided ProcessName."""
        # This is sort of an antipattern because it requires the class to know about consumers.
        # But it also enforces some invariants to ensure that things have the right kind of
        # lock
        assert process_name.obj_type in cls.obj_types
        sync_type = process_name.subtype
        obj_id = process_name.obj_id if sync_type in cls.lock_per_obj else None
        return cls(sync_type, obj_id)

    def check(self, sync_type, obj_id):
        """Check that the current lock is valid for the provided sync_type and obj_id"""
        if sync_type in self.lock_per_type:
            obj_id = None
        if not (sync_type == self.sync_type and
                obj_id == self.obj_id and
                self.lock.is_locked):
            raise ValueError("""Got wrong lock, expected sync_type:%s obj_id:%s locked:True,
                got: sync_type:%s obj_id:%s locked:%s""" %
                             (sync_type,
                              obj_id,
                              self.sync_type,
                              self.obj_id,
                              self.lock.is_locked))

    @staticmethod
    def lock_path(obj_type, sync_type, obj_id):
        if obj_id is None:
            filename = "%s_%s.lock" % (obj_type, sync_type)
        else:
            filename = "%s_%s_%s.lock" % (obj_type, sync_type, obj_id)
        return os.path.join(
            env.config["root"],
            env.config["paths"]["locks"],
            filename)


class SyncLock(ProcessLock):
    obj_types = ("sync", "try")
    lock_type = "sync"
    lock_per_type = {"landing", "upstream"}
    lock_per_obj = {"downstream"}

    locks = {}


class ProcLock(ProcessLock):
    obj_types = ("proc",)
    lock_type = "proc"
    lock_per_type = {"bugzilla"}
    lock_per_obj = set()

    locks = {}


class MutGuard(object):
    def __init__(self, lock, instance, props=None):
        """Context Manager wrapping an object that is to be accessed for mutation.

        Mutability is re-entrant in the sense that if we already have a certain object
        in a mutable state, an attempt to make the same object mutable using the same
        lock will succeed.
        """
        self.instance = instance
        self.lock = lock
        self.props = props or []
        self.owned_guards = []
        lock.check(*instance.lock_key)
        self.took_lock = None

    def __enter__(self):
        logger.debug("Making object mutable %r" % self.instance)
        if self.instance._lock is not None:
            if self.instance._lock is not self.lock:
                raise ValueError("Tried to re-lock %s with a different lock" % self.instance)
            self.took_lock = False
            return

        self.took_lock = True
        self.instance._lock = self.lock

        for prop in self.props:
            if prop._lock is None:
                self.owned_guards.append(prop.as_mut(self.lock))
                self.owned_guards[-1].__enter__()

        return self.instance

    def __exit__(self, *args, **kwargs):
        try:
            if not self.took_lock:
                return
            while self.owned_guards:
                guard = self.owned_guards.pop()
                guard.__exit__(*args, **kwargs)
            if hasattr(self.instance, "exit_mut"):
                self.instance.exit_mut()
            self.instance._lock = None
        finally:
            self.took_lock = None


class mut(object):
    def __init__(self, *args):
        """Mark a function as requiring given arguments are mutable.

        When entering the function the decorator checks that the specified
        arguments are locked for mutation with an appropriate lock.

        When no arguments are specified, self is used as a default,
        appropriate for marking instance methods that require the
        object to be locked for mutation."""
        if not args:
            args = ("self",)
        self.args = args

    def __call__(self, f):
        def inner(*args, **kwargs):
            arg_values = inspect.getcallargs(f, *args, **kwargs)

            for arg in self.args:
                arg_value = arg_values[arg]
                if arg_value._lock is None:
                    raise ValueError("Tried to use %r as mutable without locking" % arg_value)
                arg_value._lock.check(*arg_value.lock_key)

            return f(*args, **kwargs)
        inner.__name__ = f.__name__
        inner.__doc__ = f.__doc__
        return inner


class constructor(object):
    def __init__(self, arg_func):
        """Mark a classmethod as a constructor for an object which uses the
        mutation system.

        The decorator takes a single function as an argument. This function
        is run against the arguments provided to the decorator and returns a
        tuple of (subtype, obj_id) representing the kind of lock that needs
        to be held to construct the object."""

        self.arg_func = arg_func

    def __call__(self, f):
        def inner(cls, lock, *args, **kwargs):
            if lock is None:
                raise ValueError("Tried to access constructor %s without locking")

            arg_values = inspect.getcallargs(f, cls, lock, *args, **kwargs)
            sync_type, obj_id = self.arg_func(arg_values)

            lock.check(sync_type, obj_id)
            return f(cls, lock, *args, **kwargs)
        inner.__name__ = f.__name__
        inner.__doc__ = f.__doc__
        return inner
