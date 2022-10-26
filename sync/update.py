from . import downstream
from . import landing
from . import log
from . import tc
from . import trypush
from . import upstream
from .env import Environment
from .load import get_bug_sync, get_pr_sync
from .lock import SyncLock
from .gitutils import update_repositories
from .errors import AbortError
from .repos import cinnabar


MYPY = False
if MYPY:
    from git import Repo
    from github.PullRequest import PullRequest
    from sync.sync import SyncProcess
    from sync.trypush import TryPush
    from typing import Any, Dict, Iterable, List, Optional, Text, Tuple, Type

env = Environment()
logger = log.get_logger(__name__)


def handle_sync(task: Text, body: Dict[Text, Any]) -> None:
    from .tasks import get_handlers, setup

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


def construct_event(name: Text, payload: Dict[Text, Any], **kwargs: Any) -> Dict[Text, Any]:
    event = {"event": name, "payload": payload}
    event.update(**kwargs)
    return event


def schedule_pr_task(action: Text, pr: PullRequest, repo_update: bool = True) -> None:
    event = construct_event("pull_request",
                            {"action": action, "number": pr.number, "pull_request": pr.raw_data},
                            _wptsync={"repo_update": repo_update})
    logger.info("Action {} for pr {}".format(action, pr.number))
    args = ("github", event)
    handle_sync(*args)


def schedule_check_run_task(head_sha: Text, name: Text, check_run: Dict[Text, Any], repo_update: bool = True) -> None:
    check_run_data = check_run.copy()
    del check_run_data["required"]
    check_run_data["name"] = name
    check_run_data["head_sha"] = head_sha
    event = construct_event("check_run",
                            {"action": "completed",
                             "check_run": check_run_data},
                            _wptsync={"repo_update": repo_update})
    logger.info("Status changed for commit %s" % head_sha)
    args = ("github", event)
    handle_sync(*args)


def update_for_status(pr: PullRequest, repo_update: bool = True) -> None:
    for name, check_run in env.gh_wpt.get_check_runs(pr.number).items():
        if check_run["required"]:
            schedule_check_run_task(pr.head.sha, name, check_run)
            return


def update_for_action(pr: PullRequest, action: Text, repo_update: bool = True) -> None:
    event = construct_event("pull_request",
                            {"action": action,
                             "number": pr.number,
                             "pull_request": pr.raw_data,
                             },
                            _wptsync={"repo_update": repo_update})
    logger.info("Running action {} for PR {}".format(action, pr.number))
    handle_sync("github", event)


def convert_rev(git_gecko: Repo, rev: Text) -> Tuple[Text, Text]:
    try:
        git_rev = cinnabar(git_gecko).hg2git(rev)
        hg_rev = rev
    except ValueError:
        # This was probably a git rev
        try:
            hg_rev = cinnabar(git_gecko).git2hg(rev)
        except ValueError:
            raise ValueError("{} is not a valid git or hg rev".format(rev))
        git_rev = rev
    return git_rev, hg_rev


def update_push(git_gecko: Repo, git_wpt: Repo, rev: Text, base_rev: Optional[Text] = None, processes: Optional[List[Text]] = None) -> None:
    git_rev, hg_rev = convert_rev(git_gecko, rev)
    git_rev_commit = git_gecko.rev_parse(git_rev)

    hg_rev_base: Optional[Text] = None
    if base_rev is not None:
        _, hg_rev_base = convert_rev(git_gecko, base_rev)

    if git_gecko.is_ancestor(git_rev_commit,
                             git_gecko.rev_parse(env.config["gecko"]["refs"]["central"])):
        routing_key = "mozilla-central"
    elif git_gecko.is_ancestor(git_rev_commit,
                               env.config["gecko"]["refs"]["autoland"]):
        routing_key = "integration/autoland"

    kwargs: Dict[Text, Any] = {"_wptsync": {}}

    if hg_rev_base is not None:
        kwargs["_wptsync"]["base_rev"] = hg_rev_base

    if processes is not None:
        kwargs["_wptsync"]["processes"] = processes

    event = construct_event("push", {"data": {"heads": [hg_rev]}},
                            _meta={"routing_key": routing_key},
                            **kwargs)

    args = ("push", event)
    handle_sync(*args)


