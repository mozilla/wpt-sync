import argparse
import os
import sys

import git

import listen
import log
from tasks import setup
from env import Environment
from handlers import get_pr_sync, get_bug_sync

logger = log.get_logger("command")
env = Environment()

# HACK for docker
if "SHELL" not in os.environ:
    os.environ["SHELL"] = "/bin/bash"


def get_parser():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    parser.add_argument("--pdb", action="store_true", help="Run in pdb")

    parser_update = subparsers.add_parser("update",
                                          help="Update the local state by reading from GH + etc.")
    parser_update.set_defaults(func=do_update)

    parser_update = subparsers.add_parser("update-tasks",
                                          help="Update the state of try pushes")
    parser_update.set_defaults(func=do_update_tasks)

    parser_list = subparsers.add_parser("list", help="List all in-progress syncs")
    parser_list.set_defaults(func=do_list)

    parser_landing = subparsers.add_parser("landing", help="Trigger the landing code")
    parser_landing.set_defaults(func=do_landing)

    parser_setup = subparsers.add_parser("init", help="Configure repos and model in "
                                          "WPTSYNC_ROOT")
    parser_setup.add_argument("--create", action="store_true", help="Recreate the database")
    parser_setup.set_defaults(func=do_setup)

    parser_fetch = subparsers.add_parser("fetch", help="Fetch from repo.")
    parser_fetch.set_defaults(func=do_fetch)
    parser_fetch.add_argument('repo', choices=['gecko', 'web-platform-tests'])

    parser_listen = subparsers.add_parser("listen", help="Start pulse listener")
    parser_listen.set_defaults(func=do_start_listener)

    parser_pr = subparsers.add_parser("pr", help="Update the downstreaming for a specific PR")
    parser_pr.add_argument("pr_id", default=None, nargs="?", help="PR number")
    parser_pr.set_defaults(func=do_pr)

    parser_upstream = subparsers.add_parser("upstream", help="Run the upstreaming code")
    parser_upstream.add_argument("--rev", help="Revision to upstream to")
    parser_upstream.add_argument("--branch", help="Repository name to use e.g. mozilla-inbound")
    parser_upstream.set_defaults(func=do_upstream)

    parser_delete = subparsers.add_parser("delete", help="Delete a sync by bug number or pr")
    parser_delete.add_argument("--bug", help="Bug number for sync")
    parser_delete.add_argument("--pr",  help="PR number for sync")
    parser_delete.set_defaults(func=do_delete)

    parser_status = subparsers.add_parser("status", help="Set the status of a Sync or Try push")
    parser_status.add_argument("obj_type", choices=["try", "downstream", "upstream"],
                               help="Object type")
    parser_status.add_argument("obj_id",  help="Object id (pr number or bug)")
    parser_status.add_argument("new_status",  help="Status to set")
    parser_status.add_argument("--old-status", default="*", help="Current status")
    parser_status.add_argument("--seq-id",  nargs="?", default="*", help="Sequence number")
    parser_status.set_defaults(func=do_status)

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
    process_name = base.ProcessName.from_ref(branch)
    return cls(git_gecko, git_wpt, process_name)


def do_list(git_gecko, git_wpt, *args, **kwargs):
    import downstream
    import upstream
    syncs = upstream.UpstreamSync.load_all(git_gecko, git_wpt, status="open")
    syncs.extend(upstream.UpstreamSync.load_all(git_gecko, git_wpt, status="landed"))
    syncs.extend(downstream.DownstreamSync.load_all(git_gecko, git_wpt, status="open"))
    syncs.extend(downstream.DownstreamSync.load_all(git_gecko, git_wpt, status="ready"))

    for sync in syncs:
        extra = []
        if sync.sync_type == "downstream":
            try_push = sync.latest_try_push
            if try_push:
                extra.append("https://treeherder.mozilla.org/#/jobs?repo=try&revision=%s" % sync.latest_try_push.try_rev)
                if try_push.taskgroup_id:
                    extra.append(try_push.taskgroup_id)
        print("%s %s bug:%s PR:%s %s" % (sync.sync_type, sync.status, sync.bug, sync.pr,
                                         " ".join(extra)))


