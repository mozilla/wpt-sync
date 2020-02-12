import enum
import itertools
import traceback
from collections import defaultdict

import bug
import commit as sync_commit
import log
from base import BranchRefObject, IdentityMap, ProcessData, ProcessName, ProcessNameIndex
from env import Environment
from lock import MutGuard, mut, constructor
from worktree import Worktree

env = Environment()

logger = log.get_logger(__name__)


class CommitFilter(object):
    """Filter of a range of commits"""
    def __init__(self):
        self._commits = {}

    def path_filter(self):
        """Path filter for the commit range,
        returning a list of paths that match or None to
        match all paths."""
        return None

    def filter_commit(self, commit):
        """Per-commit filter.

        :param commit: wpt_commit.Commit object
        :returns: A boolean indicating whether to include the commit"""
        if commit.sha1 not in self._commits:
            self._commits[commit.sha1] = self._filter_commit(commit)
        return self._commits[commit.sha1]

    def _filter_commit(self, commit):
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
        self._base_commit = None
        self._base = base
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
        return (self._head_ref.name.subtype,
                self._head_ref.name.obj_id)

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
        if self._base_commit is None:
            self._base_commit = self.commit_cls(self.repo, self._base)
        return self._base_commit

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
        self._base_commit = None

    @head.setter
    @mut()
    def head(self, value):
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

    def reason_str(self):
        return {LandableStatus.ready: "Ready",
                LandableStatus.no_pr: "No PR",
                LandableStatus.upstream: "From gecko",
                LandableStatus.no_sync: "No sync created",
                LandableStatus.error: "Error",
                LandableStatus.missing_try_results: "Incomplete try results",
                LandableStatus.skip: "Skip"}.get(self, "Unknown")


class SyncPointName(object):
    """Like a process name but for pointers that aren't associated with a
    specific sync object, but with a general process e.g. the last update point
    for an upstream sync."""
    __metaclass__ = IdentityMap

    def __init__(self, subtype, obj_id):
        self._obj_type = "sync"
        self._subtype = subtype
        self._obj_id = str(obj_id)

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


class SyncData(ProcessData):
    obj_type = "sync"


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

        self.data = SyncData(git_gecko, process_name)

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
        self._indexes = {ProcessNameIndex(git_gecko)}

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
        idx = index.PrIdIndex(git_gecko)
        process_name = idx.get((str(pr_id),))
        if process_name and process_name.subtype == cls.sync_type:
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
        bug = str(bug)
        statuses = set(statuses) if statuses is not None else set(cls.statuses)
        rv = defaultdict(set)
        idx_key = (bug,)
        if len(statuses) == 1:
            idx_key == (bug, list(statuses)[0])
        idx = index.BugIdIndex(git_gecko)

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
    def load_by_obj(cls, git_gecko, git_wpt, obj_id, seq_id=None):
        process_names = ProcessNameIndex(git_gecko).get(cls.obj_type,
                                                        cls.sync_type,
                                                        str(obj_id))
        if seq_id is not None:
            process_names = {item for item in process_names
                             if item.seq_id == int(seq_id)}
        return {cls(git_gecko, git_wpt, process_name) for process_name in process_names}

    @classmethod
    def load_by_status(cls, git_gecko, git_wpt, status):
        import index
        idx = index.SyncIndex(git_gecko)
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
                       self.error["message"] or "",
                       self.error["stack"] or ""])
        landable_status = self.landable_status
        if landable_status:
            rv.append("Landable status: %s" % landable_status.reason_str())

        for key, value in sorted(self.data.items()):
            if key != "error":
                rv.append("%s: %s" % (key, value))

        try_pushes = self.try_pushes()
        if try_pushes:
            try_pushes = sorted(try_pushes, key=lambda x: x.process_name.seq_id)
            rv.append("Try pushes:")
            for try_push in try_pushes:
                rv.append("  %s %s" % (try_push.status,
                                       try_push.treeherder_url))
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
        return env.config["gecko"]["refs"][env.config["gecko"]["landing"]]

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
            if push.infra_fail:
                busted.append(push)
            else:
                break
        return busted

    @property
    def latest_try_push(self):
        try_pushes = self.try_pushes()
        if try_pushes:
            try_pushes = sorted(try_pushes, key=lambda x: x.process_name.seq_id)
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
        # TODO: this object creation is extremely non-atomic :/
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
        SyncData.create(lock, git_gecko, process_name, data)
        BranchRefObject.create(lock, git_gecko, process_name, gecko_head, sync_commit.GeckoCommit)
        BranchRefObject.create(lock, git_wpt, process_name, wpt_head, sync_commit.WptCommit)

        rv = cls(git_gecko, git_wpt, process_name)

        idx = index.SyncIndex(git_gecko)
        idx.insert(idx.make_key(rv), process_name).save()

        if cls.obj_id == "bug":
            bug_idx = index.BugIdIndex(git_gecko)
            bug_idx.insert(bug_idx.make_key(rv), process_name).save()
        elif cls.obj_id == "pr":
            pr_idx = index.PrIdIndex(git_gecko)
            pr_idx.insert(pr_idx.make_key(rv), process_name).save()
        return rv

    @mut()
    def finish(self, status="complete"):
        # TODO: cancel related try pushes &c.
        logger.info("Marking sync %s as %s" % (self.process_name, status))
        self.status = status
        self.error = None
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
        import index
        for worktree in [self.gecko_worktree, self.wpt_worktree]:
            worktree.delete()

        for try_push in self.try_pushes():
            with try_push.as_mut(self._lock):
                try_push.delete()

        for git, commit_cls in [(self.git_wpt, sync_commit.WptCommit),
                                (self.git_gecko, sync_commit.GeckoCommit)]:
            BranchRefObject(git, self.process_name, commit_cls=commit_cls).delete()

        for idx_cls in [index.SyncIndex, index.PrIdIndex, index.BugIdIndex]:
            key = idx_cls.make_key(self)
            idx = idx_cls(self.git_gecko)
            idx.delete(key, self.process_name).save()

        self.data.delete()
