import argparse
import os
import subprocess

import git

import listen
import log
from tasks import setup
from env import Environment
from gitutils import update_repositories
from load import get_syncs
from tasks import lock

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

    parser_bug = subparsers.add_parser("bug", help="Update the upstreaming for a specific bug")
    parser_bug.add_argument("bug", default=None, nargs="?", help="Bug number")
    parser_bug.set_defaults(func=do_bug)

    parser_upstream = subparsers.add_parser("upstream", help="Run the upstreaming code")
    parser_upstream.add_argument("branch", help="Repository name to use e.g. mozilla-inbound")
    parser_upstream.add_argument("rev", help="Revision to upstream to")
    parser_upstream.set_defaults(func=do_upstream)

    parser_delete = subparsers.add_parser("delete", help="Delete a sync by bug number or pr")
    parser_delete.add_argument("sync_type",  help="Type of sync to delete")
    parser_delete.add_argument("obj_id", help="Bug or PR id for the sync")
    parser_delete.add_argument("--try", action="store_true", help="Delete try pushes for a sync")
    parser_delete.set_defaults(func=do_delete)

    parser_status = subparsers.add_parser("status", help="Set the status of a Sync or Try push")
    parser_status.add_argument("obj_type", choices=["try", "sync"],
                               help="Object type")
    parser_status.add_argument("sync_type", choices=["downstream", "upstream", "landing"],
                               help="Sync type")
    parser_status.add_argument("obj_id",  help="Object id (pr number or bug)")
    parser_status.add_argument("new_status",  help="Status to set")
    parser_status.add_argument("--old-status", default="*", help="Current status")
    parser_status.add_argument("--seq-id",  nargs="?", default="*", help="Sequence number")
    parser_status.set_defaults(func=do_status)

    parser_status = subparsers.add_parser("test", help="Run the tests with pytest")
    parser_status.set_defaults(func=do_test)

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
    syncs.extend(downstream.DownstreamSync.load_all(git_gecko, git_wpt, status="open"))

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
    import landing
    landing.land_to_gecko(git_gecko, git_wpt)


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
    update_repositories(git_gecko, git_wpt, True)
    update.update_pr(git_gecko, git_wpt, pr)


def do_bug(git_gecko, git_wpt, bug, *args, **kwargs):
    import update
    if bug is None:
        bug = sync_from_path(git_gecko, git_wpt).bug
    update.update_bug(git_gecko, git_wpt, bug)


def do_upstream(git_gecko, git_wpt, *args, **kwargs):
    import upstream
    rev = kwargs["rev"]

    if rev is None:
        rev = git_gecko.commit(env.config["gecko"]["refs"]["mozilla-inbound"]).hexsha

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


    repository_name = kwargs["branch"]
    if repository_name is None:
        repository_name = env.config["gecko"]["refs"]["mozilla-inbound"]

    if not git_gecko.is_ancestor(git_rev, repository_name):
        raise ValueError("%s is not on branch %s" % (rev, repository_name))
    upstream.push(git_gecko, git_wpt, repository_name, hg_rev)


def do_delete(git_gecko, git_wpt, sync_type, obj_id, *args, **kwargs):
    import trypush
    if kwargs["try"]:
        try_pushes = trypush.TryPush.load_all(git_gecko, sync_type, obj_id)
        for try_push in try_pushes:
            try_push.delete()
    else:
        syncs = get_syncs(git_gecko, git_wpt, sync_type, obj_id)
        for sync in syncs:
            for try_push in sync.try_pushes():
                try_push.delete()
            sync.delete()


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


def do_status(git_gecko, git_wpt, obj_type, sync_type, obj_id, *args, **kwargs):
    import upstream
    import downstream
    import landing
    import trypush
    if obj_type == "try":
        objs = trypush.TryPush.load_all(git_gecko,
                                        sync_type,
                                        obj_id,
                                        status=kwargs["old_status"],
                                        seq_id=kwargs["seq_id"])
    elif sync_type == "upstream":
        objs = upstream.UpstreamSync.load_all(git_gecko,
                                              git_wpt,
                                              status=kwargs["old_status"],
                                              obj_id=obj_id)
    elif sync_type == "downstream":
        objs = downstream.DownstreamSync.load_all(git_gecko,
                                                  git_wpt,
                                                  status=kwargs["old_status"],
                                                  obj_id=obj_id)
    elif sync_type == "landing":
        objs = landing.LandingSync.load_all(git_gecko,
                                            git_wpt,
                                            status=kwargs["old_status"],
                                            obj_id=obj_id)
    for obj in objs:
        obj.status = kwargs["new_status"]


def do_test(*args, **kwargs):
    cmd = ["pytest", "--cov", "sync", "--cov-report", "html", "test/test_upstream.py"]
    subprocess.call(cmd)


def main():
    parser = get_parser()
    args = parser.parse_args()
    git_gecko, git_wpt = setup()
    try:
        with lock:
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
