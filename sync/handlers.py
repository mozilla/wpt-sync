from __future__ import absolute_import
import newrelic.agent

from . import downstream
from . import log
from . import landing
from . import tc
from . import trypush
from . import update
from . import upstream
from . import worktree
from .env import Environment
from .errors import RetryableError
from .gitutils import pr_for_commit, update_repositories, gecko_repo
from .load import get_pr_sync
from .lock import SyncLock
from .notify import bugupdate

MYPY = False
if MYPY:
    from git.repo.base import Repo
    from typing import Any, Dict, Text

env = Environment()

logger = log.get_logger(__name__)


class Handler(object):
    def __init__(self, config):
        self.config = config

    def __call__(self, git_gecko, git_wpt, body):
        # type: (Repo, Repo, Dict[Text, Any]) -> None
        raise NotImplementedError


def handle_pr(git_gecko, git_wpt, event):
    # type: (Repo, Repo, Dict[Text, Any]) -> None
    newrelic.agent.set_transaction_name("handle_pr")
    pr_id = event["number"]
    newrelic.agent.add_custom_parameter("pr", pr_id)
    newrelic.agent.add_custom_parameter("action", event["action"])
    env.gh_wpt.load_pull(event["pull_request"])

    sync = get_pr_sync(git_gecko, git_wpt, pr_id)
    repo_update = event.get("_wptsync", {}).get("repo_update", True)

    if not sync:
        # If we don't know about this sync then it's a new thing that we should
        # set up state for
        # TODO: maybe want to create a new sync here irrespective of the event
        # type because we missed some events.
        if event["action"] == "opened":
            downstream.new_wpt_pr(git_gecko, git_wpt, event["pull_request"],
                                  repo_update=repo_update)
    else:
        if isinstance(sync, downstream.DownstreamSync):
            update_func = downstream.update_pr
        elif isinstance(sync, upstream.UpstreamSync):
            update_func = upstream.update_pr
        else:
            return

        merge_sha = (event["pull_request"]["merge_commit_sha"]
                     if event["pull_request"]["merged"] else None)
        merged_by = (event["pull_request"]["merged_by"]["login"] if merge_sha else None)
        with SyncLock.for_process(sync.process_name) as lock:
            assert isinstance(lock, SyncLock)
            with sync.as_mut(lock):
                update_func(git_gecko,
                            git_wpt,
                            sync,
                            event["action"],
                            merge_sha,
                            event["pull_request"]["base"]["sha"],
                            merged_by)


def handle_check_run(git_gecko, git_wpt, event):
    # type: (Repo, Repo, Dict[Text, Any]) -> None
    newrelic.agent.set_transaction_name("handle_check_run")
    if event["action"] != "completed":
        return

    check_run = event["check_run"]

    newrelic.agent.add_custom_parameter("sha", check_run["head_sha"])
    newrelic.agent.add_custom_parameter("name", check_run["name"])
    newrelic.agent.add_custom_parameter("status", check_run["status"])
    newrelic.agent.add_custom_parameter("conclusion", check_run["conclusion"])

    if check_run["name"] not in env.gh_wpt.required_checks("master"):
        logger.info("Check %s is not required" % check_run["name"])
        return

    # First check if the PR is head of any pull request
    pr_id = pr_for_commit(git_wpt, check_run["head_sha"])

    if pr_id is None:
        logger.info("Commit %s is not part of a PR" % pr_id)
        return

    repo_update = event.get("_wptsync", {}).get("repo_update", True)
    if repo_update:
        update_repositories(None, git_wpt)

    sync = get_pr_sync(git_gecko, git_wpt, pr_id)
    if not isinstance(sync, upstream.UpstreamSync):
        return

    with SyncLock.for_process(sync.process_name) as lock:
        assert isinstance(lock, SyncLock)
        with sync.as_mut(lock):
            upstream.commit_check_changed(git_gecko,
                                          git_wpt,
                                          sync)


