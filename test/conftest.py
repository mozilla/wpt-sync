import copy
import glob
import io
import json
import os
import random
import shutil
import subprocess
import types
from collections import defaultdict
from io import StringIO
from unittest.mock import Mock, patch

import git
import pytest
import requests_mock

from sync import (
    commit,
    repos,
    settings,
    bugcomponents,
    base,
    downstream,
    landing,
    trypush,
    tree,
    tc,
)
from sync.env import Environment, set_env, clear_env
from sync.gh import AttrDict
from sync.lock import SyncLock

here = os.path.abspath(os.path.dirname(__file__))


def create_file_data(file_data, repo_workdir, repo_prefix=None):
    add_paths = []
    del_paths = []
    for repo_path, contents in file_data.items():
        assert contents is None or isinstance(contents, bytes)
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
            with open(path, "wb") as f:
                f.write(contents)
    return add_paths, del_paths


def git_commit(git, message=b"Example change", file_data=None):
    assert isinstance(message, bytes)
    add_paths, del_paths = create_file_data(file_data, git.working_dir)
    if add_paths:
        git.git.add(add_paths)
    if del_paths:
        git.git.rm(del_paths)
    commit.create_commit(git, message)
    return git.head.commit


def gecko_changes(env, test_changes=None, meta_changes=None, other_changes=None):
    test_prefix = env.config["gecko"]["path"]["wpt"]
    meta_prefix = env.config["gecko"]["path"]["meta"]

    def prefix_paths(changes, prefix):
        if changes is not None:
            return {
                os.path.join(prefix, path) if not path.startswith(prefix) else path: data
                for path, data in changes.items()
            }

    test_changes = prefix_paths(test_changes, test_prefix)
    meta_changes = prefix_paths(meta_changes, meta_prefix)

    changes = {}
    for item in [test_changes, meta_changes, other_changes]:
        if item is not None:
            changes.update(item)
    return changes


# TODO: Probably don't need all of these to be function scoped
def cleanup(config):
    for name, dir in config["paths"].items():
        if name == "logs":
            continue
        path = os.path.join(config["root"], dir)
        if os.path.exists(path):
            shutil.rmtree(path)


@pytest.fixture(scope="function")
def env(request, mock_mach, mock_wpt):
    assert os.environ.get("WPTSYNC_CONFIG") == "/app/config/test/sync.ini"
    assert os.environ.get("WPTSYNC_CREDS") == "/app/config/test/credentials.ini"
    clear_env()
    config = settings.load()
    cleanup(config)

    from sync import bug, gh

    gh_wpt = gh.MockGitHub()

    bz = bug.MockBugzilla(config)
    bz.output = StringIO()

    bugcomponents.Mach = downstream.Mach = landing.Mach = mock_mach
    downstream.WPT = mock_wpt

    set_env(config, bz, gh_wpt)

    for name, dir in config["paths"].items():
        path = os.path.join(config["root"], dir)
        if not os.path.exists(path):
            os.makedirs(path)

    def empty_caches():
        base.IdentityMap._cache.clear()

    request.addfinalizer(empty_caches)

    return Environment()


@pytest.fixture
def initial_gecko_content():
    return {"README": b"Initial text\n"}


@pytest.fixture
def initial_wpt_content(env):
    return {
        "example/test.html": b"""<title>Example test</title>
<script src='/resources/testharness.js'></script>
<script src='/resources/testharnessreport.js'></script>
<script>
test(() => assert_true(true), "Passing test");
test(() => assert_true(false), "Failing test");
</script>
""",
        "LICENSE": b"Initial license\n",
        "resources/testdriver_vendor.js": b"Initial testdriver_vendor\n",
    }


@pytest.fixture
def initial_meta_content(env):
    return {
        "example/META.yml": b"""
links:
  - url: https://bugzilla-dev.allizom.org/show_bug.cgi?id=1234
    product: firefox
    results:
      - test: test.html
  - label: test-data
    results:
      - test: test.html
"""
    }


@pytest.fixture
def sample_gecko_metadata(env):
    # Only added in tests that require it
    return {
        os.path.join(env.config["gecko"]["path"]["meta"], "example/test.html.ini"): b"""
[test.html]
  [Failing test]
    expected: FAIL
"""
    }


class hg:
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
            return subprocess.check_output(cmd, cwd=self.working_tree)

        call.__name__ = name
        args = (call, self)
        return types.MethodType(*args)


