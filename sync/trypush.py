import os
import re
import shutil
import subprocess
import traceback
import gzip
from collections import defaultdict
from datetime import datetime, timedelta

import newrelic
import taskcluster
import yaml

import base
import log
import tc
import tree
from env import Environment
from index import TaskGroupIndex, TryCommitIndex
from load import get_syncs
from lock import constructor, mut
from errors import AbortError, RetryableError
from projectutil import Mach

logger = log.get_logger(__name__)
env = Environment()

auth_tc = tc.TaskclusterClient()
rev_re = re.compile("revision=(?P<rev>[0-9a-f]{40})")


class TryCommit(object):
    def __init__(self, git_gecko, worktree, tests_by_type, rebuild, hacks=True, **kwargs):
        self.git_gecko = git_gecko
        self.worktree = worktree
        self.tests_by_type = tests_by_type
        self.rebuild = rebuild
        self.hacks = hacks
        self.try_rev = None
        self.extra_args = kwargs
        self.reset = None

    def __enter__(self):
        self.create()
        return self

    def __exit__(self, *args, **kwargs):
        self.cleanup()

    def create(self):
        pass

    def cleanup(self):
        if self.reset is not None:
            logger.debug("Resetting working tree to %s" % self.reset)
            self.worktree.head.reset(self.reset, working_tree=True)
            self.reset = None

    def apply_hacks(self):
        # Some changes to forceably exclude certain default jobs that are uninteresting
        # Spidermonkey jobs that take > 3 hours
        logger.info("Removing spidermonkey jobs")
        tc_config = "taskcluster/ci/config.yml"
        path = os.path.join(self.worktree.working_dir, tc_config)
        if os.path.exists(path):
            with open(path) as f:
                data = yaml.safe_load(f)

            if "try" in data and "ridealong-builds" in data["try"]:
                data["try"]["ridealong-builds"] = {}

            with open(path, "w") as f:
                yaml.dump(data, f)

                self.worktree.index.add([tc_config])

    @newrelic.agent.function_trace()
    def push(self):
        status, output = self._push()
        return self.read_treeherder(status, output)

    def _push(self):
        raise NotImplementedError

    def read_treeherder(self, status, output):
        if status != 0:
            logger.error("Failed to push to try:\n%s" % output)
            raise RetryableError(AbortError("Failed to push to try"))
        rev_match = rev_re.search(output)
        if not rev_match:
            logger.warning("No revision found in string:\n\n{}\n".format(output))
            # Assume that the revision is HEAD
            # This happens in tests and isn't a problem, but would be in real code,
            # so that's not ideal
            try:
                try_rev = self.git_gecko.cinnabar.git2hg(self.worktree.head.commit.hexsha)
            except ValueError:
                return None
        else:
            try_rev = rev_match.group('rev')
        return try_rev


