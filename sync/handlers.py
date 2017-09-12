import traceback
import urlparse

import git

import downstream
import log
import model
import push
import upstream
import worktree
from model import Landing, PullRequest, Sync, SyncDirection


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


def get_sync(session, pr_id):
    return session.query(Sync).filter(Sync.pr_id == pr_id).first()


class Handler(object):
    def __init__(self, config):
        self.config = config

    def __call__(self, session, git_gecko, git_wpt, gh_wpt, bz, body):
        raise NotImplementedError


def handle_pr(config, session, git_gecko, git_wpt, gh_wpt, bz, event):
    pr_id = event['number']
    sync = get_sync(session, pr_id)

    gh_wpt.load_pull(event["pull_request"])
    PullRequest.update_from_github(session, event["pull_request"])

    if not sync:
        # If we don't know about this sync then it's a new thing that we should
        # set up state for
        # TODO: maybe want to create a new sync here irrespective of the event
        # type because we missed some events.
        if event["action"] == "opened":
            downstream.new_wpt_pr(config, session, git_gecko, git_wpt, bz, event)
    elif sync.direction == SyncDirection.upstream:
        # This is a PR we created, so ignore it for now
        pass
    elif sync.direction == SyncDirection.downstream:
        if event["action"] == "closed":
            # TODO - close the related bug, cancel try runs, etc.
            pass
        # TODO It's a PR we already started to downstream, so update as appropriate


def pr_for_commit(git_wpt, rev):
    #TODO: Work out how to add these to the config when we set up the repo
    git_wpt.remotes.origin.fetch("+refs/pull/*/head:refs/remotes/origin/pr/*")
    branches = git_wpt.git.branch("--points-at", rev, all=True)
    pr_id = None
    for item in branches.split("\n"):
        item = item.strip()
        if item.startswith("remotes/origin/pr/"):
            pr_id = int(item.rsplit("/", 1)[1])
            break
    return pr_id


def is_ancestor(git_obj, rev, branch):
    try:
        git_obj.git.merge_base(rev, branch, is_ancestor=True)
    except git.GitCommandError:
        return False
    return True


def handle_status(config, session, git_gecko, git_wpt, gh_wpt, bz, event):
    if event["context"] == "upstream/gecko":
        # Never handle changes to our own status
        return

    rev = event["sha"]
    pr_id = pr_for_commit(git_wpt, rev)

    if not pr_id:
        if not is_ancestor(git_wpt, rev, "origin/master"):
            logger.error("Got status for commit %s, but that isn't the head of any PR" % rev)
        return
    else:
        logger.info("Got status for commit %s from PR %s" % (rev, pr_id))

    sync = get_sync(session, pr_id)

    if not sync:
        # Presumably this is a thing we ought to be downstreaming, but missed somehow
        # TODO: Handle this case
        logger.error("Got a status update for PR %i which is unknown to us" % pr_id)

    if sync.direction == SyncDirection.upstream:
        upstream.status_changed(config, session, bz, git_gecko, git_wpt, gh_wpt, sync,
                                event["context"], event["status"], event["url"])
    elif sync.direction == SyncDirection.downstream:
        downstream.status_changed(config, session, git_gecko, git_wpt, bz, sync, event)


def handle_pr_merge():
    # prepare to land downstream
    pass


def handle_pr_approved():
    # prepare to land downstream
    pass


def handle_push(config, session, git_gecko, git_wpt, gh_wpt, bz, event):
    push.wpt_push(session, git_wpt, gh_wpt, [item["sha"] for item in event["commits"]])


class GitHubHandler(Handler):
    dispatch_event = {
        "pull_request": handle_pr,
        "status": handle_status,
        "push": handle_push,
    }

    def __call__(self, session, git_gecko, git_wpt, gh_wpt, bz, body):
        handler = self.dispatch_event[body["event"]]
        if handler:
            return handler(self.config, session, git_gecko, git_wpt, gh_wpt, bz, body["payload"])
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

    def __call__(self, session, git_gecko, git_wpt, gh_wpt, bz, body):
        data = body["payload"]["data"]
        repo_url = data["repo_url"]
        logger.debug("Commit landed in repo %s" % repo_url)
        if repo_url in self.integration_repos or repo_url == self.landing_repo:
            if repo_url in self.integration_repos:
                repo_name = self.integration_repos[repo_url]
                upstream.integration_commit(self.config, session, git_gecko, git_wpt, gh_wpt,
                                            bz, repo_name)
            elif repo_url == self.landing_repo:
                upstream.landing_commit(self.config, session, git_gecko, git_wpt, gh_wpt, bz)


class TaskHandler(Handler):
    def __call__(self, session, git_gecko, git_wpt, gh_wpt, bz, body):
        return downstream.update_taskgroup(
            self.config,
            session,
            body
        )


class TaskGroupHandler(Handler):
    def __call__(self, session, git_gecko, git_wpt, gh_wpt, bz, body):
        return downstream.on_taskgroup_resolved(
            self.config,
            session,
            git_gecko,
            bz,
            body["taskGroupId"])


class LandingHandler(Handler):
    def __call__(self, session, git_gecko, git_wpt, gh_wpt, bz):
        return push.land_to_gecko(self.config, session, git_wpt, git_wpt, gh_wpt, bz)


class CleanupHandler(Handler):
    def __call__(self, session, git_gecko, git_wpt, gh_wpt, bz):
        return worktree.cleanup(self.config, session)