@pytest.fixture(scope="function")
def hg_gecko_upstream(env, initial_gecko_content, initial_wpt_content, git_wpt_upstream):
    repo_dir = os.path.join(env.config["root"], "remotes", "gecko")
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

    content = b"local: %s\nupstream: %s\n" % (local_rev, upstream_rev.hexsha.encode("ascii"))

    wpt_paths, _ = create_file_data(
        initial_wpt_content, repo_dir, env.config["gecko"]["path"]["wpt"]
    )
    meta_paths, _ = create_file_data(
        {"mozilla-sync": content}, repo_dir, env.config["gecko"]["path"]["meta"]
    )
    hg_gecko.add(*(wpt_paths + meta_paths))
    hg_gecko.commit("-m", "Initial wpt commit")

    hg_gecko.bookmark("mozilla/central")
    hg_gecko.bookmark("mozilla/autoland")
    hg_gecko.bookmark("mozilla/inbound")

    yield hg_gecko


@pytest.fixture(scope="function")
def hg_gecko_try(env, hg_gecko_upstream):
    hg_gecko_upstream_dir = os.path.join(env.config["root"], "remotes", "gecko")
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
def git_wpt_metadata_upstream(env, initial_meta_content):
    repo_dir = env.config["wpt-metadata"]["path"]
    os.makedirs(repo_dir)

    git_upstream = git.Repo.init(repo_dir)
    paths, _ = create_file_data(initial_meta_content, repo_dir)
    git_upstream.index.add(paths)

    git_upstream.index.commit("Initial meta commit")
    git_upstream.git.checkout("-b", "initial")

    return git_upstream


def set_remote_urls(repo):
    pyrepo = repo.repo()
    for name, url in repo.remotes:
        try:
            remote = pyrepo.remote(name=name)
        except ValueError:
            remote = pyrepo.create_remote(name, url)
        else:
            current_urls = list(remote.urls)
            if len(current_urls) > 1:
                for old_url in current_urls[1:]:
                    remote.delete_url(old_url)
            remote.set_url(url, current_urls[0], allow_unsafe_protocols=True)


@pytest.fixture(scope="function")
def git_gecko(env, hg_gecko_upstream):
    git_gecko = repos.Gecko(env.config)
    git_gecko.configure("/app/wpt-sync/test/testdata/gecko_config")
    set_remote_urls(git_gecko)
    git_gecko = git_gecko.repo()
    git_gecko.remotes.mozilla.fetch()
    git_gecko.create_head("sync/upstream/inbound", "FETCH_HEAD")
    git_gecko.create_head("sync/upstream/autoland", "FETCH_HEAD")
    git_gecko.create_head("sync/upstream/mozilla-central", "FETCH_HEAD")
    git_gecko.create_head("sync/landing/mozilla-central", "FETCH_HEAD")
    return git_gecko


@pytest.fixture(scope="function")
def git_wpt(env, git_wpt_upstream):
    git_wpt = repos.WebPlatformTests(env.config)
    git_wpt.configure("/app/wpt-sync/test/testdata/wpt_config")
    set_remote_urls(git_wpt)
    return git_wpt.repo()


@pytest.fixture(scope="function")
def git_wpt_metadata(env, git_wpt_metadata_upstream):
    git_wpt_metadata = repos.WptMetadata(env.config)
    git_wpt_metadata.configure("/app/wpt-sync/test/testdata/wpt-metadata_config")
    set_remote_urls(git_wpt_metadata)
    return git_wpt_metadata.repo()


@pytest.fixture
def upstream_wpt_commit(env, git_wpt_upstream, pull_request):
    def inner(message=b"Example change", file_data=None):
        commit = git_commit(git_wpt_upstream, message, file_data)
        return commit

    return inner


def hg_commit(hg, message, bookmarks):
    hg.commit("-m", message)
    rev = hg.log("-l1", "--template={node}")
    if isinstance(bookmarks, (bytes, str)):
        bookmarks = [bookmarks]
    for bookmark in bookmarks:
        hg.bookmark(bookmark)
    assert b"+" not in hg.identify("--id")
    return rev.decode("ascii")


@pytest.fixture
def upstream_gecko_commit(env, hg_gecko_upstream):
    def inner(
        test_changes=None,
        meta_changes=None,
        other_changes=None,
        bug=1234,
        message=b"Example changes",
        bookmarks="mozilla/autoland",
    ):
        changes = gecko_changes(env, test_changes, meta_changes, other_changes)
        message = b"Bug %d - %s" % (bug, message)

        file_data, _ = create_file_data(changes, hg_gecko_upstream.working_tree)
        for path in file_data:
            hg_gecko_upstream.add(path)
        return hg_commit(hg_gecko_upstream, message, bookmarks)

    return inner


