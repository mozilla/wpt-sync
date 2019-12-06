import argparse
import itertools
import json
import os
import re
import subprocess
import traceback

import git

import listen
from phabricator import listen as phablisten
import log
from tasks import setup
from env import Environment
from gitutils import update_repositories
from load import get_syncs
from lock import RepoLock, SyncLock

logger = log.get_logger(__name__)
env = Environment()

# HACK for docker
if "SHELL" not in os.environ:
    os.environ["SHELL"] = "/bin/bash"


def get_parser():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    parser.add_argument("--pdb", action="store_true", help="Run in pdb")
    parser.add_argument("--profile", action="store", help="Run in profile, dump stats to "
                        "specified filename")
    parser.add_argument("--config", action="append", help="Set a config option")

    parser_update = subparsers.add_parser("update",
                                          help="Update the local state by reading from GH + etc.")
    parser_update.add_argument("--sync-type", nargs="*", help="Type of sync to update",
                               choices=["upstream", "downstream"])
    parser_update.add_argument("--status", nargs="*", help="Statuses of syncs to update e.g. open")
    parser_update.set_defaults(func=do_update)

    parser_update_tasks = subparsers.add_parser("update-tasks",
                                                help="Update the state of try pushes")
    parser_update_tasks.add_argument("pr_id", nargs="?", help="Downstream PR id for sync to update")
    parser_update_tasks.set_defaults(func=do_update_tasks)

    parser_list = subparsers.add_parser("list", help="List all in-progress syncs")
    parser_list.add_argument("sync_type", nargs="*", help="Type of sync to list")
    parser_list.add_argument("--error", action="store_true", help="List only syncs with errors")
    parser_list.set_defaults(func=do_list)

    parser_detail = subparsers.add_parser("detail", help="List all in-progress syncs")
    parser_detail.add_argument("sync_type", help="Type of sync")
    parser_detail.add_argument("obj_id", help="Bug or PR id for the sync")
    parser_detail.set_defaults(func=do_detail)

    parser_landing = subparsers.add_parser("landing", help="Trigger the landing code")
    parser_landing.add_argument("--prev-wpt-head", help="First commit to use as the base")
    parser_landing.add_argument("--wpt-head", help="wpt commit to land to")
    parser_landing.add_argument("--no-push", dest="push", action="store_false", default=True,
                                help="Don't actually push anything to gecko")
    parser_landing.add_argument("--include-incomplete", action="store_true", default=False,
                                help="Consider PRs with incomplete syncs as landable.")
    parser_landing.add_argument("--accept-failures", action="store_true", default=False,
                                help="Consider the latest try push a success even if it has "
                                "more than the allowed number of failures")
    parser_landing.add_argument("--retry", action="store_true", default=False,
                                help="Rebase onto latest central and do another try push")
    parser_landing.set_defaults(func=do_landing)

    parser_fetch = subparsers.add_parser("repo-config", help="Configure repo.")
    parser_fetch.set_defaults(func=do_configure_repos)
    parser_fetch.add_argument('repo', choices=['gecko', 'web-platform-tests', 'wpt-metadata'])
    parser_fetch.add_argument('config_file', help="Path to git config file to copy.")

    parser_fetch = subparsers.add_parser("fetch", help="Fetch from repo.")
    parser_fetch.set_defaults(func=do_fetch)
    parser_fetch.add_argument('repo', choices=['gecko', 'web-platform-tests'])

    parser_listen = subparsers.add_parser("listen", help="Start pulse listener")
    parser_listen.set_defaults(func=do_start_listener)

    parser_listen = subparsers.add_parser("phab-listen", help="Start phabricator listener")
    parser_listen.set_defaults(func=do_start_phab_listener)

    parser_pr = subparsers.add_parser("pr", help="Update the downstreaming for a specific PR")
    parser_pr.add_argument("pr_id", default=None, nargs="?", help="PR number")
    parser_pr.add_argument("--rebase", default=False, action="store_true",
                           help="Force the PR to be rebase onto the integration branch")
    parser_pr.set_defaults(func=do_pr)

    parser_bug = subparsers.add_parser("bug", help="Update the upstreaming for a specific bug")
    parser_bug.add_argument("bug", default=None, nargs="?", help="Bug number")
    parser_bug.set_defaults(func=do_bug)

    parser_push = subparsers.add_parser("push", help="Run the push handler")
    parser_push.add_argument("--base-rev", help="Base revision for push or landing")
    parser_push.add_argument("--rev", help="Revision pushed")
    parser_push.add_argument("--process", dest="processes", action="append",
                             choices=["landing", "upstream"],
                             default=None,
                             help="Select process to run on push (default: landing, upstream)")
    parser_push.set_defaults(func=do_push)

    parser_delete = subparsers.add_parser("delete", help="Delete a sync by bug number or pr")
    parser_delete.add_argument("sync_type", help="Type of sync to delete")
    parser_delete.add_argument("obj_id", help="Bug or PR id for the sync")
    parser_delete.add_argument("--seq-id", help="Sync sequence id")
    parser_delete.add_argument("--all", action="store_true",
                               help="Delete all matches, not just most recent")
    parser_delete.add_argument("--try", action="store_true", help="Delete try pushes for a sync")
    parser_delete.set_defaults(func=do_delete)

    parser_status = subparsers.add_parser("status", help="Set the status of a Sync or Try push")
    parser_status.add_argument("obj_type", choices=["try", "sync"],
                               help="Object type")
    parser_status.add_argument("sync_type", choices=["downstream", "upstream", "landing"],
                               help="Sync type")
    parser_status.add_argument("obj_id", help="Object id (pr number or bug)")
    parser_status.add_argument("new_status", help="Status to set")
    parser_status.add_argument("--old-status", help="Current status")
    parser_status.add_argument("--seq-id", nargs="?", help="Sequence number")
    parser_status.set_defaults(func=do_status)

    parser_notify = subparsers.add_parser("notify", help="Generate notifications")
    parser_notify.add_argument("pr_id", help="PR for which to run notification code")
    parser_notify.add_argument("--force", action="store_true",
                               help="Run even if the sync is already marked as notified")
    parser_notify.set_defaults(func=do_notify)

    parser_test = subparsers.add_parser("test", help="Run the tests with pytest")
    parser_test.add_argument("--no-flake8", dest="flake8", action="store_false",
                             default=True, help="Don't run flake8")
    parser_test.add_argument("--no-pytest", dest="pytest", action="store_false",
                             default=True, help="Don't run pytest")
    parser_test.add_argument("args", nargs="*", help="Arguments to pass to pytest")
    parser_test.set_defaults(func=do_test)

    parser_cleanup = subparsers.add_parser("cleanup", help="Run the cleanup code")
    parser_cleanup.set_defaults(func=do_cleanup)

    parser_skip = subparsers.add_parser("skip",
                                        help="Mark the sync for a PR as skip so that "
                                        "it doesn't have to complete before a landing")
    parser_skip.add_argument("pr_ids", nargs="*", help="PR ids for which to skip")
    parser_skip.set_defaults(func=do_skip)

    parser_landable = subparsers.add_parser("landable",
                                            help="Display commits from upstream "
                                            "that are able to land")
    parser_landable.add_argument("--prev-wpt-head", help="First commit to use as the base")
    parser_landable.add_argument("--all", action="store_true", default=False,
                                 help="Print the status of all unlandable PRs")
    parser_landable.add_argument("--retrigger", action="store_true", default=False,
                                 help="Try to update all unlanded PRs that aren't Ready "
                                 "(requires --all)")
    parser_landable.add_argument("--include-incomplete", action="store_true", default=False,
                                 help="Consider PRs with incomplete syncs as landable.")
    parser_landable.set_defaults(func=do_landable)

    parser_retrigger = subparsers.add_parser("retrigger",
                                             help="Retrigger syncs that are not read")
    parser_retrigger.add_argument("--no-upstream", action="store_false", default=True,
                                  dest="upstream", help="Don't retrigger upstream syncs")
    parser_retrigger.add_argument("--no-downstream", action="store_false", default=True,
                                  dest="downstream", help="Don't retrigger downstream syncs")
    parser_retrigger.set_defaults(func=do_retrigger)

    parser_try_push_add = subparsers.add_parser("add-try",
                                                help="Add a try push to an existing sync")
    parser_try_push_add.add_argument("try_rev", help="Revision on try")
    parser_try_push_add.add_argument("sync_type", nargs="?", choices=["downstream", "landing"],
                                     help="Revision on try")
    parser_try_push_add.add_argument("sync_id", nargs="?",
                                     help="PR id for downstream sync or bug number "
                                     "for upstream sync")
    parser_try_push_add.add_argument("--stability", action="store_true",
                                     help="Push is stability try push")
    parser_try_push_add.add_argument("--rebuild-count", default=None, type=int,
                                     help="Rebuild count")
    parser_try_push_add.set_defaults(func=do_try_push_add)

    parser_download_logs = subparsers.add_parser("download-logs",
                                                 help="Download logs for a given try push")
    parser_download_logs.add_argument("--log-path",
                                      help="Destination path for the logs")
    parser_download_logs.add_argument("taskgroup_id", help="id of the taskgroup (decision task)")
    parser_download_logs.set_defaults(func=do_download_logs)

    parser_build_index = subparsers.add_parser("build-index",
                                               help="Build indexes")
    parser_build_index.add_argument("index_name", nargs="*",
                                    help="Index names to rebuild (default all)")
    parser_build_index.set_defaults(func=do_build_index)

    parser_migrate = subparsers.add_parser("migrate",
                                           help="Migrate to latest data storage format")
    parser_migrate.set_defaults(func=do_migrate)

    return parser


