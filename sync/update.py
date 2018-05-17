import downstream
import landing
import log
import tc
import upstream
from env import Environment
from load import get_bug_sync, get_pr_sync
from gitutils import update_repositories
from pipeline import AbortError

env = Environment()
logger = log.get_logger(__name__)


def handle_sync(task, body):
    from tasks import get_handlers, setup

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
    for status in head.get_combined_status().statuses:
        if (status.context != "upstream/gecko"):
            schedule_status_task(head, status)
            return


def update_for_action(pr, action):
    event = construct_event("pull_request",
                            {"action": action,
                             "number": pr.number,
                             "pull_request": pr.raw_data})
    logger.info("Running action %s for PR %s" % (action, pr.number))
    handle_sync("github", event)


def convert_rev(git_gecko, rev):
    try:
        git_rev = git_gecko.cinnabar.hg2git(rev)
        hg_rev = rev
    except ValueError:
        # This was probably a git rev
        try:
            hg_rev = git_gecko.cinnabar.git2hg(rev)
        except ValueError:
            raise ValueError("%s is not a valid git or hg rev" % (rev,))
        git_rev = rev
    return git_rev, hg_rev


def update_push(git_gecko, git_wpt, rev, base_rev=None, processes=None):
    git_rev, hg_rev = convert_rev(git_gecko, rev)

    if base_rev is not None:
        _, hg_rev_base = convert_rev(git_gecko, base_rev)
    else:
        hg_rev_base = None

    if not git_gecko.is_ancestor(git_rev, env.config["gecko"]["refs"]["central"]):
        routing_key = "mozilla-central"
    if not git_gecko.is_ancestor(git_rev, env.config["gecko"]["refs"]["autoland"]):
        routing_key = "integration/mozilla-inbound"
    else:
        routing_key = "integration/autoland"

    kwargs = {"_wptsync": {}}

    if hg_rev_base is not None:
        kwargs["_wptsync"]["base_rev"] = hg_rev_base

    if processes is not None:
        kwargs["_wptsync"]["processes"] = processes

    event = construct_event("push", {"data": {"heads": [hg_rev]}},
                            _meta={"routing_key": routing_key},
                            **kwargs)

    args = ("push", event)
    handle_sync(*args)


def update_pr(git_gecko, git_wpt, pr):
    sync = get_pr_sync(git_gecko, git_wpt, pr.number)

    if sync and sync.status == "complete":
        logger.info("Sync already landed")
        return

    sync_point = landing.load_sync_point(git_gecko, git_wpt)

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
        if len(sync.wpt_commits) == 0:
            sync.update_wpt_commits()

        if not sync.bug and not (pr.state == "closed" and not pr.merged):
            sync.create_bug(git_wpt, pr.number, pr.title, pr.body)

        if pr.state == "open" or pr.merged:
            if pr.head.sha != sync.wpt_commits.head:
                # Upstream has different commits, so run a push handler
                schedule_pr_task("push", pr)

            elif sync.latest_valid_try_push:
                if not sync.latest_valid_try_push.taskgroup_id:
                    update_taskgroup_ids(git_gecko, git_wpt)

                if (sync.latest_valid_try_push.taskgroup_id and
                    not sync.latest_valid_try_push.status == "complete"):
                    update_tasks(git_gecko, git_wpt, sync=sync)

        if not pr.merged:
            update_for_status(pr)
        else:
            update_for_action(pr, "closed")

    elif isinstance(sync, upstream.UpstreamSync):
        merge_sha = pr.merge_commit_sha if pr.merged else None
        sync.update_status(pr.state, merge_sha)
        sync.try_land_pr()
        if merge_sha:
            if git_wpt.is_ancestor(merge_sha, sync_point["upstream"]):
                # This sync already landed, so it should be finished
                sync.finish()
            else:
                if sync.status == "complete":
                    # We bypass the setter here because we have some cases where the
                    # status must go from complete to wpt-merged which is otherwise forbidden
                    sync._process_name.status = "wpt-merged"
                else:
                    sync.status = "wpt-merged"


def update_bug(git_gecko, git_wpt, bug):
    syncs = get_bug_sync(git_gecko, git_wpt, bug)
    if not syncs:
        raise ValueError("No sync for bug %s" % bug)

    for status in upstream.UpstreamSync.statuses:
        syncs_for_status = syncs.get(status)
        if not syncs_for_status:
            continue
        for sync in syncs_for_status:
            if isinstance(sync, upstream.UpstreamSync):
                upstream.update_sync(git_gecko, git_wpt, sync)
            else:
                logger.warning("Can't update sync %s" % sync)


def update_from_github(git_gecko, git_wpt, sync_classes, statuses=None):
    if statuses is None:
        statuses = ["*"]
    update_repositories(git_gecko, git_wpt, True)
    for cls in sync_classes:
        for status in statuses:
            if status != "*" and status not in cls.statuses:
                continue
            syncs = cls.load_all(git_gecko,
                                 git_wpt,
                                 status=status,
                                 obj_id="*")
            for sync in syncs:
                if not sync.pr:
                    continue
                logger.info("Updating sync for PR %s" % sync.pr)
                pr = env.gh_wpt.get_pull(sync.pr)
                update_pr(git_gecko, git_wpt, pr)


def update_taskgroup_ids(git_gecko, git_wpt):
    for sync in downstream.DownstreamSync.load_all(git_gecko, git_wpt):
        try_push = sync.latest_try_push

        if not try_push:
            continue

        if not try_push.taskgroup_id:
            taskgroup_id, status = tc.get_taskgroup_id("try", try_push.try_rev)
            handle_sync("task", {"origin": {"revision": try_push.try_rev},
                                 "taskId": taskgroup_id,
                                 "result": status})


def update_tasks(git_gecko, git_wpt, pr_id=None, sync=None):
    logger.info("Running update_tasks%s" % ("for PR %s" % pr_id if pr_id else ""))

    if not sync:
        obj_id = str(pr_id) or "*"
        syncs = [item for item in
                 landing.LandingSync.load_all(git_gecko, git_wpt) +
                 downstream.DownstreamSync.load_all(git_gecko, git_wpt, obj_id=obj_id)
                 if pr_id is None or sync.pr == pr_id]
    else:
        syncs = [sync]

    for sync in syncs:
        try_push = sync.latest_try_push
        if try_push and try_push.taskgroup_id:
            try:
                handle_sync("taskgroup", {"taskGroupId": try_push.taskgroup_id})
            except AbortError:
                pass


def retrigger(git_gecko, git_wpt, unlandable_prs):
    from base import LandableStatus

    errors = []
    for pr_id, commits, status in unlandable_prs:
        if status in (LandableStatus.ready,
                      LandableStatus.skip,
                      LandableStatus.upstream,
                      LandableStatus.no_pr):
            continue

        try:
            logger.info("Retriggering %s (status %s)" % (pr_id, status))
            pr = env.gh_wpt.get_pull(int(pr_id))
            update_pr(git_gecko, git_wpt, pr)
        except Exception:
            errors.append(pr_id)

    return errors
