import os
import re
import subprocess
from itertools import chain

import yaml

import base
import log
import tc
import tree
from env import Environment
from load import get_syncs
from pipeline import AbortError
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

    def push(self):
        status, output = self._push()
        return self.read_treeherder(status, output)

    def _push(self):
        raise NotImplementedError

    def read_treeherder(self, status, output):
        if status != 0:
            logger.error("Failed to push to try.")
            # TODO retry
            raise AbortError("Failed to push to try")
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


class TrySyntaxCommit(TryCommit):
    def create(self):
        self.reset = self.worktree.head.commit.hexsha
        message = self.try_message(self.tests_by_type, self.rebuild)
        if self.hacks:
            self.apply_hacks()
        self.worktree.index.commit(message=message)

    @staticmethod
    def try_message(tests_by_type=None, rebuild=0):
        """Build a try message

        Args:
            tests_by_type: dict of test paths grouped by wpt test type.
                           If dict is empty, no tests are affected so schedule just
                           first chunk of web-platform-tests.
                           If dict is None, schedule all wpt test jobs.
            rebuild: Number of times to repeat each test, max 20
            base: base directory for tests

        Returns:
            str: try message
        """
        test_data = {
            "test_jobs": [],
            "prefixed_paths": []
        }
        # Example: try: -b do -p win32,win64,linux64,linux,macosx64 -u
        # web-platform-tests[linux64-stylo,Ubuntu,10.10,Windows 8,Windows 10]
        #  -t none --artifact
        try_message = ("try: -b do -p win32,win64,linux64,linux -u {test_jobs} "
                       "-t none --artifact")
        if rebuild:
            try_message += " --rebuild {}".format(rebuild)

        test_type_suite = {
            "testharness": ["web-platform-tests-e10s-1",
                            "web-platform-tests-1"],
            "reftest": ["web-platform-tests-reftests",
                        "web-platform-tests-reftests-e10s-1"],
            "wdspec": ["web-platform-tests-wdspec"],
        }

        test_type_flavor = {
            "testharness": "web-platform-tests",
            "reftest": "web-platform-tests-reftests",
            "wdspec": "web-platform-tests-wdspec"
        }

        platform_suffix = "[linux64-stylo,Ubuntu,10.10,Windows 10]"

        def platform_filter(suite):
            return platform_suffix if "-wdspec-" not in suite else ""

        path_flavors = None

        if tests_by_type is None:
            suites = ["web-platform-tests"]
        elif len(tests_by_type) == 0:
            suites = [test_type_suite["testharness"]]
            # run first chunk of the wpt job if no tests are affected
            suites = chain.from_iterable(test_type_suite.itervalues())
        else:
            suites = chain.from_iterable(value for key, value in test_type_suite.iteritems()
                                         if key in tests_by_type)
            path_flavors = {value: tests_by_type[key] for key, value in test_type_flavor.iteritems()
                            if key in tests_by_type}

        test_data["test_jobs"] = ",".join("%s%s" % (suite, platform_filter(suite))
                                          for suite in suites)
        if path_flavors:
            try_message += " --try-test-paths {prefixed_paths}"
            test_data["prefixed_paths"] = []

            base = env.config["gecko"]["path"]["wpt"]
            for flavor, paths in path_flavors.iteritems():
                if paths:
                    for p in paths:
                        if base is not None and not p.startswith(base):
                            # try server expects paths relative to m-c root dir
                            p = os.path.join(base, p)
                        test_data["prefixed_paths"].append(flavor + ":" + p)
            test_data["prefixed_paths"] = " ".join(test_data["prefixed_paths"])

        return try_message.format(**test_data)

    def _push(self):
        logger.info("Pushing to try with message:\n{}".format(self.worktree.head.commit.message))
        status, stdout, stderr = self.worktree.git.push('try', with_extended_output=True)
        return status, "\n".join([stdout, stderr])


class TryFuzzyCommit(TryCommit):
    def __init__(self, git_gecko, worktree, tests_by_type, rebuild, hacks=True, **kwargs):
        super(TryFuzzyCommit, self).__init__(git_gecko, worktree, tests_by_type, rebuild,
                                             hacks=True, **kwargs)
        self.exclude = self.extra_args.get("exclude", ["pgo", "ccov", "macosx"])
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
        mach = Mach(self.worktree.working_dir)
        query = self.query

        logger.info("Pushing to try with fuzzy query: %s" % query)

        if self.tests_by_type:
            raise NotImplementedError

        args = ["fuzzy", "-q", query, "--artifact"]
        if self.rebuild:
            args.append("--rebuild")
            args.append(self.rebuild)
        try:
            output = mach.try_(*args, stderr=subprocess.STDOUT)
            return 0, output
        except subprocess.CalledProcessError as e:
            return e.returncode, e.output


