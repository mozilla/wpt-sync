import os
from git import Repo

import log

logger = log.get_logger(__name__)


class GitSettings(object):
    name = None
    bare = True
    cinnabar = False

    def __init__(self, config):
        self.config = config

    @property
    def root(self):
        return os.path.join(self.config["root"], self.config["paths"]["repos"], self.name)

    @property
    def remotes(self):
        return self.config[self.name]["repo"]["remote"].iteritems()

    def repo(self):
        if not os.path.exists(self.root):
            os.makedirs(self.root)
            repo = Repo.init(self.root, bare=True)
        else:
            repo = Repo(self.root)

        if self.cinnabar:
            repo.cinnabar = Cinnabar(repo)

        return repo

    def configure(self):
        repo = self.repo()
        for name, url in self.remotes:
            try:
                remote = repo.remote(name=name)
            except ValueError:
                remote = repo.create_remote(name, url)
            else:
                current_urls = list(remote.urls)
                if len(current_urls) > 1:
                    for old_url in current_urls[1:]:
                        remote.delete_url(old_url)
                remote.set_url(url, current_urls[0])

            with repo.config_writer() as config_writer:
                for key in self.config[self.name]["remote"][name].keys():
                    config_writer.set_value(
                        "remote \"%s\"" % name,
                        key,
                        self.config[self.name]["remote"][name][key])

                if self.cinnabar:
                    config_writer.set_value("fetch",
                                            "prune",
                                            "true")


class Gecko(GitSettings):
    name = "gecko"
    cinnabar = True
    fetch_args = ["mozilla"]

    def configure(self):
        super(Gecko, self).configure()
        git = self.repo().git
        git.config("remote.try.push", "+HEAD:refs/heads/branches/default/tip", add=True)


class WebPlatformTests(GitSettings):
    name = "web-platform-tests"
    fetch_args = ["origin", "master", "--no-tags"]

    def configure(self):
        super(WebPlatformTests, self).configure()
        git = self.repo().git
        git.config("remote.origin.fetch", unset_all=True)
        git.config("remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*", add=True)
        git.config("remote.origin.fetch", "+refs/pull/*/head:refs/remotes/origin/pr/*", add=True)


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

    def fsck(self):
        self.git.cinnabar("fsck")


def configure(config):
    for repo in [WebPlatformTests, Gecko]:
        repo = repo(config)
        repo.configure()
        repo.repo()


wrappers = {
    "gecko": Gecko,
    "web-platform-tests": WebPlatformTests,
}
