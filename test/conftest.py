import os
import shutil
import subprocess
import types
from cStringIO import StringIO

import git
import pytest

from sync import repos, settings
from sync.env import Environment, set_env

here = os.path.dirname(os.path.abspath(__file__))

root = os.path.join(here, "testdata")


def create_file_data(file_data, repo_workdir):
    paths = []
    for repo_path, contents in file_data.iteritems():
        path = os.path.join(repo_workdir, repo_path)
        paths.append(repo_path)
        with open(path, "w") as f:
            f.write(contents)
    return paths


def git_commit(git, message="Example change", file_data=None, metadata=None):
    git.index.add(create_file_data(file_data, git.working_dir))
    return git_wpt_upstream.index.commit(message)


def gecko_changes(env, test_changes=None, meta_changes=None, other_changes=None):
    test_prefix = env.config["gecko"]["path"]["wpt"]
    meta_prefix = env.config["gecko"]["path"]["meta"]

    def prefix_paths(changes, prefix):
        if changes is not None:
            return {os.path.join(prefix, path): data for path, data in changes.iteritems()}

    test_changes = prefix_paths(test_changes, test_prefix)
    meta_changes = prefix_paths(meta_changes, meta_prefix)

    changes = {}
    for item in [test_changes, meta_changes, other_changes]:
        if item is not None:
            changes.update(item)
    return changes


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

    def setup(self):
        self.init()
        with open(os.path.join(self.working_tree, ".hg", "hgrc"), "w") as f:
            f.write("""[ui]
username=test""")

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
    sync_dir = os.path.join(repo_dir, env.config["gecko"]["path"]["wpt"])
    meta_dir = os.path.join(repo_dir, env.config["gecko"]["path"]["meta"])

    os.makedirs(repo_dir)
    os.makedirs(sync_dir)
    os.makedirs(meta_dir)

    hg_gecko = hg(repo_dir)
    hg_gecko.setup()

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
def git_wpt_upstream(env, initial_repo_content, pr_content):
    repo_dir = env.config["web-platform-tests"]["path"]
    os.makedirs(repo_dir)

    git_upstream = git.Repo.init(repo_dir)

    for path, content in initial_repo_content:
        file_path = os.path.join(repo_dir, path)
        with open(file_path, "w") as f:
            f.write(content)
        git_upstream.index.add([path])

    git_upstream.index.commit("Initial commit")

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
    return git_gecko


@pytest.fixture(scope="function")
def git_wpt(env, git_wpt_upstream):
    git_wpt = repos.WebPlatformTests(env.config)
    git_wpt.configure()
    return git_wpt.repo()


@pytest.fixture
def upstream_wpt_commit(env, git_wpt_upstream, git_commit, pull_request):
    def inner(message="Example change", file_data=None, pr_id=1):
        commit = git_commit(git_wpt_upstream, message, file_data)
        if pr_id is not None:
            env.gh_wpt.commit_prs[commit.hexsha] = pr_id
            pull_request(pr_id, message, commits=[commit])
        return commit
    return inner


def hg_commit(hg, message, bookmarks):
    hg.commit("-m", message)
    rev = hg.log("-l1", "--template={node}")
    if isinstance(bookmarks, (str, unicode)):
        bookmarks = [bookmarks]
    for bookmark in bookmarks:
        hg.bookmark(bookmark)
    return rev


@pytest.fixture
def upstream_gecko_commit(env, hg_gecko_upstream):
    def inner(test_changes=None, meta_changes=None, other_changes=None,
              bug="1234", message="Example changes", bookmarks="inbound"):
        changes = gecko_changes(env, test_changes, meta_changes, other_changes)
        message = "Bug %s - %s" % (bug, message)

        file_data = create_file_data(changes, hg_gecko_upstream.working_tree)
        for path in file_data:
            hg_gecko_upstream.add(path)
        return hg_commit(hg_gecko_upstream, message, bookmarks)

    return inner


@pytest.fixture
def upstream_gecko_backout(env, hg_gecko_upstream):
    def inner(revs, bugs, message=None, bookmarks="inbound"):
        if isinstance(revs, (str, unicode)):
            revs = [revs]
        if isinstance(bugs, (str, unicode)):
            bugs = [bugs] * len(revs)
        assert len(bugs) == len(revs)
        msg = ["Backed out %i changesets (bug %s) for test, r=backout" % (len(revs), bugs[0]), ""]
        for rev, bug in zip(revs, bugs):
            hg_gecko_upstream.backout("--no-commit", rev)
            msg.append("Backed out changeset %s (Bug %s)" % (rev[:12], bug))
        if message is None:
            message = "\n".join(msg)
        return hg_commit(hg_gecko_upstream, message, bookmarks)
    return inner


@pytest.fixture
def local_gecko_commit(env, git_commit, pull_request):
    def inner(git_gecko, test_changes=None, meta_changes=None, other_changes=None,
              bug="1234", message="Example changes"):
        changes = gecko_changes(env, test_changes, meta_changes, other_changes)
        message = "Bug %s - %s" % (bug, message)

        return git_commit(git_gecko, changes, message)
    return inner


@pytest.fixture
def pull_request(env):
    def inner(pr_id, title=None, head=None, commits=None):
        if pr_id not in env.gh.prs:
            env.gh.create_pull(title, "Example pr", None, head, _commits=commits)
        pr = env.gh.get_pull(pr_id)
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