def handle_push(git_gecko, git_wpt, event):
    # type: (Repo, Repo, Dict[Text, Any]) -> None
    newrelic.agent.set_transaction_name("handle_push")
    update_repositories(None, git_wpt)
    landing.wpt_push(git_gecko, git_wpt, [item["id"] for item in event["commits"]])


class GitHubHandler(Handler):
    dispatch_event = {
        "pull_request": handle_pr,
        "check_run": handle_check_run,
        "push": handle_push,
    }

    def __call__(self, git_gecko, git_wpt, body):
        # type: (Repo, Repo, Dict[Text, Any]) -> None
        newrelic.agent.set_transaction_name("GitHubHandler")
        handler = self.dispatch_event[body["event"]]
        newrelic.agent.add_custom_parameter("event", body["event"])
        if handler:
            return handler(git_gecko, git_wpt, body["payload"])
        # TODO: other events to check if we can merge a PR
        # because of some update


class PushHandler(Handler):
    def __call__(self, git_gecko, git_wpt, body):
        # type: (Repo, Repo, Dict[Text, Any]) -> None
        newrelic.agent.set_transaction_name("PushHandler")
        repo = body["_meta"]["routing_key"]
        if "/" in repo:
            repo_name = repo.rsplit("/", 1)[1]
        else:
            repo_name = repo

        # Commands can override the base rev and select only certain processses
        base_rev = body.get("_wptsync", {}).get("base_rev")
        processes = body.get("_wptsync", {}).get("processes")

        # Not sure if it's ever possible to get multiple heads here in a way that
        # matters for us
        rev = body["payload"]["data"]["heads"][0]
        logger.info("Handling commit %s to repo %s" % (rev, repo))

        newrelic.agent.add_custom_parameter("repo", repo)
        newrelic.agent.add_custom_parameter("rev", rev)

        update_repositories(git_gecko, git_wpt, wait_gecko_commit=rev)
        try:
            git_rev = git_gecko.cinnabar.hg2git(rev)
        except ValueError:
            pass
        else:
            if gecko_repo(git_gecko, git_rev) is None:
                logger.info("Skipping commit as it isn't in a branch we track")
                return
        if processes is None or "upstream" in processes:
            upstream.gecko_push(git_gecko, git_wpt, repo_name, rev, base_rev=base_rev)
        if processes is None or "landing" in processes:
            landing.gecko_push(git_gecko, git_wpt, repo_name, rev, base_rev=base_rev)


