import os
import shutil
import subprocess
import types
from cStringIO import StringIO

import git
import pytest

from sync import repos, settings, bugcomponents, downstream, landing
from sync.env import Environment, set_env, clear_env


def create_file_data(file_data, repo_workdir, repo_prefix=None):
    add_paths = []
    del_paths = []
    for repo_path, contents in file_data.iteritems():
        if repo_prefix is not None:
            repo_path = os.path.join(repo_prefix, repo_path)
        if contents is None:
            del_paths.append(repo_path)
        else:
            path = os.path.join(repo_workdir, repo_path)
            add_paths.append(repo_path)
            dirname = os.path.dirname(path)
            if not os.path.exists(dirname):
                os.makedirs(dirname)
            with open(path, "w") as f:
                f.write(contents)
    return add_paths, del_paths


def git_commit(git, message="Example change", file_data=None):
    add_paths, del_paths = create_file_data(file_data, git.working_dir)
    if add_paths:
        git.index.add(add_paths)
    if del_paths:
        git.index.remove(del_paths, working_tree=True)
    return git.index.commit(message)


def gecko_changes(env, test_changes=None, meta_changes=None, other_changes=None):
    test_prefix = env.config["gecko"]["path"]["wpt"]
    meta_prefix = env.config["gecko"]["path"]["meta"]

    def prefix_paths(changes, prefix):
        if changes is not None:
            return {os.path.join(prefix, path) if not path.startswith(prefix) else path:
                    data for path, data in changes.iteritems()}

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
def env(mock_mach, mock_wpt):
    clear_env()
    config = settings.load()
    cleanup(config)

    from sync import bug, gh

    gh_wpt = gh.MockGitHub()
    gh_wpt.output = StringIO()

    bz = bug.MockBugzilla(config)
    bz.output = StringIO()

    bugcomponents.Mach = downstream.Mach = landing.Mach = mock_mach
    downstream.WPT = mock_wpt

    set_env(config, bz, gh_wpt)

    return Environment()


@pytest.fixture
def initial_gecko_content():
    return {"README": "Initial text\n"}


@pytest.fixture
def initial_wpt_content(env):
    return {"example/test.html": """<title>Example test</title>
<script src='/resources/testharness.js'></script>
<script src='/resources/testharnessreport.js'></script>
<script>
test(() => assert_true(true), "Passing test");
test(() => assert_true(false), "Failing test");
</script>
"""}


@pytest.fixture
def sample_gecko_metadata(env):
    # Only added in tests that require it
    return {os.path.join(env.config["gecko"]["path"]["meta"], "example/test.html.ini"):
            """
[test.html]
  [Failing test]
    expected: FAIL
"""}


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
def hg_gecko_upstream(env, initial_gecko_content, initial_wpt_content, git_wpt_upstream):
    repo_dir = os.path.join(env.config["root"], env.config["sync"]["landing"])
    sync_dir = os.path.join(repo_dir, env.config["gecko"]["path"]["wpt"])
    meta_dir = os.path.join(repo_dir, env.config["gecko"]["path"]["meta"])

    os.makedirs(repo_dir)
    os.makedirs(sync_dir)
    os.makedirs(meta_dir)

    hg_gecko = hg(repo_dir)
    hg_gecko.setup()

    paths, _ = create_file_data(initial_gecko_content, repo_dir)
    hg_gecko.add(*paths)
    hg_gecko.commit("-m", "Initial commit", "--user", "foo")

    local_rev = hg_gecko.log("-l1", "--template={node}")
    upstream_rev = git_wpt_upstream.commit("HEAD")

    content = "local: %s\nupstream: %s\n" % (local_rev, upstream_rev)

    wpt_paths, _ = create_file_data(initial_wpt_content, repo_dir,
                                    env.config["gecko"]["path"]["wpt"])
    meta_paths, _ = create_file_data({"mozilla-sync": content}, repo_dir,
                                     env.config["gecko"]["path"]["meta"])
    hg_gecko.add(*(wpt_paths + meta_paths))
    hg_gecko.commit("-m", "Initial wpt commit")

    hg_gecko.bookmark("mozilla/central")
    hg_gecko.bookmark("mozilla/autoland")
    hg_gecko.bookmark("mozilla/inbound")

    yield hg_gecko


@pytest.fixture(scope="function")
def hg_gecko_try(env, hg_gecko_upstream):
    hg_gecko_upstream_dir = os.path.join(env.config["root"], env.config["sync"]["landing"])
    repo_dir = os.path.join(env.config["root"], env.config["sync"]["try"])

    os.makedirs(repo_dir)

    hg_try = hg(repo_dir)
    hg_try.clone(hg_gecko_upstream_dir, repo_dir)

    yield hg_try


@pytest.fixture(scope="function")
def git_wpt_upstream(env, initial_wpt_content):
    repo_dir = env.config["web-platform-tests"]["path"]
    os.makedirs(repo_dir)

    git_upstream = git.Repo.init(repo_dir)
    paths, _ = create_file_data(initial_wpt_content, repo_dir)
    git_upstream.index.add(paths)

    git_upstream.index.commit("Initial wpt commit")

    return git_upstream