@pytest.fixture
def upstream_gecko_backout(env, hg_gecko_upstream):
    def inner(revs, bugs, message=None, bookmarks="mozilla/autoland"):
        if isinstance(revs, str):
            revs = [revs]
        if isinstance(bugs, int):
            bugs = [bugs] * len(revs)
        assert len(bugs) == len(revs)
        msg = [b"Backed out %i changesets (bug %d) for test, r=backout" % (len(revs), bugs[0]), b""]
        for rev, bug in zip(revs, bugs):
            hg_gecko_upstream.backout("--no-commit", rev)
            msg.append(b"Backed out changeset %s (Bug %d)" % (rev[:12].encode("ascii"), bug))
        if message is None:
            message = b"\n".join(msg)
        return hg_commit(hg_gecko_upstream, message, bookmarks)

    return inner


@pytest.fixture
def gecko_worktree(env, git_gecko):
    path = os.path.join(env.config["root"], env.config["paths"]["worktrees"], "gecko", "autoland")
    git_gecko.git.worktree("add", path, env.config["gecko"]["refs"]["autoland"])
    return git.Repo(path)


@pytest.fixture
def wpt_worktree(env, git_wpt):
    def inner(branch="test"):
        path = os.path.join(
            env.config["root"], env.config["paths"]["worktrees"], "web-platform-tests", "test"
        )
        git_wpt.git.worktree("add", path, "origin/master")
        return git.Repo(path)

    inner.__name__ = "wpt_worktree"
    return inner


@pytest.fixture
def local_gecko_commit(env, gecko_worktree):
    def inner(
        test_changes=None,
        meta_changes=None,
        other_changes=None,
        bug=1234,
        message=b"Example changes",
    ):
        changes = gecko_changes(env, test_changes, meta_changes, other_changes)
        message = b"Bug %d - %s" % (bug, message)

        return git_commit(gecko_worktree, message, changes)

    return inner


@pytest.fixture
def pull_request_fn(env, git_wpt_upstream):
    def inner(pr_branch_fn, title="Example PR", body="", pr_id=None):
        git_wpt_upstream.heads.master.checkout()
        gh_commits = []

        branch = pr_branch_fn()
        git_wpt_upstream.branches[branch].checkout()
        for commit_obj in git_wpt_upstream.iter_commits("master..%s" % branch):
            gh_commits.append(
                AttrDict(
                    **{
                        "sha": commit_obj.hexsha,
                        "message": commit_obj.message,
                        "_statuses": [],
                        "_checks": [],
                    }
                )
            )

        pr_id = env.gh_wpt.create_pull(
            title, body, "master", gh_commits[-1]["sha"], _commits=gh_commits, _user="test"
        )
        pr = env.gh_wpt.get_pull(pr_id)

        git_wpt_upstream.git.update_ref("refs/pull/%s/head" % pr_id, "refs/heads/%s" % branch)
        git_wpt_upstream.heads.master.checkout()
        git_wpt_upstream.delete_head(branch, force=True)

        return pr

    inner.__name__ = "pull_request_fn"
    return inner


@pytest.fixture
def pull_request(git_wpt_upstream, pull_request_fn):
    def inner(commit_data, title="Example PR", body="", pr_id=None):
        def commit_fn():
            pr_branch = git_wpt_upstream.create_head("temp_pr")
            git_wpt_upstream.branches["temp_pr"].checkout()
            for message, file_data in commit_data:
                git_commit(git_wpt_upstream, message, file_data)
            git_wpt_upstream.branches.master.checkout()
            return pr_branch.name

        return pull_request_fn(commit_fn, title, body, pr_id)

    inner.__name__ = "pull_request"
    return inner


