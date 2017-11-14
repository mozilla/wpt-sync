import json
import os
import re
import shutil
import traceback

import git

import log
import commit as sync_commit
from env import Environment

env = Environment()

invalid_re = re.compile(".*[-_]")

logger = log.get_logger("base")


class ProcessName(object):
    def __init__(self, obj_type, status, obj_id, seq_id=None):
        self._obj_type = obj_type
        self._status = status
        self._obj_id = str(obj_id)
        self._seq_id = str(seq_id) if seq_id is not None else None
        self._refs = []

    def __str__(self):
        return self.name(self._obj_type,
                         self._status,
                         self._obj_id,
                         self._seq_id)

    @staticmethod
    def name(obj_type, status, obj_id, seq_id=None):
        rv = "sync/%s/%s/%s" % (obj_type,
                                status,
                                obj_id)
        if seq_id is not None:
            rv = "%s/%s" % (rv, seq_id)
        return rv

    def attach_ref(self, ref):
        # This two-way binding is kind of nasty, but we want the
        # refs to update every time we update the property here
        # and that makes it easy to achieve
        self._refs.append(ref)
        return self

    def delete(self):
        for ref in self._refs:
            ref.delete()

    @property
    def type(self):
        return self._obj_type

    @property
    def obj_id(self):
        return self._obj_id

    @obj_id.setter
    def obj_id(self, value):
        if self._obj_id != value:
            self._obj_id = value
            self._update_refs()

    @property
    def status(self):
        return self._status

    @status.setter
    def status(self, value):
        if self._status != value:
            self._status = value
            self._update_refs()

    def _update_refs(self):
        for ref_obj in self._refs:
            ref = ref_obj.ref
            ref.rename(str(ref))

    @classmethod
    def from_ref(cls, ref):
        parts = ref.split("/")
        if parts[0] == "refs":
            parts = parts[2:]
        if parts[0] != "sync":
            return None
        return cls(*parts[1:])

    @classmethod
    def commit_refs(cls, repo, ref_type, obj_type, status="open", obj_id="*", seq_id=None):
        branch_filter = "refs/%s/sync/%s/%s/%s" % (ref_type, obj_type, status, obj_id)
        if seq_id is not None:
            branch_filter = "%s/%s" % (branch_filter, seq_id)
        commits_refs = repo.git.for_each_ref("--format", "%(objectname) %(refname:short)",
                                             branch_filter)

        commit_refs = {}
        for line in commits_refs.split("\n"):
            line = line.strip()
            if not line:
                continue
            commit, ref = line.split(" ", 1)
            ref = "/".join(ref.split("/")[1:])
            commit_refs[commit] = ref
        return commit_refs


class SyncPointName(object):
    """Like a process name but for pointers that aren't associated with a
    specific sync object, but with a general process"""
    def __init__(self, obj_type, obj_id):
        self._obj_type = obj_type
        self._obj_id = obj_id
        self._refs = []

    def attach_ref(self, ref):
        return self

    def __str__(self):
        return self.name(self._obj_type,
                         self._obj_id)

    @staticmethod
    def name(obj_type, obj_id):
        return "sync/%s/%s" % (obj_type,
                               obj_id)


class VcsRefObject(object):
    ref_prefix = None

    def __init__(self, repo, process_name, commit_cls=sync_commit.Commit):
        self.repo = repo
        self._process_name = process_name.attach_ref(self)
        self.commit_cls = commit_cls

    def __str__(self):
        return "refs/%s/%s" % (self.ref_prefix, str(self._process_name))

    def delete(self):
        self.repo.git.update_ref("-d", str(self))

    @classmethod
    def full_name(cls, obj_type, status, obj_id):
        return "refs/%s/%s" (cls.ref_prefix,
                             ProcessName.name(obj_type, status, id))

    @property
    def ref(self):
        name = str(self._process_name)
        return self.repo.refs[name] if name in self.repo.refs else None

    @property
    def commit(self):
        ref = self.ref
        if ref is not None:
            commit = self.commit_cls(self.repo, ref.commit)
            return commit

    @commit.setter
    def commit(self, commit):
        if isinstance(commit, sync_commit.Commit):
            commit = commit.commit
        self.create(self.repo, str(self), commit)

    @classmethod
    def load_all(cls, repo, obj_type, status="open", obj_id="*", seq_id=None,
                 commit_cls=sync_commit.Commit):
        rv = []
        for ref_name in sorted(ProcessName.commit_refs(repo,
                                                       ref_type=cls.ref_prefix,
                                                       obj_type=obj_type,
                                                       status=status,
                                                       obj_id=obj_id,
                                                       seq_id=None).itervalues()):
            rv.append(cls(repo, ProcessName.from_ref(ref_name), commit_cls))
        return rv