def update_pr(git_gecko: Repo, git_wpt: Repo, pr: PullRequest, force_rebase: bool = False, repo_update: bool = True) -> None:
    sync = get_pr_sync(git_gecko, git_wpt, pr.number)

    if sync and sync.status == "complete":
        logger.info("Sync already landed")
        return
    if sync:
        logger.info("sync status %s" % sync.landable_status)
    sync_point = landing.load_sync_point(git_gecko, git_wpt)

    if not sync:
        # If this looks like something that came from gecko, create
        # a corresponding sync
        with SyncLock("upstream", None) as lock:
            assert isinstance(lock, SyncLock)
            upstream_sync = upstream.UpstreamSync.from_pr(lock,
                                                          git_gecko,
                                                          git_wpt,
                                                          pr.number,
                                                          pr.body)
            if upstream_sync is not None:
                with upstream_sync.as_mut(lock):
                    assert isinstance(lock, SyncLock)
                    upstream.update_pr(git_gecko,
                                       git_wpt,
                                       upstream_sync,
                                       pr.state,
                                       pr.merged)
            else:
                if pr.state != "open" and not pr.merged:
                    return
            schedule_pr_task("opened", pr, repo_update=repo_update)
            update_for_status(pr, repo_update=repo_update)
    elif isinstance(sync, downstream.DownstreamSync):
        with SyncLock.for_process(sync.process_name) as lock:
            assert isinstance(lock, SyncLock)
            with sync.as_mut(lock):
                if force_rebase:
                    sync.gecko_rebase(sync.gecko_integration_branch())

                if len(sync.wpt_commits) == 0:
                    sync.update_wpt_commits()

                if not sync.bug and not (pr.state == "closed" and not pr.merged):
                    sync.create_bug(git_wpt, pr.number, pr.title, pr.body)

                if pr.state == "open" or pr.merged:
                    if pr.head.sha != sync.wpt_commits.head:
                        # Upstream has different commits, so run a push handler
                        schedule_pr_task("push", pr, repo_update=repo_update)

                    elif sync.latest_valid_try_push:
                        logger.info("Treeherder url %s" % sync.latest_valid_try_push.treeherder_url)
                        if not sync.latest_valid_try_push.taskgroup_id:
                            update_taskgroup_ids(git_gecko, git_wpt,
                                                 sync.latest_valid_try_push)

                        if (sync.latest_valid_try_push.taskgroup_id and
                            not sync.latest_valid_try_push.status == "complete"):
                            update_tasks(git_gecko, git_wpt, sync=sync)

                        if not sync.latest_valid_try_push.taskgroup_id:
                            logger.info("Try push doesn't have a complete decision task")
                            return
                if not pr.merged:
                    update_for_status(pr, repo_update=repo_update)
                else:
                    update_for_action(pr, "closed", repo_update=repo_update)

    elif isinstance(sync, upstream.UpstreamSync):
        with SyncLock.for_process(sync.process_name) as lock:
            assert isinstance(lock, SyncLock)
            with sync.as_mut(lock):
                merge_sha = pr.merge_commit_sha if pr.merged else None
                upstream.update_pr(git_gecko, git_wpt, sync, pr.state, merge_sha)
                sync.try_land_pr()
                if merge_sha:
                    if git_wpt.is_ancestor(git_wpt.rev_parse(merge_sha),
                                           git_wpt.rev_parse(sync_point["upstream"])):
                        # This sync already landed, so it should be finished
                        sync.finish()
                    else:
                        if sync.status != "complete":
                            sync.status = "wpt-merged"  # type: ignore


def update_bug(git_gecko: Repo, git_wpt: Repo, bug: int) -> None:
    syncs = get_bug_sync(git_gecko, git_wpt, bug)
    if not syncs:
        raise ValueError("No sync for bug %s" % bug)

    for status in upstream.UpstreamSync.statuses:
        syncs_for_status = syncs.get(status)
        if not syncs_for_status:
            continue
        with SyncLock("upstream", None) as lock:
            assert isinstance(lock, SyncLock)
            for sync in syncs_for_status:
                if isinstance(sync, upstream.UpstreamSync):
                    with sync.as_mut(lock):
                        upstream.update_sync(git_gecko, git_wpt, sync)
                else:
                    logger.warning("Can't update sync %s" % sync)