@pytest.fixture
def pull_request_commit(env, git_wpt_upstream, pull_request):
    def inner(pr_id, commits):
        pr_branch = git_wpt_upstream.create_head("temp_pr", "refs/pull/%s/head" % pr_id)
        pr_branch.checkout()

        gh_commits = []
        for message, file_data in commits:
            rev = git_commit(git_wpt_upstream, message, file_data)
            gh_commits.append(AttrDict(**{"sha": rev.hexsha, "message": message, "_statuses": []}))
        pr = env.gh_wpt.get_pull(pr_id)
        pr._commits.extend(gh_commits)

        git_wpt_upstream.git.update_ref("refs/pull/%s/head" % pr_id, "refs/heads/temp_pr")
        git_wpt_upstream.heads.master.checkout()
        git_wpt_upstream.delete_head(pr_branch, force=True)
        return rev.hexsha

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
        log.append(f"Pushing to try with message:\n{self.worktree.head.commit.message}")
        return repos.cinnabar(git_gecko).git2hg(self.worktree.commit("HEAD~").hexsha)

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
def set_pr_status(git_gecko, git_wpt, env):
    def inner(pr, status="success"):
        from sync import load

        pr = env.gh_wpt.get_pull(pr)
        check_id = 0 if not pr._commits[-1]._checks else pr._commits[-1]._checks[0]["id"] + 1
        for context in env.gh_wpt.required_checks("master"):
            pr._commits[-1]._checks.append(
                {
                    "name": context,
                    "status": "completed",
                    "conclusion": status,
                    "id": check_id,
                    "url": "http://test/",
                    "head_sha": pr._commits[-1].sha,
                    "required": True,
                }
            )
            check_id += 1
        # Add a non-required check
        pr._commits[-1]._checks.append(
            {
                "name": "another-check",
                "status": "completed",
                "conclusion": "failure",
                "id": check_id,
                "url": "http://test/",
                "head_sha": pr._commits[-1].sha,
                "required": False,
            }
        )
        sync = load.get_pr_sync(git_gecko, git_wpt, pr["number"])
        return sync

    return inner


@pytest.fixture
def wptreport_json_data():
    base_results = [
        {
            "test": "/test1.html",
            "message": None,
            "status": "OK",
            "subtests": [
                {"message": None, "name": "Subtest 1", "status": "PASS"},
                {"message": None, "name": "Subtest 2", "status": "PASS"},
                {"message": None, "name": "Subtest 3", "status": "FAIL"},
            ],
        },
        {"message": None, "status": "PASS", "subtests": [], "test": "/test2.html"},
        {"message": None, "status": "FAIL", "subtests": [], "test": "/test3.html"},
    ]

    new_results_1 = copy.deepcopy(base_results)
    new_results_1[0]["subtests"][1]["status"] = "FAIL"
    new_results_1[0]["subtests"].append({"status": "FAIL", "message": None, "name": "Subtest 4"})
    new_results_1[1]["status"] = "FAIL"
    new_results_1[2]["status"] = "CRASH"
    new_results_1.extend(
        [
            {"test": "/test4.html", "status": "FAIL", "message": None, "subtests": []},
            {"test": "/test5.html", "status": "PASS", "message": None, "subtests": []},
        ]
    )

    new_results_2 = copy.deepcopy(new_results_1)
    # On the second platform remove a crash, a regression, and a new failure
    new_results_2[0]["subtests"][1]["status"] = "PASS"
    new_results_2[2]["status"] = "FAIL"
    new_results_2[3]["status"] = "PASS"

    return {
        "central.json": json.dumps({"results": base_results}),
        "try1.json": json.dumps({"results": new_results_1}),
        "try2.json": json.dumps({"results": new_results_2}),
    }


@pytest.fixture
def open_wptreport_path(wptreport_json_data):
    def mock_open(path, *args):
        if path in wptreport_json_data:
            return io.BytesIO(wptreport_json_data[path])
        return open(path, *args)

    return mock_open


@pytest.fixture
def mock_tasks():
    def wpt_tasks(**kwargs):
        tasks = []
        for state, names in kwargs.items():
            for name in names:
                t = {}
                t["status"] = {
                    "state": state,
                    "taskGroupId": "abaaaaaaaaaaaaaaaaaaaa",
                    "taskId": "cdaaaaaaaaaaaaaaaaaaaa",
                }
                t["task"] = {
                    "metadata": {"name": name},
                    "extra": {"suite": {"name": "web-platform-tests"}},
                }
                tasks.append(t)
        return tasks

    return wpt_tasks


@pytest.fixture
def try_push(
    env, git_gecko, git_wpt, git_wpt_upstream, pull_request, set_pr_status, hg_gecko_try, mock_mach
):
    pr = pull_request([(b"Test commit", {"README": b"example_change", "LICENSE": b"Some change"})])
    head_rev = pr._commits[0]["sha"]

    trypush.Mach = mock_mach
    with patch("sync.tree.is_open", Mock(return_value=True)):
        with patch.object(trypush.TryCommit, "read_treeherder", autospec=True) as mock_read:
            mock_read.return_value = "0000000000000000"
            downstream.new_wpt_pr(git_gecko, git_wpt, pr)
            sync = set_pr_status(pr.number, "success")

            with SyncLock.for_process(sync.process_name) as sync_lock:
                git_wpt_upstream.head.commit = head_rev
                git_wpt.remotes.origin.fetch()
                landing.wpt_push(git_gecko, git_wpt, [head_rev], create_missing=False)

                with sync.as_mut(sync_lock):
                    env.gh_wpt.get_pull(sync.pr).merged = True
                    sync.data["affected-tests"] = {"Example": "affected"}
                    sync.next_try_push()
                    sync.data["force-metadata-ready"] = True

                try_push = sync.latest_try_push
                with try_push.as_mut(sync_lock):
                    try_push.taskgroup_id = "abcdef"
    return sync.latest_try_push


