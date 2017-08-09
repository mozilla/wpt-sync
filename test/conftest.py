import os
import shutil
import subprocess
import sys
import types
from cStringIO import StringIO

import git
import pytest

from sync import bug, gh, model, repos, settings

here = os.path.dirname(os.path.abspath(__file__))

#TODO: Probably don't need all of these to be function scoped

def cleanup(config):
    for dir in config["paths"].itervalues():
        path = os.path.join(config["root"], dir)
        if os.path.exists(path):
            shutil.rmtree(path)


@pytest.fixture(scope="function")
def config():
    settings.root = here
    ini_sync = settings.read_ini(os.path.abspath(os.path.join(here, "test.ini")))
    ini_credentials = None
    config = settings.load_files(ini_sync, ini_credentials)
    cleanup(config)
    return config


@pytest.fixture(scope="function")
def session(config):
    model.configure(config)
    model.create()
    yield model.session()
    model.drop()


@pytest.fixture
def initial_repo_content():
    return [("README", "Initial text\n")]


class hg(object):
    def __init__(self, path):
        self.working_tree = path

    def __getattr__(self, name):
        def call(self, *args):
            cmd = ["hg", name] + list(args)
            print("%s, cwd=%s" % (" ".join(cmd), self.working_tree))
            return subprocess.check_output(cmd, cwd=self.working_tree)
        call.__name__ = name
        return types.MethodType(call, self, hg)


@pytest.fixture(scope="function")
def hg_gecko_upstream(config, initial_repo_content):
    repo_dir = os.path.join(config["root"], config["sync"]["landing"])
    sync_dir = os.path.join(repo_dir, config["gecko"]["path"]["wpt"])

    os.makedirs(repo_dir)
    os.makedirs(sync_dir)

    hg_gecko = hg(repo_dir)

    hg_gecko.init()

    for path, content in initial_repo_content:
        file_path = os.path.join(sync_dir, path)
        with open(file_path, "w") as f:
            f.write(content)
        hg_gecko.add(os.path.relpath(file_path, repo_dir))

    hg_gecko.commit("-m", "Initial commit")
    hg_gecko.bookmark("mozilla/central")
    hg_gecko.bookmark("mozilla/autoland")
    hg_gecko.bookmark("mozilla/inbound")

    yield hg_gecko


@pytest.fixture(scope="function")
def git_wpt_upstream(config, initial_repo_content):
    repo_dir = os.path.join(config["root"], config["web-platform-tests"]["path"])
    os.makedirs(repo_dir)

    git_upstream = git.Repo.init(repo_dir)

    for path, content in initial_repo_content:
        file_path = os.path.join(repo_dir, path)
        with open(file_path, "w") as f:
            f.write(content)
        git_upstream.index.add([path])

    git_upstream.index.commit("Initial commit")

    return git_upstream


@pytest.fixture(scope="function")
def git_gecko(config, session, hg_gecko_upstream):
    git_gecko = repos.Gecko(config).repo()
    git_gecko.remotes.mozilla.fetch()
    repo, _ = model.get_or_create(session, model.Repository, name="central")
    repo.last_processed_commit_id = (
        git_gecko.iter_commits(config["gecko"]["refs"]["central"]).next().hexsha)
    return git_gecko

@pytest.fixture(scope="function")
def git_wpt(config, git_wpt_upstream):
    return repos.WebPlatformTests(config).repo()


@pytest.fixture(scope="function")
def bz(config):
    bz = bug.MockBugzilla(config)
    bz.output = StringIO()
    return bz


@pytest.fixture(scope="function")
def gh_wpt():
    gh_wpt = gh.MockGitHub()
    gh_wpt.output = StringIO()
    return gh_wpt
