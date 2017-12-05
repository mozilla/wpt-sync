import urlparse

import requests

import downstream
import log
import tasks
import upstream
from env import Environment
from load import get_bug_sync, get_pr_sync
from gitutils import update_repositories
from pipeline import AbortError
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
            if pr.state != "open" and not pr.merged:
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
        sync.update_status(pr.state, pr.merged)
        sync.try_land_pr()


def update_bug(git_gecko, git_wpt, bug):
    sync = get_bug_sync(git_gecko, git_wpt, bug)
    if not sync:
        raise ValueError("No sync for bug %s" % bug)
    elif isinstance(sync, upstream.UpstreamSync):
        upstream.update_sync(git_gecko, git_wpt, sync)
    else:
        raise ValueError("Updating sync type for bug not yet supported")


def update_from_github(git_gecko, git_wpt):
    update_repositories(git_gecko, git_wpt, True)
    for pr in env.gh_wpt.get_pulls():
        update_pr(git_gecko, git_wpt, pr)


def fetch_json(url, params=None):
    headers = {
        'Accept': 'application/json',
        'User-Agent': 'wpt-sync',
    }
    response = requests.get(url=url, params=params, headers=headers, timeout=30)
    response.raise_for_status()
    return response.json()


def update_taskgroup_ids(git_gecko, git_wpt):
    treeherder_base = "https://treeherder.mozilla.org/"
    for sync in downstream.DownstreamSync.load_all(git_gecko, git_wpt):
        try_push = sync.latest_try_push

        if not try_push:
            continue

        if not try_push.taskgroup_id:
            resultset_url = urlparse.urljoin(treeherder_base, "/api/project/try/resultset/")
            resultset_params = {
                'revision': try_push.try_rev,
            }

            revision_data = fetch_json(resultset_url, resultset_params)
            result_set = revision_data["results"][0]["id"]

            jobs_url = urlparse.urljoin(treeherder_base, "/api/project/try/jobs/")
            jobs_params = {
                'result_set_id': result_set,
                'count': 2000,
                'exclusion_profile': 'false',
                'job_type_name': "Gecko Decision Task",
            }
            jobs_data = fetch_json(jobs_url, params=jobs_params)

            if not jobs_data["results"]:
                logger.info("No decision task found for %s" % try_push.try_rev)

            if len(jobs_data["results"]) > 1:
                logger.warning("Multiple decision tasks found for %s" % try_push.try_rev)

            job_id = jobs_data["results"][-1]["id"]

            job_url = urlparse.urljoin(treeherder_base, "/api/project/try/jobs/%s/" % job_id)
            job_data = fetch_json(job_url)

            taskgroup_id = job_data["taskcluster_metadata"]["task_id"]

            handle_sync("task", {"origin": {"revision": try_push.try_rev},
                                 "taskId": taskgroup_id,
                                 "result": job_data["result"]})


def update_tasks(git_gecko, git_wpt):
    for sync in downstream.DownstreamSync.load_all(git_gecko, git_wpt):
        print sync._process_name
        try_push = sync.latest_try_push
        if try_push and try_push.taskgroup_id:
            try:
                handle_sync("taskgroup", {"taskGroupId": try_push.taskgroup_id})
            except AbortError:
                pass
