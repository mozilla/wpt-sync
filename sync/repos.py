import json
import os
import shutil
import git
import pygit2

import log

logger = log.get_logger(__name__)


wrapper_map = {}
pygit2_map = {}


class GitSettings(object):
    name = None
    cinnabar = False

    def __init__(self, config):
        self.config = config

    @property
    def root(self):
        return os.path.join(self.config["repo_root"], self.config["paths"]["repos"], self.name)

    @property
    def remotes(self):
        return self.config[self.name]["repo"]["remote"].iteritems()

    def repo(self):
        repo = git.Repo(self.root)
        wrapper_map[repo] = self
        pygit2_map[repo] = pygit2.Repository(repo.git_dir)

        logger.debug("Existing repo found at " + self.root)

        if self.cinnabar:
            repo.cinnabar = Cinnabar(repo)

        self.setup(repo)

        return repo

    def setup(self, repo):
        pass

    def configure(self, file):
        if not os.path.exists(self.root):
            os.makedirs(self.root)
            git.Repo.init(self.root, bare=True)
        r = self.repo()
        shutil.copyfile(file, os.path.normpath(os.path.join(r.git_dir, "config")))
        logger.debug("Config from {} copied to {}".format(file, os.path.join(r.git_dir, "config")))


class Gecko(GitSettings):
    name = "gecko"
    cinnabar = True
    fetch_args = ["mozilla"]

    def setup(self, repo):
        data_ref = git.Reference(repo, self.config["sync"]["ref"])
        if not data_ref.is_valid():
            import base
            with base.CommitBuilder(repo, "Create initial sync metadata",
                                    ref=data_ref) as commit:
                path = "_metadata"
                data = json.dumps({"name": "wptsync"})
                commit.add_tree({path: data})
        import index
        for idx in index.indicies:
            idx.get_or_create(repo)


class WebPlatformTests(GitSettings):
    name = "web-platform-tests"
    fetch_args = ["origin", "master", "--no-tags"]


class WptMetadata(GitSettings):
    name = "wpt-metadata"
    fetch_args = ["origin", "master", "--no-tags"]


class Cinnabar(object):
    hg2git_cache = {}
    git2hg_cache = {}

    def __init__(self, repo):
        self.git = repo.git

    def hg2git(self, rev):
        if rev not in self.hg2git_cache:
            value = self.git.cinnabar("hg2git", rev)
            if all(c == "0" for c in value):
                raise ValueError("No git rev corresponding to hg rev %s" % rev)
            self.hg2git_cache[rev] = value
        return self.hg2git_cache[rev]

    def git2hg(self, rev):
        if rev not in self.git2hg_cache:
            value = self.git.cinnabar("git2hg", rev)
            if all(c == "0" for c in value):
                raise ValueError("No hg rev corresponding to git rev %s" % rev)
            self.git2hg_cache[rev] = value
        return self.git2hg_cache[rev]


wrappers = {
    "gecko": Gecko,
    "web-platform-tests": WebPlatformTests,
    "wpt-metadata": WptMetadata,
}


def pygit2_get(repo):
    if repo not in pygit2_map:
        pygit2_map[repo] = pygit2.Repository(repo.git_dir)
    return pygit2_map[repo]


def wrapper_get(repo):
    return wrapper_map.get(repo)