class BranchRefObject(VcsRefObject):
    ref_prefix = "heads"

    @classmethod
    def create(cls, repo, ref, commit):
        repo.create_head(ref, commit, force=True)


class TagRefObject(VcsRefObject):
    ref_prefix = "tags"

    def __init__(self, repo, process_name, commit_cls=sync_commit.Commit):
        VcsRefObject.__init__(self, repo, process_name, commit_cls=commit_cls)
        self._data = None

    @classmethod
    def create(cls, repo, ref, commit, message="{}"):
        repo.create_tag(ref, commit, message=message, force=True)

    @property
    def data(self):
        if self._data is None:
            self._data = ProcessData(self)
        return self._data

    @property
    def ref(self):
        name = str(self._process_name)
        return self.repo.tags[name] if name in self.repo.tags else None

    def update_message(self, message):
        assert self.ref is not None
        commit = self.commit.sha1
        self.repo.create_tag(str(self), commit, message=message, force=True)


class ProcessData(object):
    def __init__(self, tag_object):
        self._tag = tag_object
        self._data = self._load()

    def _load(self):
        if self._tag.ref is None:
            return {}
        data = self._tag.ref.tag.message
        return json.loads(data)

    def _save(self):
        message = json.dumps(self._data)
        self._tag.update_message(message)

    def __getitem__(self, key):
        return self._data[key]

    def __setitem__(self, key, value):
        if key not in self._data or self._data[key] != value:
            self._data[key] = value
            self._save()

    def get(self, key, default=None):
        return self._data.get(key, default)


class CommitFilter(object):
    def path_filter(self):
        return None

    def filter_commit(self, commit):
        return True

    def filter_commits(self, commits):
        return commits


class CommitRange(object):
    def __init__(self, repo, process_name, commit_cls, commit_filter=None):
        self.repo = repo
        self.base_ref = TagRefObject(repo,
                                     process_name,
                                     commit_cls=commit_cls)
        self.head_ref = BranchRefObject(repo,
                                        process_name,
                                        commit_cls=commit_cls)
        self.commit_cls = commit_cls
        self.commit_filter = commit_filter

        # Cache for the commits in this range
        self._commits = None
        self._head_sha = None

    @property
    def base(self):
        return self.base_ref.commit

    @base.setter
    def base(self, commit):
        self.base_ref.ref.set_commit(commit.sha1)

    @property
    def head(self):
        return self.head_ref.commit

    @head.setter
    def head(self, commit):
        self.head_ref.ref.set_commit(commit.sha1)

    def __getitem__(self, index):
        return self.commits[index]

    def __iter__(self):
        for item in self.commits:
            yield item

    def __len__(self):
        return len(self.commits)

    @property
    def commits(self):
        if self._head_sha and self.head.sha1 == self._head_sha and self._commits:
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
        return self._commits


class Worktree(object):
    def __init__(self, repo, process_name):
        self.repo = repo
        self._worktree = None
        self.process_name = process_name
        self.path = os.path.join(env.config["root"],
                                 env.config["paths"]["worktrees"],
                                 os.path.basename(repo.working_dir),
                                 process_name.obj_id)

    def get(self):
        if self._worktree is None:
            if os.path.exists(self.path):
                worktree = git.Repo(self.path)
                worktree.index.reset(str(self.process_name), working_tree=True)
            else:
                self.repo.git.worktree("prune")
                worktree = self.repo.git.worktree("add",
                                                  os.path.abspath(self.path),
                                                  str(self.process_name))
            self._worktree = git.Repo(self.path)
        assert self._worktree.active_branch.name == str(self.process_name)
        return self._worktree

    def delete(self):
        if os.path.exists(self.path):
            try:
                shutil.rmtree(self.path)
            except Exception:
                logger.warning("Failed to remove worktree %s:%s" %
                               (self.path, traceback.format_exc()))
            else:
                logger.debug("Removed worktree %s" % (self.path,))