def sync_from_path(git_gecko, git_wpt):
    import base
    git_work = git.Repo(os.curdir)
    branch = git_work.active_branch.name
    parts = branch.split("/")
    if not parts[0] == "sync":
        return None
    if parts[1] == "downstream":
        import downstream
        cls = downstream.DownstreamSync
    elif parts[1] == "upstream":
        import upstream
        cls = upstream.UpstreamSync
    elif parts[1] == "landing":
        import landing
        cls = landing.LandingSync
    else:
        raise ValueError
    process_name = base.ProcessName.from_path(branch)
    return cls(git_gecko, git_wpt, process_name)


def do_list(git_gecko, git_wpt, sync_type, *args, **kwargs):
    import downstream
    import landing
    import upstream
    syncs = []

    def filter(sync):
        if kwargs["error"]:
            return sync.error is not None and sync.status == "open"
        return True

    for cls in [upstream.UpstreamSync, downstream.DownstreamSync, landing.LandingSync]:
        if not sync_type or cls.sync_type in sync_type:
            syncs.extend(item for item in cls.load_by_status(git_gecko, git_wpt, "open")
                         if filter(item))

    for sync in syncs:
        extra = []
        if sync.sync_type == "downstream":
            try_push = sync.latest_try_push
            if try_push:
                extra.append("https://treeherder.mozilla.org/#/jobs?repo=try&revision=%s" %
                             sync.latest_try_push.try_rev)
                if try_push.taskgroup_id:
                    extra.append(try_push.taskgroup_id)
        error = sync.error
        print("%s %s %s bug:%s PR:%s %s%s" % ("*"if sync.error else " ",
                                              sync.sync_type,
                                              sync.status,
                                              sync.bug,
                                              sync.pr,
                                              " ".join(extra),
                                              "ERROR: %s" %
                                              error["message"].split("\n", 1)[0] if error else ""))


