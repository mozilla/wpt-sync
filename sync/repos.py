import os
from git import Repo


class GitSettings(object):
    name = None
    bare = True
    cinnabar = False

    def __init__(self, config):
        self.config = config

    @property
    def root(self):
        return os.path.join(self.config["paths"]["repos"], self.name)

    @property
    def remotes(self):
        return self.config[self.name]["repo"]["remote"].iteritems()

    def repo(self):
        if not os.path.exists(self.root):
            os.makedirs(self.root)
            repo = Repo.init(self.root, bare=True)
        else:
            repo = Repo(self.root)

        for name, url in self.remotes:
            try:
                remote = repo.remote(name=name)
            except ValueError:
                remote = repo.create_remote(name, url)
            else:
                current_urls = list(remote.urls)
                print name, current_urls, url
                if len(current_urls) > 1:
                    for old_url in current_urls[1:]:
                        remote.delete_url(old_url)
                remote.set_url(url, current_urls[0])

        if self.cinnabar:
            repo.git.config("fetch.prune", "true")
            repo.cinnabar = Cinnabar(repo)

        return repo


class Gecko(GitSettings):
    name = "gecko"
    cinnabar = True


class WebPlatformTests(GitSettings):
    name = "web-platform-tests"


class Cinnabar(object):
    hg2git_cache = {}
    git2hg_cache = {}

    def __init__(self, repo):
        self.git = repo.git

    def hg2git(self, rev):
        if rev not in self.hg2git_cache:
            value = self.git.cinnabar("hg2git", rev)
            if all(c == "0" for c in value):
                raise ValueError
            self.hg2git_cache[rev] = value
        return self.hg2git_cache[rev]

    def git2hg(self, rev):
        if rev not in self.git2hg_cache:
            value = self.git.cinnabar("git2hg", rev)
            if all(c == "0" for c in value):
                raise ValueError
            self.git2hg_cache[rev] = value
        return self.git2hg_cache[rev]

    def fsck(self):
        self.git.cinnabar("fsck")


def configure():
    for settings in [Gecko, WebPlatformTests]:
        repo = settings(config).repo()
        for remote in repo.remotes:
            remote.fetch()


if __name__ == "__main__":
    configure()
