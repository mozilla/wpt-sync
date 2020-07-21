from __future__ import absolute_import
import abc
import json
import os
import shutil
import git
import pygit2
import six
from six import iteritems

from . import log

MYPY = False
if MYPY:
    from typing import Any, Dict, Text, Optional, Union
    from git.repo.base import Repo
    from git.objects.commit import Commit
    from pygit2.repository import Repository


logger = log.get_logger(__name__)


wrapper_map = {}
pygit2_map = {}


class GitSettings(six.with_metaclass(abc.ABCMeta, object)):

    name = None  # type: Text
    cinnabar = False

    def __init__(self, config):
        # type: (Dict[Text, Any]) -> None
        self.config = config

    @property
    def root(self):
        # type: () -> Text
        return os.path.join(self.config["repo_root"],
                            self.config["paths"]["repos"],
                            self.name)

    @property
    def remotes(self):
        return iteritems(self.config[self.name]["repo"]["remote"])

    def repo(self):
        # type: () -> Repo
        repo = git.Repo(self.root)
        wrapper_map[repo] = self
        pygit2_map[repo] = pygit2.Repository(repo.git_dir)

        logger.debug("Existing repo found at " + self.root)

        if self.cinnabar:
            repo.cinnabar = Cinnabar(repo)

        self.setup(repo)

        return repo

    def setup(self, repo):
        # type: (Repo) -> None
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
            from . import base
            with base.CommitBuilder(repo, "Create initial sync metadata",
                                    ref=data_ref) as commit:
                path = "_metadata"
                data = json.dumps({"name": "wptsync"})
                commit.add_tree({path: data})
        from . import index
        for idx in index.indicies:
            idx.get_or_create(repo)


class WebPlatformTests(GitSettings):
    name = "web-platform-tests"
    fetch_args = ["origin", "master", "--no-tags"]


class WptMetadata(GitSettings):
    name = "wpt-metadata"
    fetch_args = ["origin", "master", "--no-tags"]


class Cinnabar(object):
    hg2git_cache = {}  # type: Dict[Text, Text]
    git2hg_cache = {}  # type: Dict[Text, Text]

    def __init__(self, repo):
        self.git = repo.git

    def hg2git(self, rev):
        # type: (Text) -> Text
        if rev not in self.hg2git_cache:
            value = self.git.cinnabar("hg2git", rev)
            if all(c == "0" for c in value):
                raise ValueError("No git rev corresponding to hg rev %s" % rev)
            self.hg2git_cache[rev] = value
        return self.hg2git_cache[rev]

    def git2hg(self, rev):
        # type: (Union[Text, Commit]) -> Text
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
    # type: (Repo) -> Repository
    if repo not in pygit2_map:
        pygit2_map[repo] = pygit2.Repository(repo.git_dir)
    return pygit2_map[repo]


def wrapper_get(repo):
    # type: (Repo) -> Optional[GitSettings]
    return wrapper_map.get(repo)