def do_detail(git_gecko, git_wpt, sync_type, obj_id, *args, **kwargs):
    syncs = get_syncs(git_gecko, git_wpt, sync_type, obj_id)
    for sync in syncs:
        print(sync.output())


def do_landing(git_gecko, git_wpt, *args, **kwargs):
    import errors
    import landing
    import update
    current_landing = landing.current(git_gecko, git_wpt)

    accept_failures = kwargs["accept_failures"]

    def update_landing():
        landing.update_landing(git_gecko, git_wpt,
                               kwargs["prev_wpt_head"],
                               kwargs["wpt_head"],
                               kwargs["include_incomplete"],
                               retry=kwargs["retry"],
                               accept_failures=accept_failures)

    if current_landing and current_landing.latest_try_push:
        with SyncLock("landing", None) as lock:
            try_push = current_landing.latest_try_push
            logger.info("Found try push %s" % try_push.treeherder_url)
            if try_push.taskgroup_id is None:
                update.update_taskgroup_ids(git_gecko, git_wpt, try_push)
                assert try_push.taskgroup_id is not None
            with try_push.as_mut(lock), current_landing.as_mut(lock):
                if kwargs["retry"]:
                    update_landing()
                elif try_push.status == "open":
                    tasks = try_push.tasks()
                    try_result = current_landing.try_result(tasks=tasks)
                    if try_result == landing.TryPushResult.pending:
                        logger.info("Landing in bug %s is waiting for try results" % landing.bug)
                    else:
                        try:
                            landing.try_push_complete(git_gecko,
                                                      git_wpt,
                                                      try_push,
                                                      current_landing,
                                                      allow_push=kwargs["push"],
                                                      accept_failures=accept_failures,
                                                      tasks=tasks)
                        except errors.AbortError:
                            # Don't need to raise an error here because
                            # the logging is the important part
                            return
                else:
                    update_landing()
    else:
        update_landing()