class SyncProcess(object):
    sync_type = "*"
    obj_id = None  # Either "bug" or "pr"

    def __init__(self, git_gecko, git_wpt, process_name):
        self.git_gecko = git_gecko
        self.git_wpt = git_wpt

        self._process_name = process_name

        self.gecko_commits = CommitRange(git_gecko,
                                         self._process_name,
                                         commit_cls=sync_commit.GeckoCommit,
                                         commit_filter=self.gecko_commit_filter())
        self.wpt_commits = CommitRange(git_wpt,
                                       self._process_name,
                                       commit_cls=sync_commit.WptCommit,
                                       commit_filter=self.wpt_commit_filter())

        self.gecko_worktree = Worktree(git_gecko,
                                       self._process_name)
        self.wpt_worktree = Worktree(git_wpt,
                                     self._process_name)

        self._data = self.gecko_commits.base_ref.data

    def __eq__(self, other):
        if not hasattr(other, "_process_name"):
            return False
        str(self._process_name) == str(other._process_name)

    def __ne__(self, other):
        return not self == other

    def delete(self):
        for worktree in [self.gecko_worktree, self.wpt_worktree]:
            worktree.delete()
        self._process_name.delete()

    @staticmethod
    def gecko_integration_branch():
        return env.config["gecko"]["refs"]["mozilla-inbound"]

    def gecko_commit_filter(self):
        return CommitFilter()

    def wpt_commit_filter(self):
        return CommitFilter()

    def update_status(self, action, merged):
        """Update the status of the sync for a PR change"""
        pass

    @classmethod
    def new(cls, git_gecko, git_wpt, gecko_base, gecko_head,
            wpt_base="origin/master", wpt_head=None,
            bug=None, pr=None, status="open"):
        if cls.obj_id == "bug":
            assert bug is not None
            obj_id = bug
        elif cls.obj_id == "pr":
            assert pr is not None
            obj_id = pr
        else:
            raise ValueError("Invalid cls.obj_id: %s" % cls.obj_id)

        # This is pretty ugly
        process_name = ProcessName(cls.sync_type, status, obj_id)
        BranchRefObject.create(git_gecko, str(process_name), gecko_head)
        TagRefObject.create(git_gecko, str(process_name), gecko_base)

        if wpt_head is None:
            wpt_head = wpt_base

        BranchRefObject.create(git_wpt, str(process_name), wpt_head)
        TagRefObject.create(git_wpt, str(process_name), wpt_base)

        rv = cls(git_gecko, git_wpt, process_name)
        if pr and not rv.pr:
            rv.pr = pr
        if bug and not rv.bug:
            rv.bug = bug
        return rv

    @classmethod
    def load(cls, git_gecko, git_wpt, branch_name):
        process_name = ProcessName.from_ref(branch_name)
        if not process_name:
            return None
        return cls(git_gecko, git_wpt, process_name)

    @classmethod
    def load_all(cls, git_gecko, git_wpt, status="open", obj_id="*"):
        rv = []
        for branch_name in ProcessName.commit_refs(git_gecko,
                                                   ref_type="heads",
                                                   obj_type=cls.sync_type,
                                                   status=status,
                                                   obj_id=obj_id).itervalues():
            rv.append(cls.load(git_gecko, git_wpt, branch_name))
        return rv

    @property
    def branch_name(self):
        return str(self._process_name)

    @property
    def data(self):
        return self._data

    @property
    def status(self):
        return self._process_name.status

    @status.setter
    def status(self, value):
        self._process_name.status = value

    @property
    def bug(self):
        if self.obj_id == "bug":
            return self._process_name.obj_id
        else:
            return self.data.get("bug")

    @bug.setter
    def bug(self, value):
        if self.obj_id == "bug":
            raise AttributeError("Can't set attribute")
        self.data["bug"] = value

    @property
    def pr(self):
        if self.obj_id == "pr":
            return self._process_name.obj_id
        else:
            return self.data.get("pr")

    @pr.setter
    def pr(self, value):
        if self.obj_id == "pr":
            raise AttributeError("Can't set attribute")
        self.data["pr"] = value

    def gecko_rebase(self, new_base):
        new_base = sync_commit.GeckoCommit(self.repo, new_base)
        git_worktree = self.gecko_worktree.get()
        git_worktree.git.rebase(new_base.sha1)
        self.git_commits.base = new_base
