import os
import re

import commit as sync_commit


invalid_re = re.compile(".*[-_]")


class WptSyncProcess(object):
    """Base class representing one of the sync processes.

    All the data for each process is stored in the Git trees.
    Each sync process is represented by a pair of refs, one in the gecko tree and
    one in the wpt tree. These have the form:

    refs/heads/sync/<type>/<status>/<data>

    <type> is a subclass-specific process type
    <status> is the current status of the sync process e.g. open, landed
    <data> is additional data relevant to the sync. It will typically include
           the gecko bug number and the upstream pr id, although these might not
           both be known from the start, so this data is mutable

    In general, in case of conflict the gecko repo data is considered canonical,
    and it may always be necessary to reconstruct the wpt repo data.
    """
    sync_type = None
    statuses = ("open")

    # Environment variables that get filled in
    config = None
    gh_wpt = None
    bz = None

    def __init__(self, git_gecko, git_wpt, branch_name):
        self.git_gecko = git_gecko
        self.git_wpt = git_wpt
        self._branch_name = branch_name

        self._wpt_commits = None
        self._gecko_commits = None

        self._worktrees = {}

    @property
    def branch_name(self):
        return self._branch_name

    @property
    def bug(self):
        return self.parse_branch(self._branch_name).get("bug")

    @bug.setter
    def bug(self, value):
        self._set_branch_data(bug=value)

    @property
    def pr(self):
        return self.parse_branch(self._branch_name).get("pr")

    @pr.setter
    def pr(self, value):
        self._set_branch_data(pr=value)

    @property
    def status(self):
        return self.parse_branch(self.branch_name).get("pr")

    @status.setter
    def status(self, value):
        self._set_branch_data(status=value)

    @classmethod
    def parse_branch(cls, name):
        if name.startswith("refs/heads/"):
            name = name[len("refs/heads"):]
        parts = name.split("/")
        if not len(parts) == 4:
            return None
        if not parts[0] == "sync":
            return None
        if not parts[1] == cls.sync_type:
            return None
        status = parts[2]
        if status not in cls.statuses:
            raise ValueError("Invalid status %s" % status)
        data = parts[3]
        rv = {key: value for item in data.split("_") for (key, value) in item.split("-")}
        if "status" in rv:
            raise ValueError
        rv["status"] = status
        return rv

    @classmethod
    def _construct_branch_name(cls, status, data):
        if status not in cls.statuses:
            raise ValueError("Invalid status %s" % status)
        data_str = "_".join("%s-%s" % (key, value) for key, value in sorted(data.items()))
        return "sync/%s/%s/%s" % (cls.sync_type,
                                  status,
                                  data_str)

    def _set_branch_data(self, **kwargs):
        current_branch_name = self._branch_name
        data = self.parse_branch(current_branch_name)
        updated = any(key not in data or data[key] != value
                      for key, value in kwargs.iteritems())

        for key, value in data.iteritems():
            if invalid_re.match(key):
                raise ValueError(key)

            if invalid_re.match(value):
                raise ValueError(value)

        if updated:
            data.update(kwargs)
            status = data.pop("status")
            branch_name = self._construct_branch_name(status, **data)

            for repo in [self.git_gecko, self.git_wpt]:
                branch = repo.branches.get(current_branch_name)
                if branch:
                    branch.rename(branch_name)
            self._branch_name = branch_name
        return branch_name

    @classmethod
    def commit_branches(cls, repo, status="*"):
        branch_filter = "sync/%s/%s/*" % (cls.sync_type,
                                          status)
        sync_branches = repo.for_each_ref("--format", "%(objectname) %(refname:short)",
                                          branch_filter)

        commit_branches = {}
        for line in sync_branches.split("\n"):
            commit, branch = line.split(" ", 1)
            commit_branches[commit] = branch
        return commit_branches

    @classmethod
    def new(cls, git_gecko, git_wpt, bug=None, pr=None,
            gecko_start_point=None, wpt_start_point=None,
            status="open"):
        if bug is None and pr is None:
            raise ValueError("Must provide a bug number or a PR number")
        if gecko_start_point is None and wpt_start_point is None:
            raise ValueError("Must provide an initial commit in some repository")

        data = {}
        if bug is not None:
            data["bug"] = bug
        if pr is not None:
            data["pr"] = pr
        branch_name = cls._construct_branch_name(status, data)

        if gecko_start_point is not None:
            git_gecko.create_head(branch_name, commit=gecko_start_point)

        if wpt_start_point is not None:
            git_wpt.create_head(branch_name, commit=gecko_start_point)

        return cls()

    @property
    def gecko_head(self):
        return self._repo_head(self.git_gecko, sync_commit.GeckoCommit)

    @gecko_head.setter
    def gecko_head(self, value):
        self.git_wpt.create_head(self.branch_name, ref=value, force=True)

    @property
    def wpt_head(self):
        return self._repo_head(self.git_wpt, sync_commit.WptCommit)

    @wpt_head.setter
    def wpt_head(self, value):
        self.git_wpt.create_head(self.branch_name, ref=value, force=True)

    def _repo_head(self, repo, commit_cls=sync_commit.Commit):
        if self.branch_name not in repo.branches:
            return None
        return commit_cls(repo, repo.Commit(self.branch_name).hexsha)

    @classmethod
    def load(cls, git_gecko, git_wpt, status="open"):
        # TODO: this should allow the repo to be set
        rv =  []
        for branch in cls.commit_branches(git_gecko, status=status).itervalues():
            rv.append(cls(git_gecko, git_wpt, status))
        return rv

    def _worktree(self, repo):
        if self._worktrees.get(repo) is None:
            git = {"web-platform-tests": self.git_wpt,
                   "gecko": self.git_gecko}
            worktree_path = os.path.join(self.config["root"],
                                         self.config["paths"]["worktrees"],
                                         repo,
                                         self.branch_name.rsplit("_", 1)[0])
            if os.path.exists(worktree_path):
                worktree = git.Repo(worktree_path)
                worktree.index.reset(self.branch_name, working_tree=True)
            else:
                worktree = git.worktree("add",
                                        os.path.abspath(worktree_path),
                                        self.branch_name)
            self._worktrees[repo] = worktree
        return self._worktrees[repo]

    def wpt_worktree(self):
        return self._worktree("web-platform-tests")

    def gecko_worktree(self):
        return self._worktree("gecko")

    def check_finished(self):
        raise NotImplementedError

    @classmethod
    def gecko_integration_branch(cls):
        cls.config["refs"]["mozilla-inbound"]

    @property
    def gecko_base(self):
        return sync_commit.GeckoCommit(self.git_gecko,
                                       self.gecko_integration_branch())

    @property
    def wpt_base(self):
        return sync_commit.WptCommit(self.git_wpt, "origin/master")

    def validate_wpt_commit(self):
        return True, None

    def validate_gecko_commit(self):
        return True, None

    def _load_commits(self, repo, cls, base, head, validate_function=None, reverse=False):
        rv = []
        if head is None or base is None:
            return rv

        for commit in repo.iter_commits("%s..%s" % (base.sha1, head.sha1), reverse=reverse):
            c = cls(repo, commit)
            valid, err = validate_function(c) if validate_function is not None else (True, None)
            if valid:
                rv.append(c)
            else:
                raise ValueError(err)
            return rv

    def wpt_commits(self):
        """Load the commits between two PRs into the database, along with their associated PRs
        """
        if self._wpt_commits is None:
            self._wpt_commits = self._load_commits(self.git_wpt,
                                                   sync_commit.WptCommit,
                                                   self.wpt_base(),
                                                   self.wpt_head,
                                                   self.validate_wpt_commit)
        return self._wpt_commits

    def gecko_commits(self):
        """Load the commits between two PRs into the database, along with their associated PRs
        """
        if self._gecko_commits is None:
            self._gecko_commits = self._load_commits(self.git_gecko,
                                                     sync_commit.GeckoCommit,
                                                     self.gecko_base(),
                                                     self.gecko_head,
                                                     self.validate_gecko_commit)
        return self._gecko_commits
