from __future__ import annotations
import os
import re
import shutil
import subprocess
import traceback
from collections import defaultdict

import taskcluster
import yaml
import six

from . import base
from . import log
from . import tc
from . import tree
from .env import Environment
from .errors import AbortError, RetryableError
from .index import TaskGroupIndex, TryCommitIndex
from .load import get_syncs
from .lock import constructor, mut
from .projectutil import Mach
from .repos import cinnabar
from .tc import TaskGroupView

from typing import Any, Mapping, MutableMapping, TYPE_CHECKING
from git.repo.base import Repo
if TYPE_CHECKING:
    from sync.downstream import DownstreamSync
    from sync.landing import LandingSync
    from sync.lock import SyncLock
    from sync.tc import TaskGroup


logger = log.get_logger(__name__)
env = Environment()

auth_tc = tc.TaskclusterClient()
rev_re = re.compile("revision=(?P<rev>[0-9a-f]{40})")


class TryCommit:
    def __init__(self,
                 git_gecko: Repo,
                 worktree: Repo,
                 tests_by_type: Mapping[str, list[str]] | None,
                 rebuild: int,
                 hacks: bool = True,
                 **kwargs: Any
                 ) -> None:
        self.git_gecko = git_gecko
        self.worktree = worktree
        self.tests_by_type = tests_by_type
        self.rebuild = rebuild
        self.hacks = hacks
        self.try_rev = None
        self.extra_args = kwargs
        self.reset: str | None = None

    def __enter__(self) -> TryCommit:
        self.create()
        return self

    def __exit__(self, *args: Any, **kwargs: Any) -> None:
        self.cleanup()

    def create(self):
        pass

    def cleanup(self) -> None:
        if self.reset is not None:
            logger.debug("Resetting working tree to %s" % self.reset)
            self.worktree.head.reset(self.reset, working_tree=True)
            self.reset = None

    def apply_hacks(self) -> None:
        # Some changes to forceably exclude certain default jobs that are uninteresting
        # Spidermonkey jobs that take > 3 hours
        logger.info("Removing spidermonkey jobs")
        tc_config = "taskcluster/ci/config.yml"
        working_dir = self.worktree.working_dir
        assert working_dir is not None
        path = os.path.join(working_dir, tc_config)
        if os.path.exists(path):
            with open(path) as f:
                data = yaml.safe_load(f)

            if "try" in data and "ridealong-builds" in data["try"]:
                data["try"]["ridealong-builds"] = {}

            with open(path, "w") as f:
                yaml.dump(data, f)

                self.worktree.index.add([tc_config])

    def push(self) -> str:
        status, output = self._push()
        return self.read_treeherder(status, output)

    def _push(self) -> tuple[int, str]:
        raise NotImplementedError

    def read_treeherder(self, status: int, output: str) -> str:
        msg = f"Failed to push to try:\n{output}"
        try_rev: str | None = None
        if status != 0:
            logger.error(msg)
            raise RetryableError(AbortError(msg))
        rev_match = rev_re.search(output)
        if not rev_match:
            logger.warning(f"No revision found in string:\n\n{output}\n")
            # Assume that the revision is HEAD
            # This happens in tests and isn't a problem, but would be in real code,
            # so that's not ideal
            try:
                try_rev = cinnabar(self.git_gecko).git2hg(self.worktree.head.commit.hexsha)
            except ValueError:
                pass
        else:
            try_rev = rev_match.group('rev')
        if try_rev is None:
            logger.error(msg)
            raise AbortError(msg)
        return try_rev


