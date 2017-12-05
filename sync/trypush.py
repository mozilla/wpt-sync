import os
import re
from itertools import chain

import base
import log
import taskcluster
import tree
from env import Environment
from load import get_syncs
from pipeline import AbortError

logger = log.get_logger(__name__)
env = Environment()
rev_re = re.compile("revision=(?P<rev>[0-9a-f]{40})")


class TryPush(base.ProcessData):
    """A try push is represented by a an annotated tag with a path like

    try/<pr_id>/<status>/<id>

    Where id is a number to indicate the Nth try push for this PR.
    """
    obj_type = "try"
    statuses = ("open", "complete", "infra-fail")

    @classmethod
    def create(cls, sync, affected_tests=None, stability=False):

        if not tree.is_open("try"):
            logger.info("try is closed")
            # TODO make this auto-retry
            raise AbortError("Try is closed")

        git_work = sync.gecko_worktree.get()

        rebuild_count = 0 if not stability else 10
        with TrySyntaxCommit(sync.git_gecko, git_work, affected_tests, rebuild_count) as c:
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
                       "Pushed to try%s. Results: "
                       "https://treeherder.mozilla.org/#/jobs?repo=try&revision=%s" %
                       (" (stability)" if stability else "", try_rev))

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
                return push

    @classmethod
    def for_taskgroup(cls, git_gecko, taskgroup_id, sync_type="*", status="open"):
        pushes = cls.load_all(git_gecko, sync_type, "*", status=status)
        for push in reversed(pushes):
            if push.taskgroup_id == taskgroup_id:
                return push

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
        self._ref._process_name.status = value

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
        return self["stability"]

    def wpt_tasks(self):
        try:
            wpt_completed, wpt_tasks = taskcluster.get_wpt_tasks(self.taskgroup_id)
        except ValueError:
            # If this happens we may have the wrong taskgroup id
            task_id = taskcluster.normalize_task_id(self.taskgroup_id)
            if task_id != self.taskgroup_id:
                self.taskgroup_id = task_id
                wpt_completed, wpt_tasks = taskcluster.get_wpt_tasks(self.taskgroup_id)
        err = None
        if not len(wpt_tasks):
            err = "No wpt tests found. Check decision task {}".format(self.taskgroup_id)
        if len(wpt_tasks) > len(wpt_completed):
            # TODO check for unscheduled versus failed versus exception
            def get_task_names(tasks):
                return set(item["task"]["metadata"]["name"] for item in tasks)

            missing = get_task_names(wpt_tasks) - get_task_names(wpt_completed)
            err = ("The tests didn't all run; perhaps a build failed?\nMissing:%s" %
                   (",".join(missing)))

        if err:
            logger.debug(err)
            # TODO retry? manual intervention?
            self.status = "infra-fail"
            raise AbortError(err)
        return wpt_completed, wpt_tasks

    def download_logs(self):
        wpt_completed, wpt_tasks = self.wpt_tasks()
        dest = os.path.join(env.config["root"], env.config["paths"]["try_logs"])
        return taskcluster.download_logs(wpt_completed, dest)


class TryCommit(object):
    def __init__(self, git_gecko, worktree, tests_by_type, rebuild):
        self.git_gecko = git_gecko
        self.worktree = worktree
        self.tests_by_type = tests_by_type
        self.rebuild = rebuild

    def __enter__(self):
        self.create()
        self.try_rev = self.worktree.head.commit.hexsha
        return self

    def __exit__(self, *args, **kwargs):
        assert self.worktree.head.commit.hexsha == self.try_rev
        self.worktree.head.reset("HEAD~", working_tree=True)

    def push(self):
        logger.info("Pushing to try with message:\n{}".format(self.worktree.head.commit.message))
        status, stdout, stderr = self.worktree.git.push('try', with_extended_output=True)
        rev_match = rev_re.search(stderr)
        if not rev_match:
            logger.warning("No revision found in string:\n\n{}\n".format(stderr))
            # Assume that the revision is HEAD~
            try_rev = self.git_gecko.cinnabar.git2hg(self.worktree.commit("HEAD~").hexsha)
        else:
            try_rev = rev_match.group('rev')
        if status != 0:
            logger.error("Failed to push to try.")
            # TODO retry
            raise AbortError("Failed to push to try")
        return try_rev


class TrySyntaxCommit(TryCommit):
    def create(self):
        message = self.try_message(self.tests_by_type, self.rebuild)
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
        # web-platform-tests[linux64-stylo,Ubuntu,10.10,Windows 7,Windows 8,Windows 10]
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

        platform_suffix = "[linux64-stylo,Ubuntu,10.10,Windows 7,Windows 8,Windows 10]"

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