def update_from_github(git_gecko: Repo, git_wpt: Repo, sync_classes: List[Type[SyncProcess]], statuses: Optional[List[Text]] = None) -> None:
    if statuses is None:
        statuses = ["*"]
    update_repositories(git_gecko, git_wpt)
    for cls in sync_classes:
        for status in statuses:
            if status != "*" and status not in cls.statuses:
                continue
            syncs = cls.load_by_status(git_gecko,
                                       git_wpt,
                                       status)
            for sync in syncs:
                if not sync.pr:
                    continue
                logger.info("Updating sync for PR %s" % sync.pr)
                pr = env.gh_wpt.get_pull(sync.pr)
                update_pr(git_gecko, git_wpt, pr)


def update_taskgroup_ids(git_gecko: Repo, git_wpt: Repo, try_push: Optional[TryPush] = None) -> None:
    if try_push is None:
        try_pushes = trypush.TryPush.load_all(git_gecko)
    else:
        try_pushes = [try_push]

    # Make this invalid so it's obvious if we try to use it below
    try_push = None

    for try_push_item in try_pushes:
        if not try_push_item.taskgroup_id:
            logger.info("Setting taskgroup id for try push %s" % try_push_item)
            if try_push_item.try_rev is None:
                logger.warning("Try push %s has no associated revision" %
                               try_push_item.process_name)
                continue
            taskgroup_id, state, runs = tc.get_taskgroup_id("try", try_push_item.try_rev)
            logger.info("Got taskgroup id %s" % taskgroup_id)
            if state in ("completed", "failed", "exception"):
                msg = {"status": {"taskId": taskgroup_id,
                                  "taskGroupId": taskgroup_id,
                                  "state": state,
                                  "runs": runs},
                       "task": {"tags": {"kind": "decision-task"}},
                       "runId": len(runs) - 1,
                       "version": 1}
                handle_sync("decision-task", msg)
            else:
                logger.warning("Not setting taskgroup id because decision task is in state %s" %
                               state)


def update_tasks(git_gecko: Repo, git_wpt: Repo, pr_id: Optional[int] = None, sync: Optional[SyncProcess] = None) -> None:
    logger.info("Running update_tasks%s" % ("for PR %s" % pr_id if pr_id else ""))

    syncs: Iterable[SyncProcess] = []
    if not sync:
        if pr_id is not None:
            pr_syncs = downstream.DownstreamSync.load_by_obj(git_gecko, git_wpt, pr_id)
            if not pr_syncs:
                logger.error("No sync for pr_id %s" % pr_id)
                return
            assert len(pr_syncs) == 1
            syncs = [pr_syncs.pop()]
        else:
            current_landing = landing.current(git_gecko, git_wpt)
            syncs = downstream.DownstreamSync.load_by_status(git_gecko, git_wpt, "open")
            if current_landing is not None:
                syncs.add(current_landing)
    else:
        syncs = [sync]

    for sync in syncs:
        try_push = sync.latest_try_push
        if try_push and try_push.taskgroup_id:
            try:
                handle_sync("taskgroup", {"taskGroupId": try_push.taskgroup_id})
            except AbortError:
                pass


def retrigger(git_gecko: Repo, git_wpt: Repo, unlandable_prs: List[Tuple[int, List[Any], Text]], rebase: bool = False) -> List[int]:
    from .sync import LandableStatus

    retriggerable_prs = [(pr_id, commits, status)
                         for (pr_id, commits, status) in unlandable_prs
                         if status not in (LandableStatus.ready,
                                           LandableStatus.skip,
                                           LandableStatus.upstream,
                                           LandableStatus.no_pr)]

    errors = []
    for pr_data in retriggerable_prs:
        error = do_retrigger(git_gecko, git_wpt, pr_data, rebase=rebase)
        if error:
            errors.append(error)

    return errors


def do_retrigger(git_gecko: Repo, git_wpt: Repo, pr_data: Tuple[int, List[Any], Text], rebase: bool = False) -> Optional[int]:
    pr_id, commits, status = pr_data
    try:
        logger.info("Retriggering {} (status {})".format(pr_id, status))
        pr = env.gh_wpt.get_pull(pr_id)
        if pr is None:
            return pr_id
        update_pr(git_gecko, git_wpt, pr, repo_update=False, force_rebase=rebase)
    except Exception:
        return pr_id
    return None