class DecisionTaskHandler(Handler):
    """Handler for the task associated with a task completing."""

    complete_states = frozenset(["completed", "failed", "exception"])

    def __call__(self, git_gecko, git_wpt, body):
        # type: (Repo, Repo, Dict[Text, Any]) -> None
        newrelic.agent.set_transaction_name("DecisionTaskHandler")
        task_id = body["status"]["taskId"]
        taskgroup_id = body["status"]["taskGroupId"]

        msg = "Expected kind decision-task, got %s" % body["task"]["tags"]["kind"]
        assert body["task"]["tags"]["kind"] == "decision-task", msg

        newrelic.agent.add_custom_parameter("tc_task", task_id)
        newrelic.agent.add_custom_parameter("tc_taskgroup", taskgroup_id)

        state = body["status"]["state"]
        newrelic.agent.add_custom_parameter("state", state)

        # Enforce the invariant that the taskgroup id is not set until
        # the decision task is complete. This allows us to determine if a
        # try push should have the expected wpt tasks just by checking if
        # this is set
        if state not in self.complete_states:
            logger.info("Decision task is not yet complete, status %s" % state)
            return

        task = tc.get_task(task_id)
        if task is None:
            raise ValueError("Failed to get task for task_id %s" % task_id)

        sha1 = task.get("payload", {}).get("env", {}).get("GECKO_HEAD_REV")

        if sha1 is None:
            raise ValueError("Failed to get commit sha1 from task message")

        if state == "exception":
            run_id = body["runId"]
            runs = body.get("status", {}).get("runs", [])
            if 0 <= run_id < len(runs):
                reason = runs[run_id].get("reasonResolved")
                if reason in ["superseded",
                              "claim-expired",
                              "worker-shutdown",
                              "intermittent-task"]:
                    logger.info("Task %s had an exception for reason %s, "
                                "assuming taskcluster will retry" %
                                (task_id, reason))
                    return

        try_push = trypush.TryPush.for_commit(git_gecko, sha1)
        if not try_push:
            logger.debug("No try push for SHA1 %s taskId %s" % (sha1, task_id))
            # This could be a race condition if the decision task completes before this
            # task is in the index
            raise RetryableError("Got a wptsync task with no corresponding try push")

        with SyncLock.for_process(try_push.process_name) as lock:
            assert isinstance(lock, SyncLock)
            with try_push.as_mut(lock):
                # If we retrigger, we create a new taskgroup, with id equal to the new task_id.
                # But the retriggered decision task itself is still in the original taskgroup
                if state == "completed":
                    logger.info("Setting taskgroup id for try push %r to %s" %
                                (try_push, taskgroup_id))
                    try_push.taskgroup_id = taskgroup_id
                elif state in ("failed", "exception"):
                    sync = try_push.sync(git_gecko, git_wpt)
                    message = ("Decision task got status %s for task %s%s" %
                               (state, sha1, " PR %s" % sync.pr if sync and sync.pr else ""))
                    logger.error(message)
                    taskgroup = tc.TaskGroup(task["taskGroupId"])
                    if len(taskgroup.view(
                            lambda x: x["task"]["metadata"]["name"] == "Gecko Decision Task")) > 5:
                        try_push.status = "complete"
                        try_push.infra_fail = True
                        try_push.taskgroup_id = taskgroup_id
                        if sync and sync.bug:
                            env.bz.comment(
                                sync.bug,
                                "Try push failed: decision task %s returned error" % task_id)
                    else:
                        logger.info("Retriggering decision task for sync %s" %
                                    (sync.process_name,))
                        client = tc.TaskclusterClient()
                        client.retrigger(task_id)


class TryTaskHandler(Handler):
    """Handler for the task associated with a try push task completing."""

    def __call__(self, git_gecko, git_wpt, body):
        # type: (Repo, Repo, Dict[Text, Any]) -> None
        newrelic.agent.set_transaction_name("TryTaskHandler")
        taskgroup_id = body["status"]["taskGroupId"]
        newrelic.agent.add_custom_parameter("tc_taskgroup", taskgroup_id)

        try_push = trypush.TryPush.for_taskgroup(git_gecko, taskgroup_id)
        if not try_push:
            logger.debug("No try push for taskgroup %s" % taskgroup_id)
            # this is not one of our try_pushes
            return

        if try_push.status == "complete":
            return

        logger.info("Found try push for taskgroup %s" % taskgroup_id)

        # Check if the taskgroup has all tasks complete, excluding unscheduled tasks.
        # This allows us to tell if the taskgroup is complete (per the treeherder definition)
        # even when there are tasks that won't be scheduled because the task they depend on
        # failed. Otherwise we'd have to wait until those unscheduled tasks time out, which
        # usually takes 24hr
        if try_push.taskgroup_id is None:
            with SyncLock.for_process(try_push.process_name) as lock:
                assert isinstance(lock, SyncLock)
                with try_push.as_mut(lock):
                    try_push.taskgroup_id = taskgroup_id
        tasks = try_push.tasks()
        if tasks.complete(allow_unscheduled=True):
            taskgroup_complete(git_gecko, git_wpt, taskgroup_id, try_push)


class TaskGroupHandler(Handler):
    def __call__(self, git_gecko, git_wpt, body):
        # type: (Repo, Repo, Dict[Text, Any]) -> None
        newrelic.agent.set_transaction_name("TaskGroupHandler")
        taskgroup_id = tc.normalize_task_id(body["taskGroupId"])

        newrelic.agent.add_custom_parameter("tc_task", taskgroup_id)

        try_push = trypush.TryPush.for_taskgroup(git_gecko, taskgroup_id)
        if not try_push:
            logger.debug("No try push for taskgroup %s" % taskgroup_id)
            # this is not one of our try_pushes
            return
        logger.info("Found try push for taskgroup %s" % taskgroup_id)
        taskgroup_complete(git_gecko, git_wpt, taskgroup_id, try_push)


