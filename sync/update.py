import downstream
import log
import tasks
import upstream
from env import Environment
from handlers import get_pr_sync
from gitutils import update_repositories
from tasks import get_handlers, setup

env = Environment()
logger = log.get_logger("command")


def handle_sync(task, body):
    handlers = get_handlers()
    if task in handlers:
        logger.info("Running task %s" % task)
        git_gecko, git_wpt = setup()
        try:
            handlers[task](git_gecko, git_wpt, body)
        except Exception:
            logger.error(body)
            raise
    else:
        logger.error("No handler for %s" % task)


def construct_event(name, payload, **kwargs):
    event = {"event": name, "payload": payload}
    event.update(**kwargs)
    return event


def schedule_pr_task(action, pr):
    event = construct_event("pull_request",
                            {"action": action, "number": pr.number, "pull_request": pr.raw_data})
    logger.info("Action %s for pr %s" % (action, pr.number))
    args = ("github", event)
    if False:
        tasks.handle.apply_async(args)
    else:
        handle_sync(*args)


def schedule_status_task(commit, status):
    event = construct_event("status",
                            {"sha": commit.sha,
                             "context": status.context,
                             "state": status.state,
                             "description": status.description,
                             "target_url": status.target_url,
                             "branches": []  # Hopefully we don't use this
                             })
    logger.info("Status changed for commit %s" % commit.sha)
    args = ("github", event)
    if False:
        tasks.handle.apply_async(args)
    else:
        handle_sync(*args)


def update_for_status(pr):
    commits = pr.get_commits()
    head = commits.reversed[0]
    for status in head.get_statuses():
        if (status.context == "continuous-integration/travis-ci/pr" and
            status.state != "pending"):
            schedule_status_task(head, status)
            return


def update_pr(git_gecko, git_wpt, pr):
    sync = get_pr_sync(git_gecko, git_wpt, pr.number)
    if not sync:
        # If this looks like something that came from gecko, create
        # a corresponding sync
        upstream_sync = upstream.UpstreamSync.from_pr(git_gecko, git_wpt, pr.number, pr.body)
        if upstream_sync is not None:
            upstream_sync.update_status(pr.state, pr.merged)
        else:
            if pr.state != "open":
                # If this landed, the landing code will notice
                # we don't have results for it and kick off that process
                # so we don't need those results here
                return
            schedule_pr_task("opened", pr)
            update_for_status(pr)
    elif isinstance(sync, downstream.DownstreamSync):
        if pr.state == "open":
            if pr.head.sha != sync.wpt_commits.head:
                # Upstream has different commits, so run a push handler
                schedule_pr_task("push", pr)
            update_for_status(pr)
        elif pr.state == "closed" and not pr.merged:
            sync.state = "closed"
    elif isinstance(sync, upstream.UpstreamSync):
        sync.update_state(pr.state, pr.merged)


def update_from_github(git_gecko, git_wpt):
    update_repositories(git_gecko, git_wpt, True)
    for pr in env.gh_wpt.get_pulls():
        update_pr(git_gecko, git_wpt, pr)
