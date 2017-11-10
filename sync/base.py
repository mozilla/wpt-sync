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
        self._obj_id = obj_id
        self._seq_id = seq_id
        self._refs = []

    def __str__(self):
        return self.name(self._obj_type.
                         self._status,
                         self._obj_id,
                         self._seq_id)

    @staticmethod
    def name(obj_type, status, obj_id, seq_id=None):
        rv = "sync/%s/%s/%s" % (obj_type.
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
        return cls(*parts)

    @classmethod
    def commit_refs(cls, repo, obj_type, status="open", obj_id="*", seq_id=None):
        branch_filter = "sync/%s/%s/%s" % (obj_type, status, obj_id)
        if seq_id is not None:
            branch_filter = "%s/%s" % (branch_filter, seq_id)
        commits_refs = repo.for_each_ref("--format", "%(objectname) %(refname:short)",
                                         branch_filter)

        commit_refs = {}
        for line in commits_refs.split("\n"):
            commit, ref = line.split(" ", 1)
            commit_refs[commit] = ref
        return commit_refs


class SyncPointName(object):
    """Like a process name but for pointers that aren't associated with a
    specific sync object, but with a general process"""
    def __init__(self, obj_type, obj_id):
        self._obj_type = obj_type
        self._obj_id = obj_id
        self._refs = []

    def __str__(self):
        return self.name(self._obj_type.
                         self._obj_id)

    @staticmethod
    def name(obj_type, status, obj_id):
        return "sync/%s/%s" % (obj_type.
                               obj_id)


class VcsRefObject(object):
    ref_prefix = None

    def __init__(self, repo, process_name, commit_cls=sync_commit.Commit):
        self.repo = repo
        self._process_name = process_name.attach_ref(self)

    def __str__(self):
        return "%s/%s" % (self.ref_prefix, str(self._process_name))

    @classmethod
    def name(cls, obj_type, status, obj_id):
        return "%s/%s" (cls.ref_prefix,
                        ProcessName.name(obj_type, status, id))

    @property
    def ref(self):
        return self.repo.refs.get(str(self))

    @property
    def commit(self):
        ref = self.ref
        if ref is not None:
            commit = self.commit_cls(self.repo, ref.commit)
        return commit

    @classmethod
    def load_all(cls, repo, status="open", obj_id="*", seq_id=None,
                 commit_cls=sync_commit.Commit):
        rv = []
        for ref_name in sorted(ProcessName.commit_refs(repo,
                                                       status=status,
                                                       obj_id=obj_id,
                                                       seq_id=None).itervalues()):
            rv.append(cls(repo, ProcessName.from_ref(ref_name), commit_cls))
        return rv


class BranchRefObject(VcsRefObject):
    ref_prefix = "heads"

    @classmethod
    def create(cls, repo, ref, commit):
        repo.create_head(ref, commit)


class TagRefObject(VcsRefObject):
    ref_prefix = "tags"

    def __init__(self, repo, process_name, commit_cls=sync_commit.Commit):
        VcsRefObject.__init__(repo, process_name, commit_cls=sync_commit.Commit)
        self._data = None

    @classmethod
    def create(cls, repo, ref, commit, message="{}"):
        repo.create_tag(ref, commit, message=message)

    @property
    def data(self):
        if self._data is None:
            self._data = ProcessData(self.ref.tag)
        return self._data

    def update_message(self, message):
        assert self.ref is not None
        commit = self.commit.sha1
        self.repo.create_tag(str(self), commit, message=message, force=True)


class ProcessData(object):
    def __init__(self, tag_object):
        self._tag = tag_object
        self._data = self._load()

    def _load(self):
        tag = self._tag.ref
        if tag:
            data = tag.tag.message
            return json.loads(data)
        return {}

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
                                     process_name.sync_type,
                                     process_name.status,
                                     process_name.obj_id,
                                     commit_cls=commit_cls)
        self.head_ref = BranchRefObject(repo,
                                        process_name.sync_type,
                                        process_name.status,
                                        process_name.obj_id,
                                        commit_cls=commit_cls)
        self.commit_cls = commit_cls

        # Cache for the commits in this range
        self._commits = None
        self._head_sha = None

    @property
    def base(self):
        return self.base_ref.commit

    @base.setter
    def base(self, commit):
        self.head_ref.ref = commit.sha1

    @property
    def head(self):
        return self.head_ref.commit

    @head.setter
    def head(self, commit):
        self.head_ref.ref = commit.sha1

    def __getitem__(self, index):
        return self.commits()[index]

    def __iter__(self):
        for item in self.commits():
            yield item

    def __len__(self):
        return len(self.commits())

    @property
    def commits(self):
        if self._head_sha and self.head.sha1 == self._head_sha and self._commits:
            return self._commits
        revish = "%s..%s" % (self.base.sha1, self.head.sha1)
        commits = []
        for commit in self.repo.iter_commits(revish,
                                             reverse=True,
                                             paths=self.commit_filter.path_filter()):
            if not self.commit_filter.filter_commit(commit):
                continue
            commits.append(commit)
        commits = self.commit_filter.filter_commits(commits)
        self._commits = commits
        self._head_sha = self.head.sha1
        return self._commits


class Worktree(object):
    def __init__(self, repo, process_name):
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
                worktree = self.repo.git.worktree("add",
                                                  os.path.abspath(self.path),
                                                  str(self.process_name))
            self._worktree = worktree
        assert self._worktree.active_branch == str(self.process_name)
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

    gecko_integration_branch = env.config["gecko"]["refs"]["inbound"]

    def __init__(self, git_gecko, git_wpt, process_name):
        self.git_gecko = git_gecko
        self.git_wpt = git_wpt

        self._process_name = process_name

        self.gecko_commits = CommitRange(git_gecko, self._process_name,
                                         sync_commit.GeckoCommit,
                                         commit_filter=self.gecko_commit_filter())
        self.wpt_commits = CommitRange(git_wpt, self._process_name,
                                       sync_commit.WptCommit,
                                       commit_filter=self.wpt_commit_filter())

        self.gecko_worktree = Worktree(git_gecko,
                                       self._process_name)
        self.wpt_worktree = Worktree(git_wpt,
                                     self._process_name)

        self._data = self.gecko_commits.base.data

    def gecko_commit_filter(self):
        return CommitFilter()

    def wpt_commit_filter(self):
        return CommitFilter()

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
            raise ValueError("Invalid cls.obj_id")

        # This is pretty ugly
        process_name = ProcessName(cls.sync_type, status, obj_id)
        BranchRefObject.create(git_gecko, str(process_name), gecko_head)
        TagRefObject.create(git_gecko, str(process_name), gecko_base)

        if wpt_head:
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
        parts = ProcessName.parse_ref(branch_name)
        if not parts:
            return None
        return cls(git_gecko, git_wpt, parts[2:])

    @classmethod
    def commit_refs(cls, repo, status="open", obj_id="*"):
        return ProcessName.commit_refs(repo,
                                       cls.sync_type,
                                       status,
                                       obj_id)

    @classmethod
    def load_all(cls, git_gecko, git_wpt, status="open", obj_id="*"):
        rv = []
        for branch_name in cls.commit_refs(git_gecko,
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
        if self.obj_type == "bug":
            return self.obj_id
        else:
            return self.data.get("bug")

    @bug.setter
    def bug(self, value):
        if self.obj_id == "bug":
            raise AttributeError("Can't set attribute")
        self.data["bug"] = value

    @property
    def pr(self):
        if self.obj_type == "pr":
            return self.obj_id
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
