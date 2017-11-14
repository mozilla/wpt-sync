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


def do_landing(*args, **kwargs):
    import push
    git_gecko, git_wpt = setup()
    push.land_to_gecko(git_gecko, git_wpt)


def do_update(*args, **kwargs):
    import update
    git_gecko, git_wpt = setup()
    update.update_from_github(git_gecko, git_wpt)


def do_pr(pr_id, *args, **kwargs):
    import update
    git_gecko, git_wpt = setup()
    if pr_id is None:
        pr_id = sync_from_path(git_gecko, git_wpt).pr
    pr = env.gh_wpt.get_pull(int(pr_id))
    update.update_pr(git_gecko, git_wpt, pr)


def do_upstream(*args, **kwargs):
    import upstream
    git_gecko, git_wpt = setup()
    rev = kwargs["rev"]
    if rev is None:
        rev = git_gecko.commit(env.config["gecko"]["refs"]["mozilla-inbound"]).hexsha

    repository_name = kwargs["branch"]
    if repository_name is None:
        repository_name = env.config["gecko"]["refs"]["mozilla-inbound"]

    if not git_gecko.is_ancestor(rev, repository_name):
        raise ValueError("%s is not on branch %s" % (rev, repository_name))
    upstream.push(git_gecko, git_wpt, repository_name, rev)


def do_delete(*args, **kwargs):
    git_gecko, git_wpt = setup()
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


def do_start_listener(config, *args, **kwargs):
    listen.run_pulse_listener(config)


def do_fetch(config, *args, **kwargs):
    import repos
    name = kwargs.get("repo")
    r = repos.wrappers[name](config)
    r.configure()
    logger.info("Fetching %s..." % name)
    r.repo().git.fetch(*r.fetch_args)


def do_setup(config, *args, **kwargs):
    import repos
    repos.configure(config)


def main():
    parser = get_parser()
    args = parser.parse_args()
    try:
        args.func(**vars(args))
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