class TryPush(base.ProcessData):
    """A try push is represented by an annotated tag with a path like

    try/<pr_id>/<status>/<id>

    Where id is a number to indicate the Nth try push for this PR.
    """
    obj_type = "try"
    statuses = ("open", "complete", "infra-fail")
    status_transitions = [("open", "complete"),
                          ("open", "infra-fail")]
    _retrigger_count = 6

    @classmethod
    def create(cls, sync, affected_tests=None, stability=False, hacks=True,
               try_cls=TrySyntaxCommit, **kwargs):
        logger.info("Creating try push for PR %s" % sync.pr)
        if not tree.is_open("try"):
            logger.info("try is closed")
            # TODO make this auto-retry
            raise AbortError("Try is closed")

        git_work = sync.gecko_worktree.get()

        rebuild_count = 0 if not stability else 10
        with try_cls(sync.git_gecko, git_work, affected_tests, rebuild_count, hacks=hacks,
                     **kwargs) as c:
            try_rev = c.push()

        data = {
            "try-rev": try_rev,
            "stability": stability,
        }
        process_name = base.ProcessName.with_seq_id(sync.git_gecko,
                                                    "syncs",
                                                    cls.obj_type,
                                                    sync.sync_type,
                                                    "open",
                                                    getattr(sync, sync.obj_id))
        rv = super(TryPush, cls).create(sync.git_gecko, process_name, data)

        env.bz.comment(sync.bug,
                       "Pushed to try%s %s" %
                       (" (stability)" if stability else "",
                        cls.treeherder_url(try_rev)))

        return rv

    @classmethod
    def load_all(cls, git_gecko, sync_type, sync_id, status="open", seq_id="*"):
        return [cls.load(git_gecko, ref.path)
                for ref in base.DataRefObject.load_all(git_gecko,
                                                       obj_type=cls.obj_type,
                                                       subtype=sync_type,
                                                       status=status,
                                                       obj_id=sync_id,
                                                       seq_id=seq_id)]

    @classmethod
    def for_commit(cls, git_gecko, sha1, sync_type="*", sync_id="*", status="open"):
        pushes = cls.load_all(git_gecko, sync_type, sync_id, status=status)
        for push in reversed(pushes):
            if push.try_rev == sha1:
                logger.info("Found try push %r for rev %s" % (push, sha1))
                return push
        logger.info("No try push for rev %s" % (sha1,))

    @classmethod
    def for_taskgroup(cls, git_gecko, taskgroup_id, sync_type="*", status="open"):
        pushes = cls.load_all(git_gecko, sync_type, "*", status=status)
        for push in reversed(pushes):
            if push.taskgroup_id == taskgroup_id:
                return push

    @staticmethod
    def treeherder_url(try_rev):
        return "https://treeherder.mozilla.org/#/jobs?repo=try&revision=%s" % try_rev

    def __eq__(self, other):
        if not (hasattr(other, "_ref") and hasattr(other._ref, "_process_name")):
            return False
        for attr in ["obj_type", "subtype", "obj_id", "seq_id"]:
            if getattr(self._ref._process_name, attr) != getattr(other._ref._process_name, attr):
                return False
        return True

    @property
    def try_rev(self):
        return self.get("try-rev")

    @property
    def taskgroup_id(self):
        return self.get("taskgroup-id")

    @taskgroup_id.setter
    def taskgroup_id(self, value):
        self["taskgroup-id"] = value

    @property
    def status(self):
        return self._ref._process_name.status

    @status.setter
    def status(self, value):
        if value not in self.statuses:
            raise ValueError("Unrecognised status %s" % value)
        current = self._ref._process_name.status
        if current == value:
            return
        if (current, value) not in self.status_transitions:
            raise ValueError("Tried to change status from %s to %s" % (current, value))
        self._ref._process_name.status = value

    def delete(self):
        self._ref._process_name.delete()

    def sync(self, git_gecko, git_wpt):
        process_name = self._ref._process_name
        syncs = get_syncs(git_gecko, git_wpt,
                          process_name.subtype,
                          process_name.obj_id)
        if len(syncs) == 0:
            return None
        if len(syncs) == 1:
            return syncs[0]
        for item in syncs:
            if item.status == "open":
                return item
        raise ValueError("Got multiple syncs and none were open")

    @property
    def stability(self):
        """Is the current try push a stability test"""
        return self["stability"]

    def wpt_tasks(self, force_update=False):
        """Get a list of all the taskcluster tasks for web-platform-tests
        jobs associated with the current try push.

        :param bool force_update: Force the tasks to be refreshed from the
                                  server
        :return: List of tasks
        """
        if not force_update and "tasks" in self._data:
            return self._data["tasks"]

        try:
            wpt_tasks = tc.get_wpt_tasks(self.taskgroup_id)
        except ValueError:
            # If this happens we may have the wrong taskgroup id
            task_id = tc.normalize_task_id(self.taskgroup_id)
            if task_id != self.taskgroup_id:
                self.taskgroup_id = task_id
            wpt_tasks = tc.get_wpt_tasks(self.taskgroup_id)
        err = None
        if not len(wpt_tasks):
            err = "No wpt tests found. Check decision task {}".format(self.taskgroup_id)
        # TODO: it seems we don't have a great way of sanity checking that all the expected
        # jobs ran to completion
        # if any(item.get("status", {}).get("state", None)
        #     # TODO check for unscheduled versus failed versus exception
        #     def get_task_names(tasks):
        #         return set(item["task"]["metadata"]["name"] for item in tasks)

        #     missing = get_task_names(wpt_tasks) - get_task_names(wpt_completed)
        #     err = ("The tests didn't all run; perhaps a build failed?\nMissing:%s" %
        #            (",".join(missing)))

        if err:
            logger.debug(err)
            # TODO retry? manual intervention?
            self.status = "infra-fail"
            raise AbortError(err)

        self._data["tasks"] = wpt_tasks
        return wpt_tasks

    def retrigger_failures(self, count=_retrigger_count):
        task_states = self.wpt_states()

        def is_failure(task_data):
            states = task_data["states"]
            return states[tc.FAIL] > 0 or states[tc.EXCEPTION] > 0

        failures = [data["task_id"] for name, data in task_states.iteritems() if is_failure(data)]
        count = 0
        for task_id in failures:
            jobs = auth_tc.retrigger(task_id, count=count)
            if jobs:
                count += len(jobs)
        return count

    def wpt_states(self, force_update=False):
        if not force_update and "task_states" in self._data:
            return self._data["task_states"]

        task_states = tc.count_task_state_by_name(self.wpt_tasks(force_update))
        self._data["task_states"] = task_states
        return task_states

    def retriggered_wpt_states(self, force_update=False):
        # some retrigger requests may have failed, and we try to ignore
        # manual/automatic retriggers made outside of wptsync
        threshold = max(1, self._retrigger_count / 2)
        task_counts = self.wpt_states(force_update)
        return {name: data for name, data in task_counts.iteritems()
                if sum(data["states"].itervalues()) > threshold}

    def success(self):
        """Check if all the wpt tasks in a try push ended with a successful status"""
        wpt_tasks = self.wpt_tasks()
        return all(task.get("status", {}).get("state") == tc.SUCCESS for task in wpt_tasks)

    def success_rate(self):
        wpt_tasks = self.wpt_tasks()

        def is_success(task):
            return task.get("status", {}).get("state") == tc.SUCCESS

        success = [task for task in wpt_tasks if is_success(task)]
        return float(len(success)) / len(wpt_tasks)

    def download_logs(self, raw=True, report=True, exclude=None):
        """Download all the logs for the current try push

        :return: List of paths to raw logs
        """
        if exclude is None:
            exclude = []

        def excluded(t):
            # if a name is on the excluded list, only download SUCCESS job logs
            name = t.get("task", {}).get("metadata", {}).get("name")
            state = t.get("status", {}).get("state")
            return name in exclude and state != tc.SUCCESS

        wpt_tasks = self.wpt_tasks()
        if self.try_rev is None:
            if wpt_tasks:
                logger.info("Got try push with no rev; setting it from a task")
                self._data["try-rev"] = wpt_tasks[0]["task"]["payload"]["env"]["GECKO_HEAD_REV"]
        wpt_tasks = [t for t in wpt_tasks if not excluded(t)]
        logger.info("Downloading logs for try revision %s" % self.try_rev)
        dest = os.path.join(env.config["root"], env.config["paths"]["try_logs"],
                            "try", self.try_rev)
        tc.download_logs(wpt_tasks, dest, raw=raw, report=report)
        return wpt_tasks

    def download_raw_logs(self, exclude=None):
        wpt_tasks = self.download_logs(raw=True, exclude=exclude)
        raw_logs = []
        for task in wpt_tasks:
            for run in task.get("status", {}).get("runs", []):
                log = run.get("_log_paths", {}).get("wpt_raw.log")
                if log:
                    raw_logs.append(log)
        return raw_logs
