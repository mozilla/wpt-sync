import traceback
import urlparse

import downstream
import log
import push
import upstream
import worktree
from gitutils import is_ancestor, pr_for_commit
from env import Environment

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


def get_pr_sync(git_gecko, git_wpt, pr_id):
    sync = None
    sync = downstream.DownstreamSync.for_pr(git_gecko, git_wpt, pr_id)
    if not sync:
        sync = upstream.UpstreamSync.for_pr(git_gecko, git_wpt, pr_id)
    return sync


def get_bug_sync(git_gecko, git_wpt, bug_number):
    sync = None
    sync = upstream.UpstreamSync.for_bug(git_gecko, git_wpt, bug_number)
    if not sync:
        sync = downstream.DownstreamSync.for_bug(git_gecko, git_wpt, bug_number)
    return sync


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
        sync.update_status(event["action"], event["pull_request"]["merged"])


def handle_status(git_gecko, git_wpt, event):
    if event["context"] == "upstream/gecko":
        # Never handle changes to our own status
        return

    rev = event["sha"]
    pr_id = pr_for_commit(git_wpt, rev)

    if not pr_id:
        if not is_ancestor(git_wpt, rev, "origin/master"):
            logger.debug(event)
            logger.error("Got status for commit %s, but that isn't the head of any PR" % rev)
        return
    else:
        logger.info("Got status for commit %s from PR %s" % (rev, pr_id))

    sync = get_pr_sync(git_gecko, git_wpt, pr_id)

    if not sync:
        # Presumably this is a thing we ought to be downstreaming, but missed somehow
        # TODO: Handle this case
        logger.error("Got a status update for PR %s which is unknown to us" % pr_id)

    if isinstance(sync, upstream.UpstreamSync):
        upstream.status_changed(git_gecko, git_wpt, sync, event["context"], event["state"],
                                event["target_url"], event["sha"])
    elif isinstance(sync, downstream.DownstreamSync):
        downstream.status_changed(git_gecko, git_wpt, sync, event["context"], event["state"],
                                  event["target_url"], event["sha"])


def handle_push(git_gecko, git_wpt, event):
    push.wpt_push(git_wpt, [item["sha"] for item in event["commits"]])


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
        self.integration_repos = {}
        for repo_name, url in config["sync"]["integration"].iteritems():
            url_parts = urlparse.urlparse(url)
            url = urlparse.urlunparse(("https",) + url_parts[1:])
            self.integration_repos[url] = repo_name
        self.landing_repo = config["sync"]["landing"]

    def __call__(self, git_gecko, git_wpt, body):
        data = body["payload"]["data"]
        repo_url = data["repo_url"]
        # Not sure if it's everey possible to get multiple heads here in a way that
        # matters for us
        rev = data["heads"][0]
        logger.debug("Commit landed in repo %s" % repo_url)
        if repo_url in self.repos:
            repo_name = self.integration_repos[repo_url]
            upstream.integration_commit(git_gecko, git_wpt, repo_name, rev)


class TaskHandler(Handler):
    def __call__(self, git_gecko, git_wpt, body):
        if not (body.get("origin")
                and body["origin"].get("revision")
                and body.get("taskId")):
            logger.debug("Oh no, this payload doesn't have the format we expect!"
                         "Need 'revision' and 'taskId'. Got:\n{}\n".format(body))
            return

        return downstream.update_taskgroup(git_gecko,
                                           git_wpt,
                                           body["origin"]["revision"],
                                           body["taskId"],
                                           body["result"])


class TaskGroupHandler(Handler):
    def __call__(self, git_gecko, git_wpt, body):
        return downstream.on_taskgroup_resolved(git_gecko,
                                                git_wpt,
                                                body["taskGroupId"])


class LandingHandler(Handler):
    def __call__(self, git_gecko, git_wpt):
        return push.land_to_gecko(git_gecko, git_wpt)


class CleanupHandler(Handler):
    def __call__(self, session, git_gecko, git_wpt, gh_wpt, bz):
        return worktree.cleanup(self.config, session)
