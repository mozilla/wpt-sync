import os
import shutil
import subprocess
import sys

import git
import pytest

from sync import bug, gh, model, repos, settings

here = os.path.dirname(os.path.abspath(__file__))


def cleanup(config):
    for dir in config["paths"].itervalues():
        path = os.path.join(config["root"], dir)
        if os.path.exists(path):
            shutil.rmtree(path)


@pytest.fixture
def config():
    settings.root = here
    ini_sync = settings.read_ini(os.path.abspath(os.path.join(here, "test.ini")))
    ini_credentials = None
    config = settings.load_files(ini_sync, ini_credentials)
    cleanup(config)
    return config


@pytest.fixture
def session(config):
    model.configure(config)
    return model.session()


@pytest.fixture
def initial_repo_content():
    return [("README", "Initial text\n")]


def hg(repo):
    def inner(*args):
        return subprocess.check_call(["hg"] + list(args), cwd=repo)
    inner.__name__ = hg.__name__
    return inner


@pytest.fixture
def gecko_upstream_hg(config, initial_repo_content):
    repo_dir = os.path.join(config["root"], config["sync"]["landing"])
    sync_dir = os.path.join(repo_dir, config["gecko"]["path"]["wpt"])
    os.makedirs(repo_dir)
    os.makedirs(sync_dir)

    hg_gecko = hg(repo_dir)

    hg_gecko("init")

    for path, content in initial_repo_content:
        file_path = os.path.join(sync_dir, path)
        with open(file_path, "w") as f:
            f.write(content)
        hg_gecko("add", os.path.relpath(file_path, repo_dir))

    hg_gecko("commit", "-m", "Initial commit")
    hg_gecko("bookmark", "mozilla/central")
    hg_gecko("bookmark", "mozilla/autoland")
    hg_gecko("bookmark", "mozilla/inbound")

    return hg_gecko


@pytest.fixture
def wpt_upstream(config, initial_repo_content):
    repo_dir = os.path.join(config["root"], config["web-platform-tests"]["repo"]["url"])
    os.makedirs(repo_dir)

    git_upstream = git.Repo.init(repo_dir)

    for path, content in initial_repo_content:
        file_path = os.path.join(repo_dir, path)
        with open(file_path, "w") as f:
            f.write(content)
        git_upstream.index.add([path])

    git_upstream.index.commit("Initial commit")

    return git_upstream


@pytest.fixture
def git_gecko(config, gecko_upstream_hg):
    return repos.Gecko(config).repo()


@pytest.fixture
def git_wpt(config, wpt_upstream):
    return repos.WebPlatformTests(config).repo()


@pytest.fixture
def bz(config):
    return bug.MockBugzilla(config)


@pytest.fixture
def gh_wpt():
    return gh.MockGitHub()