@pytest.fixture(scope="function")
def git_gecko(env, hg_gecko_upstream):
    git_gecko = repos.Gecko(env.config)
    git_gecko.configure()
    git_gecko = git_gecko.repo()
    git_gecko.remotes.mozilla.fetch()
    git_gecko.create_head("sync/upstream/inbound", "FETCH_HEAD")
    git_gecko.create_head("sync/upstream/central", "FETCH_HEAD")
    return git_gecko


@pytest.fixture(scope="function")
def git_wpt(env, git_wpt_upstream):
    git_wpt = repos.WebPlatformTests(env.config)
    git_wpt.configure()
    return git_wpt.repo()


@pytest.fixture
def upstream_wpt_commit(env, git_wpt_upstream, pull_request):
    def inner(message="Example change", file_data=None):
        commit = git_commit(git_wpt_upstream, message, file_data)
        return commit
    return inner


def hg_commit(hg, message, bookmarks):
    hg.commit("-m", message)
    rev = hg.log("-l1", "--template={node}")
    if isinstance(bookmarks, (str, unicode)):
        bookmarks = [bookmarks]
    for bookmark in bookmarks:
        hg.bookmark(bookmark)
    assert "+" not in hg.identify("--id")
    return rev


@pytest.fixture
def upstream_gecko_commit(env, hg_gecko_upstream):
    def inner(test_changes=None, meta_changes=None, other_changes=None,
              bug="1234", message="Example changes", bookmarks="mozilla/inbound"):
        changes = gecko_changes(env, test_changes, meta_changes, other_changes)
        message = "Bug %s - %s" % (bug, message)

        file_data, _ = create_file_data(changes, hg_gecko_upstream.working_tree)
        for path in file_data:
            hg_gecko_upstream.add(path)
        return hg_commit(hg_gecko_upstream, message, bookmarks)

    return inner


@pytest.fixture
def upstream_gecko_backout(env, hg_gecko_upstream):
    def inner(revs, bugs, message=None, bookmarks="mozilla/inbound"):
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
def gecko_worktree(env, git_gecko):
    path = os.path.join(env.config["root"],
                        env.config["paths"]["worktrees"],
                        "gecko"
                        "inbound")
    git_gecko.git.worktree("add",
                           path,
                           env.config["gecko"]["refs"]["mozilla-inbound"])
    return git.Repo(path)


@pytest.fixture
def local_gecko_commit(env, gecko_worktree):
    def inner(test_changes=None, meta_changes=None, other_changes=None,
              bug="1234", message="Example changes"):
        changes = gecko_changes(env, test_changes, meta_changes, other_changes)
        message = "Bug %s - %s" % (bug, message)

        return git_commit(gecko_worktree, message, changes)
    return inner


@pytest.fixture
def pull_request(env, git_wpt_upstream):
    def inner(commits, title="Example PR", body="", pr_id=None):

        git_wpt_upstream.heads.master.checkout()
        pr_branch = git_wpt_upstream.create_head("temp_pr")
        pr_branch.checkout()

        gh_commits = []

        for message, file_data in commits:
            rev = git_commit(git_wpt_upstream, message, file_data)
            gh_commits.append({"sha": rev.hexsha,
                               "message": message,
                               "_statuses": []})

        pr_id = env.gh_wpt.create_pull(title,
                                       body,
                                       "master",
                                       gh_commits[-1]["sha"],
                                       _commits=gh_commits)
        pr = env.gh_wpt.get_pull(pr_id)

        git_wpt_upstream.git.update_ref("refs/pull/%s/head" % pr_id, "refs/heads/temp_pr")
        git_wpt_upstream.heads.master.checkout()
        git_wpt_upstream.delete_head(pr_branch, force=True)

        return pr
    inner.__name__ = "pull_request"
    return inner


@pytest.fixture
def mock_mach():
    from sync import projectutil

    cls = projectutil.create_mock("mach")
    projectutil.Mach = cls
    return cls


@pytest.fixture(scope="function")
def mock_wpt():
    from sync import projectutil

    cls = projectutil.create_mock("wpt")
    projectutil.WPT = cls
    return cls


@pytest.fixture(scope="function")
def mock_try_push(git_gecko):
    from sync import trypush
    log = []

    def push(self):
        log.append("Pushing to try with message:\n{}".format(self.worktree.head.commit.message))
        return git_gecko.cinnabar.git2hg(self.worktree.commit("HEAD~").hexsha)

    trypush.TryCommit.push = push

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


@pytest.fixture
def set_pr_status(git_gecko, git_wpt):
    def inner(pr, status):
        from sync import load
        sync = load.get_pr_sync(git_gecko, git_wpt, pr["number"])
        downstream.status_changed(git_gecko, git_wpt, sync,
                                  "continuous-integration/travis-ci/pr",
                                  "success", "http://test/", pr["head"])
        return sync
    return inner