class TryFuzzyCommit(TryCommit):
    def __init__(self, git_gecko, worktree, tests_by_type, rebuild, hacks=True, **kwargs):
        super(TryFuzzyCommit, self).__init__(git_gecko, worktree, tests_by_type, rebuild,
                                             hacks=hacks, **kwargs)
        self.exclude = self.extra_args.get("exclude", ["macosx", "shippable"])
        self.include = self.extra_args.get("include", ["web-platform-tests"])

    def create(self):
        if self.hacks:
            self.reset = self.worktree.head.commit.hexsha
            self.apply_hacks()
            # TODO add something useful to the commit message here since that will
            # appear in email &c.
            self.worktree.index.commit(message="Apply task hacks before running try")

    @property
    def query(self):
        return " ".join(self.include + ["!%s" % item for item in self.exclude])

    def _push(self):
        self.worktree.git.reset("--hard")
        mach = Mach(self.worktree.working_dir)
        # Gross hack to create a objdir until we figure out why this is failing
        # from here but not from the shell
        try:
            if not os.path.exists(os.path.join(self.worktree.working_dir,
                                               "obj-x86_64-pc-linux-gnu")):
                mach.python("-c", "")
        except OSError:
            pass
        query = self.query

        logger.info("Pushing to try with fuzzy query: %s" % query)

        args = ["fuzzy", "-q", query, "--artifact"]
        if self.rebuild:
            args.append("--rebuild")
            args.append(str(self.rebuild))

        if self.tests_by_type is not None:
            paths = []
            for values in self.tests_by_type.itervalues():
                paths.extend(item for item in values
                             if os.path.exists(os.path.join(self.worktree.working_dir,
                                                            item)))
            max_tests = env.config["gecko"]["try"].get("max-tests")
            if max_tests and len(paths) > max_tests:
                logger.warning("Capping number of affected tests at %d" % max_tests)
                paths = paths[:max_tests]
            args.extend(paths)

        try:
            output = mach.try_(*args, stderr=subprocess.STDOUT)
            return 0, output
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
    def create(cls, lock, sync, affected_tests=None, stability=False, hacks=True,
               try_cls=TryFuzzyCommit, rebuild_count=None, check_open=True, **kwargs):
        logger.info("Creating try push for PR %s" % sync.pr)
        if check_open and not tree.is_open("try"):
            logger.info("try is closed")
            raise RetryableError(AbortError("Try is closed"))

        # Ensure the required indexes exist
        TaskGroupIndex.get_or_create(sync.git_gecko)
        try_idx = TryCommitIndex.get_or_create(sync.git_gecko)

        git_work = sync.gecko_worktree.get()

        if rebuild_count is None:
            rebuild_count = 0 if not stability else 10
        with try_cls(sync.git_gecko, git_work, affected_tests, rebuild_count, hacks=hacks,
                     **kwargs) as c:
            try_rev = c.push()

        data = {
            "try-rev": try_rev,
            "stability": stability,
            "gecko-head": sync.gecko_commits.head.sha1,
            "wpt-head": sync.wpt_commits.head.sha1,
            "status": "open",
        }
        process_name = base.ProcessName.with_seq_id(sync.git_gecko,
                                                    cls.obj_type,
                                                    sync.sync_type,
                                                    getattr(sync, sync.obj_id))
        rv = super(TryPush, cls).create(lock, sync.git_gecko, process_name, data)
        # Add to the index
        if try_rev:
            try_idx.insert(try_idx.make_key(try_rev), process_name)

        with rv.as_mut(lock):
            rv.created = taskcluster.fromNowJSON("0 days")

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
            logger.info("Found try push %r for rev %s" % (process_name, sha1))
            return cls(git_gecko, process_name)
        logger.info("No try push for rev %s" % (sha1,))

    @classmethod
    def for_taskgroup(cls, git_gecko, taskgroup_id):
        idx = TaskGroupIndex(git_gecko)
        process_name = idx.get(idx.make_key(taskgroup_id))
        if process_name:
            return cls(git_gecko, process_name)

    @property
    def treeherder_url(self):
        return "https://treeherder.mozilla.org/#/jobs?repo=try&revision=%s" % self.try_rev

    def __eq__(self, other):
        if not other.__class__ == self.__class__:
            return False
        return other.process_name == self.process_name

    @property
    def created(self):
        return self.get("created")

    @created.setter
    @mut()
    def created(self, value):
        self["created"] = value

    @property
    def try_rev(self):
        return self.get("try-rev")

    @try_rev.setter
    @mut()
    def try_rev(self, value):
        idx = TryCommitIndex(self.repo)
        if self.try_rev is not None:
            idx.delete(idx.make_key(self.try_rev), self.process_name)
        self._data["try-rev"] = value
        idx.insert(idx.make_key(self.try_rev), self.process_name)

    @property
    def taskgroup_id(self):
        return self.get("taskgroup-id")

    @taskgroup_id.setter
    @mut()
    def taskgroup_id(self, value):
        self["taskgroup-id"] = value
        idx = TaskGroupIndex(self.repo)
        if value:
            idx.insert(idx.make_key(value), self.process_name)

    @property
    def status(self):
        return self.get("status")

    @status.setter
    @mut()
    def status(self, value):
        if value not in self.statuses:
            raise ValueError("Unrecognised status %s" % value)
        current = self.get("status")
        if current == value:
            return
        if (current, value) not in self.status_transitions:
            raise ValueError("Tried to change status from %s to %s" % (current, value))
        self["status"] = value

    @property
    def wpt_head(self):
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

    def expired(self):
        now = taskcluster.fromNow("0 days")
        created_date = None
        is_expired = True
        try:
            if self.created:
                created_date = datetime.strptime(self.created, tc._DATE_FMT)
            else:
                # for legacy pushes, save creation date from TaskCluster
                task = tc.get_task(self.taskgroup_id)
                if task and task.get("created"):
                    self.created = task["created"]
                    created_date = datetime.strptime(self.created, tc._DATE_FMT)
        except ValueError:
            logger.debug("Failed to determine creation date for %s" % self)
        if created_date:
            # try push created more than 14 days ago
            is_expired = now > created_date + timedelta(days=14)
        return is_expired

    @property
    def stability(self):
        """Is the current try push a stability test"""
        return self["stability"]

    @property
    def infra_fail(self):
        """Is the current try push a stability test"""
        if self.status == "infra-fail":
            self.status = "complete"
            self.infra_fail = True
        return self.get("infra-fail", False)

    @infra_fail.setter
    @mut()
    def infra_fail(self, value):
        """Is the current try push a stability test"""
        self["infra-fail"] = value

    def tasks(self):
        """Get a list of all the taskcluster tasks for web-platform-tests
        jobs associated with the current try push.

        :return: List of tasks
        """
        task_id = tc.normalize_task_id(self.taskgroup_id)
        if task_id != self.taskgroup_id:
            self.taskgroup_id = task_id

        tasks = tc.TaskGroup(self.taskgroup_id)
        tasks.refresh()

        return TryPushTasks(tasks)

    def log_path(self):
        return os.path.join(env.config["root"], env.config["paths"]["try_logs"],
                            "try", self.try_rev)

    @mut()
    def download_logs(self, wpt_tasks, raw=True, report=True, exclude=None):
        """Download all the logs for the current try push

        :return: List of paths to raw logs
        """
        # Allow passing either TryPushTasks or the actual TaskGroupView
        if hasattr(wpt_tasks, "wpt_tasks"):
            wpt_tasks = wpt_tasks.wpt_tasks

        if exclude is None:
            exclude = []

        def included(t):
            # if a name is on the excluded list, only download SUCCESS job logs
            name = t.get("task", {}).get("metadata", {}).get("name")
            state = t.get("status", {}).get("state")
            return name not in exclude or state == tc.SUCCESS

        if self.try_rev is None:
            if wpt_tasks:
                logger.info("Got try push with no rev; setting it from a task")
                try_rev = (iter(wpt_tasks).next()
                           .get("task", {})
                           .get("payload", {})
                           .get("env", {})
                           .get("GECKO_HEAD_REV"))
                if try_rev:
                    self.try_rev = try_rev
                else:
                    raise ValueError("Unknown try rev for %s" % self.process_name)

        include_tasks = wpt_tasks.filter(included)
        logger.info("Downloading logs for try revision %s" % self.try_rev)
        file_names = []
        if raw:
            file_names.append("wpt_raw.log")
        if report:
            file_names.append("wptreport.json")
        include_tasks.download_logs(self.log_path(), file_names)
        return include_tasks

    @mut()
    def download_raw_logs(self, wpt_tasks, exclude=None):
        wpt_tasks = self.download_logs(raw=True, report=True, exclude=exclude)
        raw_logs = []
        for task in wpt_tasks:
            for run in task.get("status", {}).get("runs", []):
                log = run.get("_log_paths", {}).get("wpt_raw.log")
                if log:
                    raw_logs.append(log)
        return raw_logs

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
        super(TryPush, self).delete()
        for (idx_cls, data) in [(TaskGroupIndex, self.taskgroup_id),
                                (TryCommitIndex, self.try_rev)]:
            if data is not None:
                idx = idx_cls(self.repo)
                key = idx.make_key(data)
                idx.delete(key, data)


