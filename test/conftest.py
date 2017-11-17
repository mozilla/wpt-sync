import os
import shutil
import subprocess
import types
from cStringIO import StringIO

import git
import pytest

from file import create_file_data

from sync import repos, settings
from sync.env import Environment, set_env

here = os.path.dirname(os.path.abspath(__file__))

root = os.path.join(here, "testdata")


# TODO: Probably don't need all of these to be function scoped
def cleanup(config):
    for name, dir in config["paths"].iteritems():
        if name == "logs":
            continue
        path = os.path.join(config["root"], dir)
        if os.path.exists(path):
            shutil.rmtree(path)


@pytest.fixture(scope="function")
def env():
    settings.root = root
    ini_sync = settings.read_ini(os.path.abspath(os.path.join(here, "test.ini")))
    ini_credentials = None
    config = settings.load_files(ini_sync, ini_credentials)
    cleanup(config)
    settings._config = config

    from sync import bug, gh

    gh_wpt = gh.MockGitHub()
    gh_wpt.output = StringIO()

    bz = bug.MockBugzilla(config)
    bz.output = StringIO()

    set_env(config, bz, gh_wpt)

    return Environment()


#Ensure that configuration is loaded
env()


@pytest.fixture
def initial_repo_content():
    return [("README", "Initial text\n")]


@pytest.fixture
def pr_content():
    branches_commits = [
        [("README", "More text, Initial text\n"), ("README", "Another line\nInitial text\n")],
        [("README", "Changed text\n")]
    ]
    return branches_commits


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
def hg_gecko_upstream(env, initial_repo_content):
    repo_dir = os.path.join(env.config["root"], env.config["sync"]["landing"])
    sync_dir = os.path.join(repo_dir, config["gecko"]["path"]["wpt"])
    meta_dir = os.path.join(repo_dir, config["gecko"]["path"]["meta"])

    os.makedirs(repo_dir)
    os.makedirs(sync_dir)
    os.makedirs(meta_dir)

    hg_gecko = hg(repo_dir)

    hg_gecko.init()

    for wpt_dir in [sync_dir, meta_dir]:
        for path, content in initial_repo_content:
            file_path = os.path.join(wpt_dir, path)
            with open(file_path, "w") as f:
                f.write(content)
            hg_gecko.add(os.path.relpath(file_path, repo_dir))

    hg_gecko.commit("-m", "Initial commit", "--user", "foo")
    hg_gecko.bookmark("mozilla/central")
    hg_gecko.bookmark("mozilla/autoland")
    hg_gecko.bookmark("mozilla/inbound")

    yield hg_gecko


@pytest.fixture(scope="function")
def git_wpt_upstream(initial_repo_content, pr_content):
    repo_dir = env.config["web-platform-tests"]["path"]
    os.makedirs(repo_dir)

    git_upstream = git.Repo.init(repo_dir)

    for path, content in initial_repo_content:
        file_path = os.path.join(repo_dir, path)
        with open(file_path, "w") as f:
            f.write(content)
        git_upstream.index.add([path])

    head = git_upstream.index.commit("Initial commit")

    count = 0
    for pr_id, commits in enumerate(pr_content):
        git_upstream.heads.master.checkout()
        pr_branch = git_upstream.create_head("pull/{}/head".format(pr_id))
        pr_branch.checkout()
        for path, content in commits:
            count += 1
            file_path = os.path.join(repo_dir, path)
            with open(file_path, "w") as f:
                f.write(content)
            git_upstream.index.add([path])
            git_upstream.index.commit("Commit {}".format(count))
    git_upstream.heads.master.checkout()

    return git_upstream


@pytest.fixture(scope="function")
def git_gecko(env, hg_gecko_upstream):
    git_gecko = repos.Gecko(env.config)
    git_gecko.configure()
    git_gecko = git_gecko.repo()
    git_gecko.remotes.mozilla.fetch()
    repo.last_processed_commit_id = (
        git_gecko.iter_commits(env.config["gecko"]["refs"]["central"]).next().hexsha)
    return git_gecko


@pytest.fixture(scope="function")
def git_wpt(git_wpt_upstream):
    git_wpt = repos.WebPlatformTests(env.config)
    git_wpt.configure()
    return git_wpt.repo()


@pytest.fixture
def upstream_wpt_commit(env, git_wpt_upstream, pull_request):
    def inner(title="Example change", file_data=None, pr_id=1):
        git_wpt_upstream.index.add(create_file_data(file_data, git_wpt_upstream.working_dir))
        commit = git_wpt_upstream.index.commit("Example change")

        if pr_id is not None:
            env.gh_wpt.commit_prs[commit.hexsha] = pr_id
            pull_request(pr_id, title, commits=[wpt_commit])
        return commit
    return inner


@pytest.fixture
def local_gecko_commit(env, git_gecko, pull_request):
    def inner(test_changes=None, meta_changes=None, pr_id=1, cls=None,
              bug=1234, title="Example changes", metadata_ready=False):
        if cls:
            sync = cls()
            sync.pr = pull_request(pr_id=pr_id)
            sync.bug = bug
            sync.metadata_ready = metadata_ready
        else:
            sync = None

        for path in create_file_data(test_changes, git_work.working_dir, config["gecko"]["path"]["wpt"]):
            git_work.git.add(path)
        git_work.git.commit(message=title)
        if meta_changes is not None:
            git_work.index.add(
                create_file_data(meta_changes, os.path.join(git_work.working_dir,
                                                            env.config["gecko"]["path"]["meta"])))
            git_work.commit("%s [metadata]" % title)
        return git_work, sync
    return inner


@pytest.fixture
def pull_request(gh_wpt):
    def inner(pr_id, title=None, commits=None):
        if created:
            pr.title = title if title is not None else "Example PR"
            if commits is not None:
                pr.commits = commits
            head = commits[0].rev if commits else None
            gh_wpt.create_pull(title, "Example pr", None, head)
        else:
            assert title is None and commits is None
        return pr

    inner.__name__ = "pull_request"
    return inner


@pytest.fixture
def mock_mach():
    from sync import projectutil
    log = []

    def get(self, *args, **kwargs):
        log.append({"command": self.name,
                    "cwd": self.path,
                    "args": args,
                    "kwargs": kwargs})
    projectutil.Mach.get = get
    return log


@pytest.fixture
def mock_wpt():
    from sync import projectutil
    log = []

    def get(self, *args, **kwargs):
        log.append({"command": self.name,
                    "cwd": self.path,
                    "args": args,
                    "kwargs": kwargs})
    projectutil.WPT.get = get
    return log


@pytest.fixture
def directory(request, env):
    created = []

    def make_dir(rel_path):
        path = os.path.join(env.config["root"], rel_path)
        os.makedirs(path)
        created.append(path)
        return path

    def fin():
        for path in created:
            shutil.rmtree(path)

    request.addfinalizer(fin)

    return make_dir