def do_update(git_gecko, git_wpt, *args, **kwargs):
    import downstream
    import update
    import upstream
    sync_classes = []
    if not kwargs["sync_type"]:
        kwargs["sync_type"] = ["upstream", "downstream"]
    for key in kwargs["sync_type"]:
        sync_classes.append({"upstream": upstream.UpstreamSync,
                             "downstream": downstream.DownstreamSync}[key])
    update.update_from_github(git_gecko, git_wpt, sync_classes, kwargs["status"])


def do_update_tasks(git_gecko, git_wpt, *args, **kwargs):
    import update
    update.update_taskgroup_ids(git_gecko, git_wpt)
    update.update_tasks(git_gecko, git_wpt, kwargs["pr_id"])


def do_pr(git_gecko, git_wpt, pr_id, *args, **kwargs):
    import update
    if pr_id is None:
        pr_id = sync_from_path(git_gecko, git_wpt).pr
    pr = env.gh_wpt.get_pull(int(pr_id))
    update_repositories(git_gecko, git_wpt, True)
    update.update_pr(git_gecko, git_wpt, pr, kwargs["rebase"])


def do_bug(git_gecko, git_wpt, bug, *args, **kwargs):
    import update
    if bug is None:
        bug = sync_from_path(git_gecko, git_wpt).bug
    update.update_bug(git_gecko, git_wpt, bug)


def do_push(git_gecko, git_wpt, *args, **kwargs):
    import update
    rev = kwargs["rev"]
    base_rev = kwargs["base_rev"]
    processes = kwargs["processes"]
    if rev is None:
        rev = git_gecko.commit(env.config["gecko"]["refs"]["mozilla-inbound"]).hexsha

    update.update_push(git_gecko, git_wpt, rev, base_rev=base_rev, processes=processes)


def do_delete(git_gecko, git_wpt, sync_type, obj_id, *args, **kwargs):
    import trypush
    if kwargs["try"]:
        objs = trypush.TryPush.load_by_obj(git_gecko, sync_type, obj_id,
                                           seq_id=kwargs["seq_id"])
    else:
        objs = get_syncs(git_gecko, git_wpt, sync_type, obj_id, seq_id=kwargs["seq_id"])
    if not kwargs["all"] and objs:
        objs = sorted(objs, key=lambda x: -int(x.process_name.seq_id))[:1]
    for obj in objs:
        with SyncLock.for_process(obj.process_name) as lock:
            with obj.as_mut(lock):
                obj.delete()


def do_start_listener(git_gecko, git_wpt, *args, **kwargs):
    listen.run_pulse_listener(env.config)


def do_start_phab_listener(git_gecko, git_wpt, *args, **kwargs):
    phablisten.run_phabricator_listener(env.config)


def do_fetch(git_gecko, git_wpt, *args, **kwargs):
    import repos
    c = env.config
    name = kwargs.get("repo")
    r = repos.wrappers[name](c)
    logger.info("Fetching %s in %s..." % (name, r.root))
    pyrepo = r.repo()
    with RepoLock(pyrepo):
        try:
            pyrepo.git.fetch(*r.fetch_args)
        except git.GitCommandError as e:
            # GitPython fails when git warns about adding known host key for new IP
            if re.search(".*Warning: Permanently added.*host key.*", e.stderr) is not None:
                logger.debug(e.stderr)
            else:
                raise e


def do_configure_repos(git_gecko, git_wpt, *args, **kwargs):
    import repos
    name = kwargs.get("repo")
    r = repos.wrappers[name](env.config)
    with RepoLock(r.repo()):
        r.configure(os.path.abspath(os.path.normpath(kwargs.get("config_file"))))