def do_landing(git_gecko, git_wpt, *args, **kwargs):
    import push
    push.land_to_gecko(git_gecko, git_wpt)


def do_update(git_gecko, git_wpt, *args, **kwargs):
    import update
    update.update_from_github(git_gecko, git_wpt)


def do_update_tasks(git_gecko, git_wpt, *args, **kwargs):
    import update
    update.update_taskgroup_ids(git_gecko, git_wpt)
    update.update_tasks(git_gecko, git_wpt)


def do_pr(git_gecko, git_wpt, pr_id, *args, **kwargs):
    import update
    if pr_id is None:
        pr_id = sync_from_path(git_gecko, git_wpt).pr
    pr = env.gh_wpt.get_pull(int(pr_id))
    update.update_pr(git_gecko, git_wpt, pr)


def do_upstream(git_gecko, git_wpt, *args, **kwargs):
    import upstream
    rev = kwargs["rev"]
    if rev is None:
        rev = git_gecko.commit(env.config["gecko"]["refs"]["mozilla-inbound"]).hexsha

    repository_name = kwargs["branch"]
    if repository_name is None:
        repository_name = env.config["gecko"]["refs"]["mozilla-inbound"]

    if not git_gecko.is_ancestor(rev, repository_name):
        raise ValueError("%s is not on branch %s" % (rev, repository_name))
    upstream.push(git_gecko, git_wpt, repository_name, rev)


def do_delete(git_gecko, git_wpt, *args, **kwargs):
    if not kwargs["bug"] and not kwargs["pr"]:
        print >> sys.stderr, "Must provide a bug number or PR number"
        sys.exit(1)
    if kwargs["bug"]:
        sync_bug = get_bug_sync(git_gecko, git_wpt, kwargs["bug"])
    else:
        sync_bug = None
    if kwargs["pr"]:
        sync_pr = get_pr_sync(git_gecko, git_wpt, kwargs["pr"])
    else:
        sync_pr = None
    if sync_pr is None and sync_bug is None:
        print >> sys.stderr, "No sync found"
        sys.exit(1)
    if sync_pr is not None and sync_bug is not None and sync_bug.process_name != sync_pr:
        print >> sys.stderr, "Bug and PR numbers don't match"
        sys.exit(1)
    if sync_pr is not None:
        sync_pr.delete()
    else:
        sync_bug.delete()


def do_start_listener(git_gecko, git_wpt, *args, **kwargs):
    listen.run_pulse_listener(env.config)


def do_fetch(git_gecko, git_wpt, *args, **kwargs):
    import repos
    name = kwargs.get("repo")
    r = repos.wrappers[name](config)
    r.configure()
    logger.info("Fetching %s..." % name)
    r.repo().git.fetch(*r.fetch_args)


def do_setup(git_gecko, git_wpt, *args, **kwargs):
    import repos
    repos.configure()


def do_status(git_gecko, git_wpt, *args, **kwargs):
    import downstream
    if kwargs["obj_type"] == "try":
        objs = downstream.TryPush.load_all(git_gecko,
                                           status=kwargs["old_status"],
                                           pr_id=kwargs["obj_id"],
                                           seq_id=kwargs["seq_id"])
    else:
        raise NotImplementedError
    for obj in objs:
        obj.status = kwargs["new_status"]
        print obj.status


def main():
    parser = get_parser()
    args = parser.parse_args()
    git_gecko, git_wpt = setup()
    try:
        args.func(git_gecko, git_wpt, **vars(args))
    except Exception as e:
        if args.pdb:
            import traceback
            traceback.print_exc(e)
            import pdb
            pdb.post_mortem()
        else:
            raise


if __name__ == "__main__":
    main()
