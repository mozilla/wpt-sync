import traceback

import downstream
import log
import landing
import tc
import trypush
import upstream
import worktree
from env import Environment
from gitutils import pr_for_commit, update_repositories, gecko_repo
from load import get_pr_sync

env = Environment()

logger = log.get_logger(__name__)


def log_exceptions(f):
    def inner(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except Exception as e:
            logger.critical("%s failed with error:%s" % (f.__name__, traceback.format_exc(e)))
            # For now:
            raise

    inner.__name__ = f.__name__
    inner.__doc__ = f.__doc__
    return inner


class Handler(object):
    def __init__(self, config):
        self.config = config

    def __call__(self, git_gecko, git_wpt, body):
        raise NotImplementedError


def handle_pr(git_gecko, git_wpt, event):
    pr_id = event["number"]

    env.gh_wpt.load_pull(event["pull_request"])

    sync = get_pr_sync(git_gecko, git_wpt, pr_id)

    if not sync:
        # If we don't know about this sync then it's a new thing that we should
        # set up state for
        # TODO: maybe want to create a new sync here irrespective of the event
        # type because we missed some events.
        if event["action"] == "opened":
            downstream.new_wpt_pr(git_gecko, git_wpt, event["pull_request"])
    else:
        if isinstance(sync, downstream.DownstreamSync):
            update_func = downstream.update_pr
        elif isinstance(sync, upstream.UpstreamSync):
            update_func = upstream.update_pr
        else:
            return

        merge_sha = (event["pull_request"]["merge_commit_sha"]
                     if event["pull_request"]["merged"] else None)
        update_func(git_gecko,
                    git_wpt,
                    sync,
                    event["action"],
                    merge_sha,
                    event["pull_request"]["base"]["sha"])


def handle_status(git_gecko, git_wpt, event):
    if event["context"] == "upstream/gecko":
        # Never handle changes to our own status
        return

    update_repositories(None, git_wpt, False)

    rev = event["sha"]
    # First check if the PR is head of any pull request
    pr_id = pr_for_commit(git_wpt, rev)

    if not pr_id:
        # This usually happens if we got behind, so the commit is no longer the latest one
        # There are a few possibilities for what happened:
        # * Something new was pushed. In that case ignoring this message is fine
        # * The PR got merged in a way that changes the SHAs. In that case we assume that
        #   the syncc will get triggered later like when there's a push for the commit
        logger.warning("Got status for commit %s which is not the current HEAD of any PR\n"
                       "context: %s url: %s state: %s" %
                       (rev, event["context"], event["target_url"], event["state"]))
        return
    else:
        logger.info("Got status for commit %s from PR %s\n"
                    "context: %s url: %s state: %s" %
                    (rev, pr_id, event["context"], event["target_url"], event["state"]))

    sync = get_pr_sync(git_gecko, git_wpt, pr_id)

    if not sync:
        # Presumably this is a thing we ought to be downstreaming, but missed somehow
        logger.info("Got a status update for PR %s which is unknown to us; starting downstreaming" %
                    pr_id)
        from update import schedule_pr_task
        schedule_pr_task("opened", env.gh_wpt.get_pull(pr_id))

    if isinstance(sync, upstream.UpstreamSync):
        upstream.commit_status_changed(git_gecko, git_wpt, sync, event["context"], event["state"],
                                       event["target_url"], event["sha"])
    elif isinstance(sync, downstream.DownstreamSync):
        downstream.commit_status_changed(git_gecko, git_wpt, sync, event["context"], event["state"],
                                         event["target_url"], event["sha"])


def handle_push(git_gecko, git_wpt, event):
    update_repositories(None, git_wpt, False)
    landing.wpt_push(git_gecko, git_wpt, [item["id"] for item in event["commits"]])


def handle_pull_request_review(git_gecko, git_wpt, event):
    if event["action"] != "submitted":
        return
    if event["review"]["state"] != "approved":
        return
    pr_id = event["pull_request"]["number"]

    sync = get_pr_sync(git_gecko, git_wpt, pr_id)

    if not sync or not isinstance(sync, downstream.DownstreamSync):
        return

    downstream.pull_request_approved(git_gecko, git_wpt, sync)


class GitHubHandler(Handler):
    dispatch_event = {
        "pull_request": handle_pr,
        "status": handle_status,
        "push": handle_push,
        "pull_request_review": handle_pull_request_review
    }

    def __call__(self, git_gecko, git_wpt, body):
        handler = self.dispatch_event[body["event"]]
        if handler:
            return handler(git_gecko, git_wpt, body["payload"])
        # TODO: other events to check if we can merge a PR
        # because of some update


class PushHandler(Handler):
    def __call__(self, git_gecko, git_wpt, body):
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
        update_repositories(git_gecko, git_wpt, include_autoland=True, wait_gecko_commit=rev)
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


class TaskHandler(Handler):
    def __call__(self, git_gecko, git_wpt, body):
        sha1 = body["origin"]["revision"]
        task_id = tc.normalize_task_id(body["taskId"])
        result = body["result"]

        try_push = trypush.TryPush.for_commit(git_gecko, sha1)
        if not try_push:
            logger.debug("No try push for SHA1 %s" % (sha1,))
            return

        logger.info("Setting taskgroup id for try push %r to %s" % (try_push, task_id))
        try_push.taskgroup_id = task_id

        failed_builds = len(try_push.failed_builds())
        if result != "success" or failed_builds:
            try_push.status = "complete"
            try_push.infra_fail = True
            sync = try_push.sync(git_gecko, git_wpt)
            message = ("Decision task got status %s for task %s%s" %
                       (result, sha1, " PR %s" % sync.pr if sync and sync.pr else ""))
            if failed_builds:
                message = "The try push includes build failures. %s" % message
            logger.error(message)
            if sync and sync.bug:
                # TODO this is commenting too frequently on bugs
                env.bz.comment(sync.bug,
                               "Try push failed: decision task returned error")


class TaskGroupHandler(Handler):
    def __call__(self, git_gecko, git_wpt, body):
        taskgroup_id = tc.normalize_task_id(body["taskGroupId"])

        try_push = trypush.TryPush.for_taskgroup(git_gecko, taskgroup_id)
        if not try_push:
            logger.debug("No try push for taskgroup %s" % taskgroup_id)
            # this is not one of our try_pushes
            return
        logger.info("Found try push for taskgroup %s" % taskgroup_id)
        sync = try_push.sync(git_gecko, git_wpt)

        if isinstance(sync, downstream.DownstreamSync):
            downstream.try_push_complete(git_gecko, git_wpt, try_push, sync)
        elif isinstance(sync, landing.LandingSync):
            landing.try_push_complete(git_gecko, git_wpt, try_push, sync)


class LandingHandler(Handler):
    def __call__(self, git_gecko, git_wpt):
        return landing.update_landing(git_gecko, git_wpt)


class CleanupHandler(Handler):
    def __call__(self, git_gecko, git_wpt):
        logger.info("Running cleanup")
        worktree.cleanup(git_gecko, git_wpt)
        tc.cleanup()