class TryFuzzyCommit(TryCommit):
    def __init__(self,
                 git_gecko: Repo,
                 worktree: Repo,
                 tests_by_type: Mapping[str, list[str]] | None,
                 rebuild: int,
                 hacks: bool = True,
                 **kwargs: Any
                 ) -> None:
        super().__init__(git_gecko, worktree, tests_by_type, rebuild,
                         hacks=hacks, **kwargs)
        self.queries = self.extra_args.get("queries",
                                           ["web-platform-tests !macosx !shippable !asan !tsan"])
        if isinstance(self.queries, str):
            self.queries = [self.queries]
        self.full = self.extra_args.get("full", False)
        self.disable_target_task_filter = self.extra_args.get("disable_target_task_filter", False)
        self.artifact = self.extra_args.get("artifact", True)

    def create(self) -> None:
        if self.hacks:
            self.reset = self.worktree.head.commit.hexsha
            self.apply_hacks()
            # TODO add something useful to the commit message here since that will
            # appear in email &c.
            self.worktree.index.commit(message="Apply task hacks before running try")

    def _push(self) -> tuple[int, str]:
        self.worktree.git.reset("--hard")

        working_dir = self.worktree.working_dir
        assert working_dir is not None

        mach = Mach(working_dir)
        # Gross hack to create a objdir until we figure out why this is failing
        # from here but not from the shell
        try:
            if not os.path.exists(os.path.join(working_dir,
                                               "obj-x86_64-pc-linux-gnu")):
                mach.python("-c", "")
        except OSError:
            pass

        query_args = []
        for query in self.queries:
            query_args.extend(["-q", query])
        logger.info("Pushing to try with fuzzy query: %s" % " ".join(query_args))

        can_push_routes = b"--route " in mach.try_("fuzzy", "--help")

        args = ["fuzzy"] + query_args
        if self.rebuild:
            args.append("--rebuild")
            args.append(str(self.rebuild))
        if self.full:
            args.append("--full")
        if self.disable_target_task_filter:
            args.append("--disable-target-task-filter")
        if can_push_routes:
            args.append("--route=notify.pulse.wptsync.try-task.on-any")
        if self.artifact:
            args.append("--artifact")
        else:
            args.append("--no-artifact")

        if self.tests_by_type is not None:
            paths = []
            all_paths = set()
            for values in self.tests_by_type.values():
                for item in values:
                    if (item not in all_paths and
                        os.path.exists(os.path.join(working_dir,
                                                    item))):
                        paths.append(item)
                    all_paths.add(item)
            max_tests = env.config["gecko"]["try"].get("max-tests")
            if max_tests and len(paths) > max_tests:
                logger.warning("Capping number of affected tests at %d" % max_tests)
                paths = paths[:max_tests]
            args.extend(paths)

        try:
            output = mach.try_(*args, stderr=subprocess.STDOUT)
            return 0, output.decode("utf8", "replace")
        except subprocess.CalledProcessError as e:
            return e.returncode, e.output