@pytest.fixture
def landing_with_try_push(
    env, git_gecko, git_wpt, git_wpt_upstream, upstream_wpt_commit, MockTryCls, mock_mach
):
    base_commit = git_wpt_upstream.head.commit
    new_commit = upstream_wpt_commit(b"First change", {"README": b"Example change\n"})
    git_wpt.remotes.origin.fetch()
    with SyncLock("landing", None) as lock:
        landing_sync = landing.LandingSync.new(
            lock, git_gecko, git_wpt, base_commit.hexsha, new_commit.hexsha
        )
        with landing_sync.as_mut(lock):
            with patch("sync.tree.is_open", Mock(return_value=True)):
                try_push = trypush.TryPush.create(
                    lock,
                    landing_sync,
                    hacks=False,
                    try_cls=MockTryCls,
                    exclude=["pgo", "ccov", "msvc"],
                )
            trypush.Mach = mock_mach
        tree.is_open = lambda x: True
        with try_push.as_mut(lock):
            try_push.taskgroup_id = "abcdef"
    return landing_sync


@pytest.fixture
def MockTryCls():
    class MockTryPush:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args, **kwargs):
            pass

        def push(self):
            return "".join(hex(random.randint(0, 15))[2:] for _ in range(40))

    return MockTryPush


@pytest.fixture
def tc_response():
    class FileData:
        def __init__(self, filename):
            self.path = os.path.join(here, "sample-data", "taskcluster", filename)
            self._file = None

        def __enter__(self):
            self._file = open(self.path, "rb")
            return self._file

        def __exit__(self, *args):
            self._file.close()
            self._file = None

    return FileData


@pytest.fixture
def mock_taskgroup(tc_response):
    def inner(filename):
        with tc_response(filename) as f:
            with requests_mock.Mocker() as m:
                taskgroup_id = "test"
                m.register_uri("GET", f"{tc.QUEUE_BASE}task/{taskgroup_id}", body=None)
                m.register_uri("GET", f"{tc.QUEUE_BASE}task-group/{taskgroup_id}/list", body=f)
                taskgroup = tc.TaskGroup(taskgroup_id)
                taskgroup.refresh()
                return taskgroup

    return inner


@pytest.fixture
def wptfyi_pr_results():
    sha1 = "fcf424c168778e2eaf2a6ca31d19339e3e36beac"
    path = os.path.join(here, "sample-data", "wptfyi", "wptfyi_pr_%s.json" % sha1)

    with open(path) as f:
        results = json.load(f)

    return sha1, results


@pytest.fixture
def wptfyi_metadata():
    path = os.path.join(here, "sample-data", "wptfyi", "metadata.json")

    with open(path) as f:
        metadata = json.load(f)

    return metadata


@pytest.fixture
def pr_19900_github():
    from sync.notify.results import LogFile

    glob_pat = os.path.join(here, "sample-data", "taskcluster", "pr_19900", "GitHub-*")

    base_data = defaultdict(dict)
    head_data = defaultdict(dict)
    for path in glob.glob(glob_pat):
        log_type, browser, target = os.path.splitext(os.path.basename(path))[0].split("-")
        assert log_type == "GitHub"
        dest = {"base": base_data, "head": head_data}[target]
        dest[browser][log_type] = [LogFile(path)]

    return [base_data, head_data]


@pytest.fixture
def pr_19900_gecko_ci():
    from sync.notify.results import get_logs

    base_path = os.path.join(here, "sample-data", "taskcluster", "pr_19900")

    data = [("central_tasks", []), ("try_tasks", [])]
    for dir_name, tasks in data:
        for path in glob.glob(os.path.join(base_path, dir_name, "*.json")):
            task_name = os.path.splitext(os.path.basename(path))[0]
            while True:
                split = task_name.rsplit("-", 1)
                try:
                    int(split[1])
                    break
                except ValueError:
                    task_name = split[0]
            task = {
                "task": {
                    "metadata": {"name": task_name},
                },
                "status": {"runs": [{"_log_paths": {"wptreport.json": path}}]},
            }
            tasks.append(task)

    return [get_logs(item[1]) for item in data]


@pytest.fixture
def phabricator_feed():
    path = os.path.join(here, "sample-data", "phabricator", "feed.json")

    with open(path) as f:
        feed = json.load(f)

    return feed