def do_status(git_gecko, git_wpt, obj_type, sync_type, obj_id, *args, **kwargs):
    import upstream
    import downstream
    import landing
    import trypush
    if obj_type == "try":
        objs = trypush.TryPush.load_by_obj(git_gecko,
                                           sync_type,
                                           obj_id,
                                           seq_id=kwargs["seq_id"])
    else:
        if sync_type == "upstream":
            cls = upstream.UpstreamSync
        if sync_type == "downstream":
            cls = downstream.DownstreamSync
        if sync_type == "landing":
            cls = landing.LandingSync
        objs = cls.load_by_obj(git_gecko,
                               git_wpt,
                               obj_id,
                               seq_id=kwargs["seq_id"])

    if kwargs["old_status"] is not None:
        objs = {item for item in objs if item.status == kwargs["old_status"]}

    if not objs:
        logger.error("No matching syncs found")

    for obj in objs:
        logger.info("Setting status of %s to %s" % (obj.process_name, kwargs["new_status"]))
        with SyncLock.for_process(obj.process_name) as lock:
            with obj.as_mut(lock):
                obj.status = kwargs["new_status"]


def do_notify(git_gecko, git_wpt, pr_id, **kwargs):
    import downstream
    sync = downstream.DownstreamSync.for_pr(git_gecko, git_wpt, pr_id)
    if sync is None:
        logger.error("No active sync for PR %s" % pr_id)
        return
    with SyncLock.for_process(sync.process_name) as lock:
        with sync.as_mut(lock):
            old_notified = None
            if kwargs["force"]:
                old_notified = sync.results_notified
                sync.results_notified = False
            try:
                sync.try_notify()
            finally:
                # Reset the notification status if it was set before and isn't now
                if not sync.results_notified and old_notified:
                    sync.results_notified = True


def do_test(*args, **kwargs):
    if kwargs.pop("flake8", True):
        logger.info("Running flake8")
        cmd = ["flake8"]
        subprocess.check_call(cmd, cwd="/app/wpt-sync/sync/")
        subprocess.check_call(cmd, cwd="/app/wpt-sync/test/")

    if kwargs.pop("pytest", True):
        args = kwargs["args"]
        if not any(item.startswith("test") for item in args):
            args.append("test")

        logger.info("Running pytest")
        cmd = ["pytest", "-s", "-v", "-p", "no:cacheprovider"] + args
        subprocess.check_call(cmd, cwd="/app/wpt-sync/")


def do_cleanup(git_gecko, git_wpt, *args, **kwargs):
    from tasks import cleanup
    cleanup()


def do_skip(git_gecko, git_wpt, pr_ids, *args, **kwargs):
    import downstream
    if not pr_ids:
        syncs = [sync_from_path(git_gecko, git_wpt)]
    else:
        syncs = [downstream.DownstreamSync.for_pr(git_gecko, git_wpt, pr_id) for pr_id in pr_ids]
    for sync in syncs:
        if sync is None:
            logger.error("No active sync for PR %s" % pr_id)
        else:
            with SyncLock.for_process(sync.process_name) as lock:
                with sync.as_mut(lock):
                    sync.skip = True


def do_landable(git_gecko, git_wpt, *args, **kwargs):
    import update
    from sync import LandableStatus
    from downstream import DownstreamAction, DownstreamSync
    from landing import current, load_sync_point, landable_commits, unlanded_with_type

    current_landing = current(git_gecko, git_wpt)

    if kwargs["prev_wpt_head"] is not None:
        prev_wpt_head = kwargs["prev_wpt_head"]
    elif current_landing:
        print("Current landing will update head to %s" % current_landing.wpt_commits.head.sha1)
        prev_wpt_head = current_landing.wpt_commits.head.sha1
    else:
        sync_point = load_sync_point(git_gecko, git_wpt)
        print("Last sync was to commit %s" % sync_point["upstream"])
        prev_wpt_head = sync_point["upstream"]

    landable = landable_commits(git_gecko, git_wpt, prev_wpt_head,
                                include_incomplete=kwargs["include_incomplete"])

    if landable is None:
        print("Next landing will not add any new commits")
        wpt_head = None
    else:
        wpt_head, commits = landable
        print("Next landing will update wpt head to %s, adding %i new PRs" %
              (wpt_head, len(commits)))

    if kwargs["all"] or kwargs["retrigger"]:
        unlandable = unlanded_with_type(git_gecko, git_wpt, wpt_head, prev_wpt_head)
        count = 0
        for pr, _, status in unlandable:
            count += 1
            msg = status.reason_str()
            if status == LandableStatus.missing_try_results:
                sync = DownstreamSync.for_pr(git_gecko, git_wpt, pr)
                next_action = sync.next_action
                reason = next_action.reason_str()
                if next_action == DownstreamAction.wait_try:
                    latest_try_push = sync.latest_try_push
                    reason = "%s %s" % (reason,
                                        latest_try_push.treeherder_url)
                elif next_action == DownstreamAction.manual_fix:
                    latest_try_push = sync.latest_try_push
                    reason = "Manual fixup required %s" % (
                        latest_try_push.treeherder_url,)
                msg = "%s (%s)" % (msg, reason)
            elif status == LandableStatus.error:
                sync = DownstreamSync.for_pr(git_gecko, git_wpt, pr)
                msg = "%s (%s)" % (msg, sync.error["message"].split("\n")[0])
            print("%s: %s" % (pr, msg))

        print ("%i PRs are unlandable:" % count)

        if kwargs["retrigger"]:
            errors = update.retrigger(git_gecko, git_wpt, unlandable)
            if errors:
                print("The following PRs have errors:\n%s" % "\n".join(
                    str(item) for item in errors))


