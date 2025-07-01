import abc
import git
import json
import os
import subprocess
import shutil
from os import PathLike
from typing import Any, Optional, Mapping, Union

from git.repo.base import Repo
from git.objects.commit import Commit
from pygit2.repository import Repository

from . import log

logger = log.get_logger(__name__)


wrapper_map: dict[Repo, "GitSettings"] = {}
pygit2_map: dict[Repo, Repository] = {}
cinnabar_map: dict[Repo, "Cinnabar"] = {}


class GitSettings(metaclass=abc.ABCMeta):
    name: str = ""
    cinnabar = False

    def __init__(self, config: Mapping[str, Any]) -> None:
        self.config = config

    @property
    def root(self) -> str:
        return os.path.join(self.config["repo_root"], self.config["paths"]["repos"], self.name)

    @property
    def remotes(self) -> list[tuple[str, str]]:
        return list(self.config[self.name]["repo"]["remote"].items())

    def repo(self) -> Repo:
        repo = git.Repo(self.root)
        wrapper_map[repo] = self
        pygit2_map[repo] = Repository(str(repo.git_dir))

        logger.debug("Existing repo found at " + self.root)

        if self.cinnabar:
            cinnabar_map[repo] = Cinnabar(repo)

        self.setup(repo)

        return repo

    def setup(self, repo: Repo) -> None:
        pass

    def configure(self, config_path: str) -> None:
        if not os.path.exists(self.root):
            os.makedirs(self.root)
            git.Repo.init(self.root, bare=True)
        r = self.repo()
        shutil.copyfile(config_path, os.path.normpath(os.path.join(r.git_dir, "config")))
        logger.debug(
            "Config from {} copied to {}".format(config_path, os.path.join(r.git_dir, "config"))
        )

    def after_worktree_create(self, path: str) -> None:
        pass

    def after_worktree_delete(self, path: str) -> None:
        pass


class Gecko(GitSettings):
    name = "gecko"
    cinnabar = True
    fetch_args = ["mozilla"]

    def setup(self, repo: Repo) -> None:
        data_ref = git.Reference(repo, self.config["sync"]["ref"])
        if not data_ref.is_valid():
            from . import base

            with base.CommitBuilder(
                repo, "Create initial sync metadata", ref=data_ref.path
            ) as commit:
                path = "_metadata"
                data = json.dumps({"name": "wptsync"})
                commit.add_tree({path: data.encode("utf8")})
        from . import index

        for idx in index.indicies:
            idx.get_or_create(repo)

    @staticmethod
    def get_state_path(config: Mapping[str, Any], path: str | PathLike[str]) -> str:
        return os.path.join(
            config["root"], config["paths"]["state"], os.path.relpath(path, config["root"])
        )

    def after_worktree_create(self, path: str) -> None:
        from sync.projectutil import Mach

        state_path = self.get_state_path(self.config, path)
        if not os.path.exists(state_path):
            os.makedirs(state_path)
            mach = Mach(path)
            # create-mach-environment no longer exists, but keep trying
            # to run it in case we have an old tree
            try:
                mach.create_mach_environment()
            except subprocess.CalledProcessError:
                pass

    def after_worktree_delete(self, path: str) -> None:
        state_path = self.get_state_path(self.config, path)
        if os.path.exists(state_path):
            shutil.rmtree(state_path)


class WebPlatformTests(GitSettings):
    name = "web-platform-tests"
    fetch_args = ["origin", "master", "--no-tags"]


class WptMetadata(GitSettings):
    name = "wpt-metadata"
    fetch_args = ["origin", "master", "--no-tags"]


class Cinnabar:
    hg2git_cache: dict[str, str] = {}
    git2hg_cache: dict[str, str] = {}

    def __init__(self, repo: Repo) -> None:
        self.git = repo.git

    def hg2git(self, rev: str) -> str:
        if rev not in self.hg2git_cache:
            value = self.git.cinnabar("hg2git", rev)
            if all(c == "0" for c in value):
                raise ValueError("No git rev corresponding to hg rev %s" % rev)
            self.hg2git_cache[rev] = value
        return self.hg2git_cache[rev]

    def git2hg(self, rev: Union[str, Commit]) -> str:
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


def pygit2_get(repo: Repo) -> Repository:
    if repo not in pygit2_map:
        pygit2_map[repo] = Repository(str(repo.git_dir))
    return pygit2_map[repo]


def wrapper_get(repo: Repo) -> Optional[GitSettings]:
    return wrapper_map.get(repo)


def cinnabar(repo: Repo) -> Cinnabar:
    return cinnabar_map[repo]