class TcPush(TryPush):
    @classmethod
    @constructor(lambda args: (args["sync"].process_name.subtype,
                               args["sync"].process_name.obj_id))
    def create(cls, lock, sync, affected_tests=None, stability=False, hacks=True,
               try_cls=TryFuzzyCommit, rebuild_count=None, check_open=True, **kwargs):
        logger.info("Creating Tc object for PR %s" % sync.pr)

        data = {
            "gecko-head": sync.gecko_commits.head.sha1,
            "wpt-head": sync.wpt_commits.head.sha1,
            "status": "open",
        }
        process_name = base.ProcessName.with_seq_id(sync.git_gecko,
                                                    cls.obj_type,
                                                    sync.sync_type,
                                                    getattr(sync, sync.obj_id))

        # This creates a ref for try/downstream/<pr>/<seq_id>, we probably don't want that
        rv = super(TryPush, cls).create(lock, sync.git_gecko, process_name, data)

        with rv.as_mut(lock):
            rv.created = taskcluster.fromNowJSON("0 days")
            rv.pr_id = str(sync.pr)

        return rv

    def log_path(self):
        return os.path.join(env.config["root"], env.config["paths"]["try_logs"],
                            "gh_wpt", self.pr_id)

    @mut()
    def download_logs(self, tasks, session=None):
        filename = 'wpt_report.json'
        base_path = self.log_path()

        archive = filename + '.gz'
        tasks.download_logs(base_path, [archive])

        # Find the archive, decompress, and delete archive
        task = tasks.tasks[0]
        task_id = task.get('status', {}).get('taskId')
        if not task_id:
            logger.error("TaskCluster task not found for PR %s" % self.pr_id)
            return
        log_path = os.path.join(base_path, task_id + "_" + filename)
        archive_path = os.path.join(base_path, task_id + "_" + archive)

        # TODO maybe need to convert wpt_report to wptreport to be consistent with Trypush versions
        with gzip.open(archive_path, 'rb') as f:
            with open(log_path, 'wb') as outfile:
                shutil.copyfileobj(f, outfile)
        os.remove(archive_path)

        runs = task.get('status', {}).get('runs', [])
        if not runs:
            logger.debug("No runs found for task %s" % task_id)
            return
        run = runs[-1]
        assert run['_log_paths'][archive] == archive_path
        del run['_log_paths'][archive]
        run['_log_paths'][filename] = log_path
        return tasks