def taskgroup_complete(git_gecko, git_wpt, taskgroup_id, try_push):
    # type: (Repo, Repo, Text, trypush.TryPush) -> None
    sync = try_push.sync(git_gecko, git_wpt)
    if not sync:
        newrelic.agent.record_custom_event("taskgroup_sync_missing", params={
            "taskgroup-id": taskgroup_id,
            "try_push": try_push,
        })
        return

    with SyncLock.for_process(sync.process_name) as lock:
        assert isinstance(lock, SyncLock)
        with sync.as_mut(lock), try_push.as_mut(lock):
            # We sometimes see the taskgroup ID being None. If it isn't set but found via its
            # taskgroup ID, it is safe to set it here.
            if try_push.taskgroup_id is None:
                logger.info("Try push for taskgroup %s does not have its ID set, setting now" %
                            taskgroup_id)
                try_push.taskgroup_id = taskgroup_id  # type: ignore
                newrelic.agent.record_custom_event("taskgroup_id_missing", params={
                    "taskgroup-id": taskgroup_id,
                    "try_push": try_push,
                    "sync": sync,
                })
            elif try_push.taskgroup_id != taskgroup_id:
                msg = ("TryPush %s, expected taskgroup ID %s, found %s instead" %
                       (try_push, taskgroup_id, try_push.taskgroup_id))
                logger.error(msg)
                exc = ValueError(msg)
                newrelic.agent.record_exception(exc=exc)
                raise exc

            if sync:
                logger.info("Updating try push for sync %r" % sync)
            if isinstance(sync, downstream.DownstreamSync):
                downstream.try_push_complete(git_gecko, git_wpt, try_push, sync)
            elif isinstance(sync, landing.LandingSync):
                landing.try_push_complete(git_gecko, git_wpt, try_push, sync)


class LandingHandler(Handler):
    def __call__(self, git_gecko, git_wpt, body):
        # type: (Repo, Repo, Dict[Text, Any]) -> None
        newrelic.agent.set_transaction_name("LandingHandler")
        landing.update_landing(git_gecko, git_wpt)


class CleanupHandler(Handler):
    def __call__(self, git_gecko, git_wpt, body):
        # type: (Repo, Repo, Dict[Text, Any]) -> None
        newrelic.agent.set_transaction_name("CleanupHandler")
        logger.info("Running cleanup")
        worktree.cleanup(git_gecko, git_wpt)
        tc.cleanup()


class RetriggerHandler(Handler):
    def __call__(self, git_gecko, git_wpt, body):
        # type: (Repo, Repo, Dict[Text, Any]) -> None
        newrelic.agent.set_transaction_name("RetriggerHandler")
        logger.info("Running retrigger")
        update_repositories(git_gecko, git_wpt)
        sync_point = landing.load_sync_point(git_gecko, git_wpt)
        prev_wpt_head = sync_point["upstream"]
        unlanded = landing.unlanded_with_type(git_gecko, git_wpt, None, prev_wpt_head)
        update.retrigger(git_gecko, git_wpt, unlanded)


class PhabricatorHandler(Handler):
    def __call__(self, git_gecko, git_wpt, body):
        # type: (Repo, Repo, Dict[Text, Any]) -> None
        newrelic.agent.set_transaction_name("PhabricatorHandler")
        logger.info('Got phab event, doing nothing: %s' % body)


class BugUpdateHandler(Handler):
    def __call__(self, git_gecko, git_wpt, body):
        # type: (Repo, Repo, Dict[Text, Any]) -> None
        newrelic.agent.set_transaction_name("BugUpdateHandler")
        logger.info("Running bug update")
        bugupdate.update_triage_bugs(git_gecko)