def do_retrigger(git_gecko, git_wpt, **kwargs):
    import errors
    import update
    import upstream
    from landing import current, load_sync_point, unlanded_with_type

    update_repositories(git_gecko, git_wpt, True)

    if kwargs["upstream"]:
        print("Retriggering upstream syncs with errors")
        for sync in upstream.UpstreamSync.load_by_status(git_gecko, git_wpt, "open"):
            if sync.error:
                with SyncLock.for_process(sync.process_name) as lock:
                    with sync.as_mut(lock):
                        try:
                            upstream.update_sync(git_gecko, git_wpt, sync, repo_update=False)
                        except errors.AbortError as e:
                            print("Update failed:\n%s" % e)
                            pass

    if kwargs["downstream"]:
        print("Retriggering downstream syncs on master")
        current_landing = current(git_gecko, git_wpt)
        if current_landing is None:
            sync_point = load_sync_point(git_gecko, git_wpt)
            prev_wpt_head = sync_point["upstream"]
        else:
            prev_wpt_head = current_landing.wpt_commits.head.sha1
        unlandable = unlanded_with_type(git_gecko, git_wpt, None, prev_wpt_head)

        errors = update.retrigger(git_gecko, git_wpt, unlandable)
        if errors:
            print("The following PRs have errors:\n%s" % "\n".join(errors))


def do_try_push_add(git_gecko, git_wpt, sync_type=None, sync_id=None, **kwargs):
    import downstream
    import landing
    import trypush

    sync = None
    if sync_type is None:
        sync = sync_from_path(git_gecko, git_wpt)
    elif sync_type == "downstream":
        sync = downstream.DownstreamSync.for_pr(git_gecko, git_wpt, sync_id)
    elif sync_type == "landing":
        syncs = landing.LandingSync.for_bug(git_gecko, git_wpt, sync_id, flat=True)
        if syncs:
            sync = syncs[0]
    else:
        raise ValueError

    if not sync:
        raise ValueError

    class FakeTry(object):
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def push(self):
            return kwargs["try_rev"]

    with SyncLock.for_process(sync.process_name) as lock:
        with sync.as_mut(lock):
            trypush = trypush.TryPush.create(lock,
                                             sync,
                                             None,
                                             stability=kwargs["stability"],
                                             try_cls=FakeTry, rebuild_count=kwargs["rebuild_count"],
                                             check_open=False)

    print "Now run an update for the sync"


def do_download_logs(git_gecko, git_wpt, log_path, taskgroup_id, **kwargs):
    import tc
    import trypush
    import tempfile

    if log_path is None:
        log_path = tempfile.mkdtemp()

    taskgroup_id = tc.normalize_task_id(taskgroup_id)

    tasks = tc.TaskGroup(taskgroup_id)
    tasks.refresh()

    try_tasks = trypush.TryPushTasks(tasks)
    try_tasks.wpt_tasks.download_logs(os.path.join(log_path, taskgroup_id),
                                      ["wptreport.json"])