class TryPushTasks(object):
    _retrigger_count = 6
    # min rate of job success to proceed with metadata update
    _min_success = 0.7

    def __init__(self, tasks):
        """Wrapper object that implements sync-specific business logic on top of a
        list of tasks"""
        self.wpt_tasks = tasks.view(tc.is_suite_fn("web-platform-tests"))

    def __len__(self):
        return len(self.wpt_tasks)

    def complete(self, allow_unscheduled=False):
        return self.wpt_tasks.is_complete(allow_unscheduled)

    def validate(self):
        err = None
        if not len(self.wpt_tasks):
            err = ("No wpt tests found. Check decision task {}" %
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

    def retrigger_failures(self, count=_retrigger_count):
        task_states = self.wpt_states()

        def is_failure(task_data):
            states = task_data["states"]
            return states[tc.FAIL] > 0 or states[tc.EXCEPTION] > 0

        failures = [data["task_id"] for name, data in task_states.iteritems() if is_failure(data)]
        retriggered_count = 0
        for task_id in failures:
            jobs = auth_tc.retrigger(task_id, count=count)
            if jobs:
                retriggered_count += len(jobs)
        return retriggered_count

    def wpt_states(self):
        # e.g. {"test-linux32-stylo-disabled/opt-web-platform-tests-e10s-6": {
        #           "task_id": "abc123"
        #           "states": {
        #               "completed": 5,
        #               "failed": 1
        #           }
        #       }}
        by_name = self.wpt_tasks.by_name()
        task_states = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
        for name, tasks in by_name.iteritems():
            for task in tasks:
                task_id = task.get("status", {}).get("taskId")
                state = task.get("status", {}).get("state")
                task_states[name]["states"][state] += 1
                # need one task_id per job group for retriggering
                task_states[name]["task_id"] = task_id
        return task_states

    def failed_builds(self):
        builds = self.wpt_tasks.filter(tc.is_build)
        return builds.filter(tc.is_status_fn({tc.FAIL, tc.EXCEPTION}))

    def retriggered_wpt_states(self):
        # some retrigger requests may have failed, and we try to ignore
        # manual/automatic retriggers made outside of wptsync
        threshold = max(1, self._retrigger_count / 2)
        task_counts = self.wpt_states()
        return {name: data for name, data in task_counts.iteritems()
                if sum(data["states"].itervalues()) > threshold}

    def success(self):
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

    def success_rate(self):
        wpt_tasks = self.wpt_tasks

        if not wpt_tasks:
            return float(0)

        success = wpt_tasks.filter(tc.is_status_fn(tc.SUCCESS))
        return float(len(success)) / len(wpt_tasks)

    def failure_limit_exceeded(self, target_rate=_min_success):
        return self.success_rate() < target_rate