class TryPush(base.ProcessData):
    """A try push is represented by an annotated tag with a path like

    try/<pr_id>/<id>

    Where id is a number to indicate the Nth try push for this PR.
    """
    obj_type = "try"
    statuses = ("open", "complete", "infra-fail")
    status_transitions = [("open", "complete"),
                          ("complete", "open"),  # For reopening "failed" landing try pushes
                          ("infra-fail", "complete")]

    @classmethod
    @constructor(lambda args: (args["sync"].process_name.subtype,
                               args["sync"].process_name.obj_id))
    def create(cls,
               lock: SyncLock,
               sync: DownstreamSync | LandingSync,
               affected_tests: dict[str, list[str]] | None = None,
               stability: bool = False,
               hacks: bool = True,
               try_cls: type = TryFuzzyCommit,
               rebuild_count: int | None = None,
               check_open: bool = True,
               **kwargs: Any
               ) -> TryPush:
        logger.info("Creating try push for PR %s" % sync.pr)
        if check_open and not tree.is_open("try"):
            logger.info("try is closed")
            raise RetryableError(AbortError("Try is closed"))

        # Ensure the required indexes exist
        TaskGroupIndex.get_or_create(sync.git_gecko)
        try_idx = TryCommitIndex.get_or_create(sync.git_gecko)

        git_work = sync.gecko_worktree.get()

        if rebuild_count is None:
            rebuild_count = 0 if not stability else env.config['gecko']['try']['stability_count']
            if not isinstance(rebuild_count, int):
                logger.error("Could not find config for Stability rebuild count, using default 5")
                rebuild_count = 5
        with try_cls(sync.git_gecko, git_work, affected_tests, rebuild_count, hacks=hacks,
                     **kwargs) as c:
            try_rev = c.push()

        data = {
            "try-rev": try_rev,
            "stability": stability,
            "gecko-head": sync.gecko_commits.head.sha1,
            "wpt-head": sync.wpt_commits.head.sha1,
            "status": "open",
            "bug": sync.bug,
        }
        process_name = base.ProcessName.with_seq_id(sync.git_gecko,
                                                    cls.obj_type,
                                                    sync.sync_type,
                                                    six.ensure_text(
                                                        str(getattr(sync, sync.obj_id))))
        rv = super().create(lock, sync.git_gecko, process_name, data)
        try_idx.insert(try_idx.make_key(try_rev), process_name)

        with rv.as_mut(lock):
            rv.created = taskcluster.fromNowJSON("0 days")

        if sync.bug is not None:
            env.bz.comment(sync.bug,
                           "Pushed to try%s %s" %
                           (" (stability)" if stability else "",
                            rv.treeherder_url))

        return rv

    @classmethod
    def load_all(cls, git_gecko):
        process_names = base.ProcessNameIndex(git_gecko).get("try")
        for process_name in process_names:
            yield cls(git_gecko, process_name)

    @classmethod
    def for_commit(cls, git_gecko, sha1):
        idx = TryCommitIndex(git_gecko)
        process_name = idx.get(idx.make_key(sha1))
        if process_name:
            logger.info(f"Found try push {process_name!r} for rev {sha1}")
            return cls(git_gecko, process_name)
        logger.info(f"No try push for rev {sha1}")

    @classmethod
    def for_taskgroup(cls, git_gecko, taskgroup_id):
        idx = TaskGroupIndex(git_gecko)
        process_name = idx.get(idx.make_key(taskgroup_id))
        if process_name:
            return cls(git_gecko, process_name)

    @property
    def treeherder_url(self) -> str:
        return "https://treeherder.mozilla.org/#/jobs?repo=try&revision=%s" % self.try_rev

    @property
    def created(self) -> Any | None:
        return self.get("created")

    @created.setter  # type: ignore
    @mut()
    def created(self, value: str) -> None:
        self["created"] = value

    @property
    def try_rev(self) -> str | None:
        return self.get("try-rev")

    @try_rev.setter  # type: ignore
    @mut()
    def try_rev(self, value: str) -> None:
        idx = TryCommitIndex(self.repo)
        if self.try_rev is not None:
            idx.delete(idx.make_key(self.try_rev), self.process_name)
        self._data["try-rev"] = value
        assert self.try_rev is not None
        idx.insert(idx.make_key(self.try_rev), self.process_name)

    @property
    def taskgroup_id(self) -> str | None:
        return self.get("taskgroup-id")

    @taskgroup_id.setter  # type: ignore
    @mut()
    def taskgroup_id(self, value: str) -> None:
        self["taskgroup-id"] = value
        idx = TaskGroupIndex(self.repo)
        if value:
            idx.insert(idx.make_key(value), self.process_name)

    @property
    def status(self) -> str:
        return self.get("status")

    @status.setter  # type: ignore
    @mut()
    def status(self, value: str) -> None:
        if value not in self.statuses:
            raise ValueError("Unrecognised status %s" % value)
        current = self.get("status")
        if current == value:
            return
        if (current, value) not in self.status_transitions:
            raise ValueError(f"Tried to change status from {current} to {value}")
        self["status"] = value

    @property
    def wpt_head(self) -> str:
        return self.get("wpt-head")

    def sync(self, git_gecko, git_wpt):
        process_name = self.process_name
        syncs = get_syncs(git_gecko,
                          git_wpt,
                          process_name.subtype,
                          process_name.obj_id)
        if len(syncs) == 0:
            return None
        if len(syncs) == 1:
            return syncs.pop()
        for item in syncs:
            if item.status == "open":
                return item
        raise ValueError("Got multiple syncs and none were open")

    @property
    def stability(self) -> bool:
        """Is the current try push a stability test"""
        return self["stability"]

    @property
    def bug(self) -> int:
        return int(self.get("bug"))

    @property
    def infra_fail(self) -> bool:
        """Does this push have infrastructure failures"""
        if self.status == "infra-fail":
            self.status = "complete"  # type: ignore
            self.infra_fail = True  # type: ignore
        return self.get("infra-fail", False)

    @infra_fail.setter  # type: ignore
    @mut()
    def infra_fail(self, value: bool) -> None:
        """Set the status of this push's infrastructure failure state"""
        if value == self.get("infra-fail"):
            return
        self["infra-fail"] = value
        if value:
            self.notify_failed_builds()

    def notify_failed_builds(self) -> None:
        if self.taskgroup_id is None:
            logger.error("No task group for try push %s" % self)
            return
        tasks = tc.TaskGroup(self.taskgroup_id).view()
        failed = tasks.failed_builds()

        if not failed:
            logger.error("No failed builds to report for Try push: %s" % self)
            return

        bug = self.bug

        if not bug:
            logger.error("No associated bug found for Try push: %s" % self)
            return

        msg = "There were infrastructure failures for the Try push (%s):\n" % self.treeherder_url
        msg += "\n".join(task.get("task", {}).get("metadata", {}).get("name")
                         for task in failed.tasks)
        env.bz.comment(bug, msg)

    @property
    def accept_failures(self) -> bool:
        return self.get("accept-failures", False)

    @accept_failures.setter  # type: ignore
    @mut()
    def accept_failures(self, value: bool) -> None:
        self["accept-failures"] = value

    def tasks(self) -> TryPushTasks | None:
        """Get a list of all the taskcluster tasks for web-platform-tests
        jobs associated with the current try push.

        :return: List of tasks
        """
        if self.taskgroup_id is None:
            return None
        task_id = tc.normalize_task_id(self.taskgroup_id)
        if task_id != self.taskgroup_id:
            self.taskgroup_id = task_id  # type: ignore

        tasks = tc.TaskGroup(self.taskgroup_id)
        tasks.refresh()

        return TryPushTasks(tasks)

    def log_path(self) -> str:
        assert self.try_rev is not None
        return os.path.join(env.config["root"], env.config["paths"]["try_logs"],
                            "try", self.try_rev)

    @mut()
    def download_logs(self, wpt_taskgroup: TaskGroupView | TryPushTasks,
                      first_only: bool = False) -> TaskGroupView:
        """Download all the wptreport logs for the current try push

        :return: List of paths to logs
        """
        # Allow passing either TryPushTasks or the actual TaskGroupView
        if hasattr(wpt_taskgroup, "wpt_tasks"):
            assert isinstance(wpt_taskgroup, TryPushTasks)
            wpt_tasks = wpt_taskgroup.wpt_tasks
        else:
            assert isinstance(wpt_taskgroup, TaskGroupView)
            wpt_tasks = wpt_taskgroup

        exclude = set()

        def included(t: dict[str, dict[str, Any]]) -> bool:
            # if a name is on the excluded list, only download SUCCESS job logs
            name = t.get("task", {}).get("metadata", {}).get("name")
            state = t.get("status", {}).get("state")
            output = name not in exclude or state == tc.SUCCESS

            # Add this name to exclusion list, so that we don't download the repeated run
            # logs in a stability try run
            if first_only and name is not None and name not in exclude:
                exclude.add(name)

            return output

        if self.try_rev is None:
            if wpt_tasks:
                logger.info("Got try push with no rev; setting it from a task")
                try_rev = (next(iter(wpt_tasks))
                           .get("task", {})
                           .get("payload", {})
                           .get("env", {})
                           .get("GECKO_HEAD_REV"))
                if try_rev:
                    self.try_rev = try_rev  # type: ignore
                else:
                    raise ValueError("Unknown try rev for %s" % self.process_name)

        include_tasks = wpt_tasks.filter(included)
        logger.info("Downloading logs for try revision %s" % self.try_rev)
        file_names = ["wptreport.json"]
        include_tasks.download_logs(self.log_path(), file_names)
        return include_tasks

    @mut()
    def cleanup_logs(self):
        logger.info("Removing downloaded for try push %s" % self.process_name)
        try:
            shutil.rmtree(self.log_path())
        except Exception:
            logger.warning("Failed to remove logs %s:%s" %
                           (self.log_path(), traceback.format_exc()))

    @mut()
    def delete(self):
        super().delete()
        for (idx_cls, data) in [(TaskGroupIndex, self.taskgroup_id),
                                (TryCommitIndex, self.try_rev)]:
            if data is not None:
                idx = idx_cls(self.repo)
                key = idx.make_key(data)
                idx.delete(key, data)