def do_build_index(git_gecko, git_wpt, index_name, **kwargs):
    import index
    if not index_name:
        index_names = None
    else:
        index_names = set(index_name)
    for idx_cls in index.indicies:
        if index_names and idx_cls.name not in index_names:
            continue
        print "Building %s index" % idx_cls.name
        idx = idx_cls(git_gecko)
        idx.build(git_gecko, git_wpt)


def do_migrate(git_gecko, git_wpt, **kwargs):
    # Migrate refs from the refs/<type>/<subtype>/<status>/<obj_id>[/<seq_id>] format
    # to refs/<type>/<subtype>/<obj_id>/<seq_id>
    from collections import defaultdict
    import base

    import pygit2

    git2_gecko = pygit2.Repository(git_gecko.working_dir)
    git2_wpt = pygit2.Repository(git_wpt.working_dir)

    repo_map = {git_gecko: git2_gecko,
                git_wpt: git2_wpt}
    rev_repo_map = {value: key for key, value in repo_map.iteritems()}

    special = {}

    sync_ref = re.compile("^refs/"
                          "(?P<reftype>[^/]+)/"
                          "(?P<obj_type>[^/]+)/"
                          "(?P<subtype>[^/]+)/"
                          "(?P<status>[^0-9/]+)/"
                          "(?P<obj_id>[0-9]+)"
                          "(?:/(?P<seq_id>[0-9]*))?$")
    print "Updating refs"
    seen = defaultdict(list)
    total_refs = 0
    processing_refs = 0
    for ref in itertools.chain(git_gecko.refs, git_wpt.refs):
        git2_repo = repo_map[ref.repo]
        ref = git2_repo.lookup_reference(ref.path)
        total_refs += 1
        if ref.name in special:
            continue
        m = sync_ref.match(ref.name)
        if not m:
            continue
        if m.group("reftype") not in ("heads", "syncs"):
            continue
        if m.group("obj_type") not in ("sync", "try"):
            continue
        processing_refs += 1
        assert m.group("subtype") in ("upstream", "downstream", "landing")
        assert int(m.group("obj_id")) > 0
        new_ref = "refs/%s/%s/%s/%s/%s" % (m.group("reftype"),
                                           m.group("obj_type"),
                                           m.group("subtype"),
                                           m.group("obj_id"),
                                           m.group("seq_id") or "0")
        seen[(git2_repo, new_ref)].append((ref, m.group("status")))

    duplicate = {}
    delete = set()
    for (repo, new_ref), refs in seen.iteritems():
        if len(refs) > 1:
            # If we have multiple /syncs/ ref, but only one /heads/ ref, use the corresponding one
            if new_ref.startswith("refs/syncs/"):
                has_head = set()
                no_head = set()
                for ref, status in refs:
                    if "refs/heads/%s" % ref.name[len("refs/syncs/")] in repo.references:
                        has_head.add((ref.name, status))
                    else:
                        no_head.add((ref.name, status))
                if len(has_head) == 1:
                    print "  Using %s from %s" % (list(has_head)[0][0].path,
                                                  " ".join(ref.name for ref, _ in refs))
                    refs[:] = list(has_head)
                    delete |= set((repo, ref_name) for ref_name, _ in no_head)

        if len(refs) > 1:
            # If we have a later status, prefer that over an earlier one
            matches = {ref.name: sync_ref.match(ref.name) for ref, _ in refs}
            by_status = {matches[ref.name].group("status"): (ref, status) for (ref, status) in refs}
            for target_status in ["complete", "wpt-merged", "incomplete", "infra-fail"]:
                if target_status in by_status:
                    print "  Using %s from %s" % (by_status[target_status][0].name,
                                                  " ".join(ref.name for ref, _ in refs))
                    delete |= set((repo, ref.name) for ref, status in refs
                                  if ref != by_status[target_status])
                    refs[:] = [by_status[target_status]]

        if len(refs) > 1:
            duplicate[(repo, new_ref)] = refs

    if duplicate:
        print "  ERROR! Got duplicate %s source refs" % len(duplicate)
        for (repo, new_ref), refs in duplicate.iteritems():
            print "    %s %s: %s" % (repo.working_dir,
                                     new_ref,
                                     " ".join(ref.name for ref, _ in refs))
        return

    for (repo, new_ref), refs in seen.iteritems():
        ref, _ = refs[0]

        if ref.name.startswith("refs/syncs/sync/"):
            if "refs/heads/%s" % ref.name[len("refs/syncs/"):] not in repo.references:
                # Try with the post-migration head
                m = sync_ref.match(ref.name)
                ref_path = "refs/heads/%s/%s/%s/%s" % (m.group("obj_type"),
                                                       m.group("subtype"),
                                                       m.group("obj_id"),
                                                       m.group("seq_id"))
                if ref_path not in repo.references:
                    print "  Missing head %s" % (ref.name)

    created = 0
    for i, ((repo, new_ref), refs) in enumerate(seen.iteritems()):
        assert len(refs) == 1
        ref, status = refs[0]
        print "Updating %s" % ref.name

        print "  Moving %s to %s %d/%d" % (ref.name, new_ref, i + 1, len(seen))

        if "/syncs/" in ref.name:
            ref_obj = ref.peel().id
            data = json.loads(repo[ref.peel().tree["data"].id].data)
            if data.get("status") != status:
                with base.CommitBuilder(rev_repo_map[repo], "Add status", ref=ref.name) as commit:
                    now_ref_obj = ref.peel().id
                    if ref_obj != now_ref_obj:
                        data = json.loads(repo[ref.peel().tree["data"].id].data)
                    data["status"] = status
                    commit.add_tree({"data": json.dumps(data)})
                    print "Making commit"
            commit = commit.get().sha1
        else:
            commit = ref.peel().id

        print "  Got commit %s" % commit

        if new_ref not in repo.references:
            print "  Rename %s %s" % (ref.name, new_ref)
            repo.references.create(new_ref, commit)
            created += 1
        else:
            print "  %s already exists" % new_ref
        delete.add((repo, ref.name))

    for repo, ref_name in delete:
        print "  Deleting %s" % ref_name
        repo.references.delete(ref_name)

    print "%s total refs" % total_refs
    print "%s refs to process" % processing_refs
    print "%s refs to create" % created
    print "%s refs to delete" % len(delete)

    print "Moving to single history"
    # Migrate from refs/syncs/ to paths
    sync_ref = re.compile("^refs/"
                          "syncs/"
                          "(?P<obj_type>[^/]*)/"
                          "(?P<subtype>[^/]*)/"
                          "(?P<obj_id>[^/]*)/"
                          "(?P<seq_id>[0-9]*)$")
    delete = set()
    initial_ref = git.Reference(git_gecko, "refs/syncs/data")
    if initial_ref.is_valid():
        existing_paths = {item.path for item in initial_ref.commit.tree.traverse()}
    else:
        existing_paths = set()
    for ref in git_gecko.refs:
        m = sync_ref.match(ref.path)
        if not m:
            continue
        path = "%s/%s/%s/%s" % (m.group("obj_type"),
                                m.group("subtype"),
                                m.group("obj_id"),
                                m.group("seq_id"))
        if path not in existing_paths:
            with base.CommitBuilder(git_gecko,
                                    "Migrate %s to single ref for data" % ref.path,
                                    ref="refs/syncs/data") as commit:
                data = json.load(ref.commit.tree["data"].data_stream)
                print "  Moving path %s" % (path,)
                tree = {path: json.dumps(data)}
                commit.add_tree(tree)
        delete.add(ref.path)

    git2_repo = repo_map[git_gecko]
    for ref_name in delete:
        git2_repo.references.delete(ref_name)


def set_config(opts):
    for opt in opts:
        keys, value = opt.split("=", 1)
        keys = keys.split(".")
        target = env.config
        for key in keys[:-1]:
            target = target[key]
        logger.info("Setting config option %s from %s to %s" %
                    (".".join(keys), target[keys[-1]], value))
        target[keys[-1]] = value


def main():
    parser = get_parser()
    args = parser.parse_args()

    if args.profile:
        import cProfile
        prof = cProfile.Profile()
        prof.enable()

    try:
        func_name = args.func.__name__
    except AttributeError as e:
        func_name = None
    if func_name == "do_test":
        git_gecko, git_wpt = (None, None)
    else:
        git_gecko, git_wpt = setup()

    if args.config:
        set_config(args.config)

    try:
        args.func(git_gecko, git_wpt, **vars(args))
    except Exception as e:
        if args.pdb:
            traceback.print_exc(e)
            import pdb
            pdb.post_mortem()
        else:
            raise
    finally:
        if args.profile:
            prof.dump_stats(args.profile)
            prof.print_stats()


if __name__ == "__main__":
    main()
