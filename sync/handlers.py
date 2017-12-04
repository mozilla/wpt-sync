import traceback
import urlparse

import downstream
import log
import landing
import trypush
import upstream
import worktree
from gitutils import is_ancestor, pr_for_commit, update_repositories, gecko_repo
from env import Environment
from load import get_pr_sync

env = Environment()

logger = log.get_logger("handlers")


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
        if event["action"] == "opened" and "downstream" in env.config["sync"]["enabled"]:
            downstream.new_wpt_pr(git_gecko, git_wpt, event["pull_request"])
    else:
        sync.update_status(event["action"], event["pull_request"]["merged"])


def handle_status(git_gecko, git_wpt, event):
    if event["context"] == "upstream/gecko":
        # Never handle changes to our own status
        return

    update_repositories(None, git_wpt, False)

    rev = event["sha"]
    pr_id = pr_for_commit(git_wpt, rev)

    if not pr_id:
        if not git_wpt.is_ancestor(rev, "origin/master"):
            logger.error("Got status for commit %s, but that isn't the head of any PR" % rev)
        return
    else:
        logger.info("Got status for commit %s from PR %s" % (rev, pr_id))

    sync = get_pr_sync(git_gecko, git_wpt, pr_id)

    if not sync:
        # Presumably this is a thing we ought to be downstreaming, but missed somehow
        # TODO: Handle this case
        logger.info("Got a status update for PR %s which is unknown to us; starting downstreaming" % pr_id)
        from update import schedule_pr_task
        schedule_pr_task("opened", env.gh_wpt.get_pull(pr_id))

    if isinstance(sync, upstream.UpstreamSync) and "upstream" in env.config["sync"]["enabled"]:
        upstream.status_changed(git_gecko, git_wpt, sync, event["context"], event["state"],
                                event["target_url"], event["sha"])
    elif isinstance(sync, downstream.DownstreamSync) and "downstream" in env.config["sync"]["enabled"]:
        downstream.status_changed(git_gecko, git_wpt, sync, event["context"], event["state"],
                                  event["target_url"], event["sha"])


def handle_push(git_gecko, git_wpt, event):
    landing.wpt_push(git_wpt, [item["id"] for item in event["commits"]])


class GitHubHandler(Handler):
    dispatch_event = {
        "pull_request": handle_pr,
        "status": handle_status,
        "push": handle_push,
    }

    def __call__(self, git_gecko, git_wpt, body):
        handler = self.dispatch_event[body["event"]]
        if handler:
            return handler(git_gecko, git_wpt, body["payload"])
        # TODO: other events to check if we can merge a PR
        # because of some update


class PushHandler(Handler):
    def __init__(self, config):
        self.config = config
        self.repos = {}
        for repo_name, url in config["sync"]["integration"].iteritems():
            url_parts = urlparse.urlparse(url)
            url = urlparse.urlunparse(("https",) + url_parts[1:])
            self.repos[url] = repo_name
        self.repos[config["sync"]["landing"]] = "central"

    def __call__(self, git_gecko, git_wpt, body):
        data = body["payload"]["data"]
        repo_url = data["repo_url"]
        # Not sure if it's ever possible to get multiple heads here in a way that
        # matters for us
        rev = data["heads"][0]
        update_repositories(git_gecko, None)
        logger.debug("Commit landed in repo %s" % repo_url)
        try:
            git_rev = git_gecko.cinnabar.hg2git(rev)
        except ValueError:
            pass
        else:
            if gecko_repo(git_gecko, git_rev) is None:
                logger.info("Skipping commit as it isn't in a branch we track")
                return
        print repo_url, repo_url in self.repos
        if repo_url in self.repos:
            repo_name = self.repos[repo_url]
            if "upstream" in env.config["sync"]["enabled"]:
                upstream.push(git_gecko, git_wpt, repo_name, rev)


class TaskHandler(Handler):
    def __call__(self, git_gecko, git_wpt, body):
        if not (body.get("origin")
                and body["origin"].get("revision")
                and body.get("taskId")):
            logger.debug("Oh no, this payload doesn't have the format we expect!"
                         "Need 'revision' and 'taskId'. Got:\n{}\n".format(body))
            return

        sha1 = body["origin"]["revision"]
        task_id = body["taskId"]
        result = body["result"]

        try_push = trypush.TryPush.for_commit(git_gecko, sha1)
        if not try_push:
            return

        try_push.taskgroup_id = task_id

        if result != "success":
            try_push.status = "infra-fail"
            sync = try_push.sync(git_gecko, git_wpt)
            if sync and sync.bug:
                env.bz.comment(try_push.sync.bug,
                               "Try push failed: decision task returned error")


class TaskGroupHandler(Handler):
    def __call__(self, git_gecko, git_wpt, body):
        taskgroup_id = body["taskGroupId"]

        try_push = trypush.TryPush.for_taskgroup(git_gecko, taskgroup_id)
        if not try_push:
            # this is not one of our try_pushes
            return
        sync = try_push.sync(git_gecko, git_wpt)

        if isinstance(sync, downstream.DownstreamSync) and "downstream" in env.config["sync"]["enabled"]:
            downstream.try_push_complete(git_gecko, git_wpt, try_push, sync)
        elif isinstance(sync, landing.LandingSync) and "landing" in env.config["sync"]["enabled"]:
            landing.try_push_complete(git_gecko, git_wpt, try_push, sync)


class LandingHandler(Handler):
    def __call__(self, git_gecko, git_wpt):
        if "landing" in env.config["sync"]["enabled"]:
            return landing.land_to_gecko(git_gecko, git_wpt)


class CleanupHandler(Handler):
    def __call__(self, git_gecko, git_wpt):
        return worktree.cleanup(git_gecko, git_wpt)