class TryPushTasks:
    _retrigger_count = 6
    # min rate of job success to proceed with metadata update
    _min_success = 0.7

    def __init__(self, tasks: TaskGroup) -> None:
        """Wrapper object that implements sync-specific business logic on top of a
        list of tasks"""
        self.wpt_tasks = tasks.view(tc.is_suite_fn("web-platform-tests"))

    def __len__(self) -> int:
        return len(self.wpt_tasks)

    def complete(self, allow_unscheduled: bool = False) -> bool:
        return self.wpt_tasks.is_complete(allow_unscheduled)

    def validate(self):
        err = None
        if not len(self.wpt_tasks):
            err = ("No wpt tests found. Check decision task %s" %
                   self.wpt_tasks.taskgroup.taskgroup_id)
        else:
            exception_tasks = self.wpt_tasks.filter(tc.is_status_fn(tc.EXCEPTION))
            if float(len(exception_tasks)) / len(self.wpt_tasks) > (1 - self._min_success):
                err = ("Too many exceptions found among wpt tests. "
                       "Check decision task %s" % self.wpt_tasks.taskgroup.taskgroup_id)
        if err:
            logger.error(err)
            return False
        return True

    def retrigger_failures(self, count: int = _retrigger_count) -> int:
        task_states = self.wpt_states()

        def is_failure(task_data: dict) -> bool:
            states = task_data["states"]
            return states[tc.FAIL] > 0 or states[tc.EXCEPTION] > 0

        def is_excluded(name: str) -> bool:
            return "-aarch64" in name

        failures = [data["task_id"] for name, data in task_states.items()
                    if is_failure(data) and not is_excluded(name)]
        retriggered_count = 0
        for task_id in failures:
            jobs = auth_tc.retrigger(task_id, count=count)
            if jobs:
                retriggered_count += len(jobs)
        return retriggered_count

    def wpt_states(self) -> Mapping[str, Any]:
        # e.g. {"test-linux32-stylo-disabled/opt-web-platform-tests-e10s-6": {
        #           "task_id": "abc123"
        #           "states": {
        #               "completed": 5,
        #               "failed": 1
        #           }
        #       }}
        by_name = self.wpt_tasks.by_name()
        task_states: MutableMapping[str, Any] = defaultdict(lambda:
                                                            defaultdict(lambda: defaultdict(int)))
        for name, tasks in by_name.items():
            for task in tasks:
                task_id = task.get("status", {}).get("taskId")
                state = task.get("status", {}).get("state")
                task_states[name]["states"][state] += 1
                # need one task_id per job group for retriggering
                task_states[name]["task_id"] = task_id
        return task_states

    def failed_builds(self) -> TaskGroupView:
        """Return the builds that failed"""
        return self.wpt_tasks.failed_builds()

    def successful_builds(self):
        """Return the builds that were successful"""
        builds = self.wpt_tasks.filter(tc.is_build)
        return builds.filter(tc.is_status_fn({tc.SUCCESS}))

    def retriggered_wpt_states(self) -> Mapping[str, Mapping[str, Any]]:
        # some retrigger requests may have failed, and we try to ignore
        # manual/automatic retriggers made outside of wptsync
        threshold = max(1, self._retrigger_count / 2)
        task_counts = self.wpt_states()
        return {name: data for name, data in task_counts.items()
                if sum(data["states"].values()) > threshold}

    def success(self) -> bool:
        """Check if all the wpt tasks in a try push ended with a successful status"""
        wpt_tasks = self.wpt_tasks
        if wpt_tasks:
            return all(task.get("status", {}).get("state") == tc.SUCCESS for task in wpt_tasks)
        return False

    def has_failures(self):
        """Check if any of the wpt tasks in a try push ended with a failure status"""
        wpt_tasks = self.wpt_tasks
        if wpt_tasks:
            return any(task.get("status", {}).get("state") == tc.FAIL for task in wpt_tasks)
        return False

    def has_completed_tests(self) -> bool:
        """Check if all the wpt tasks in a try push completed"""
        wpt_tasks = self.wpt_tasks.filter(tc.is_test)
        if wpt_tasks:
            return any(task.get("status", {}).get("state") in (tc.SUCCESS, tc.FAIL)
                       for task in wpt_tasks)
        return False

    def success_rate(self) -> float:
        wpt_tasks = self.wpt_tasks

        if not wpt_tasks:
            return float(0)

        success = wpt_tasks.filter(tc.is_status_fn(tc.SUCCESS))
        return float(len(success)) / len(wpt_tasks)

    def failure_limit_exceeded(self, target_rate: float = _min_success) -> bool:
        return self.success_rate() < target_rate
