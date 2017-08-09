import traceback
import urlparse

import bug
import downstream
import gh
import log
import model
import repos
import upstream
from model import Sync, SyncDirection


logger = log.get_logger("handlers")


def setup(config):
    model.configure(config)
    session = model.session()

    git_gecko = repos.Gecko(config).repo()
    git_wpt = repos.WebPlatformTests(config).repo()

    gh_wpt = gh.GitHub(config["web-platform-tests"]["github"]["token"],
                       config["web-platform-tests"]["repo"]["url"])

    bz = bug.MockBugzilla(config)

    return session, git_gecko, git_wpt, gh_wpt, bz


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
    return session.query(Sync).filter(Sync.pr == pr_id).first()


class Handler(object):
    def __init__(self, config):
        self.config = config

    def __call__(self, body):
        raise NotImplementedError


class GitHubHandler(Handler):
    def __call__(self, body):
        routing_key = body['_meta']['routing_key']
        if not routing_key.startswith("w3c/"):
            return

        logger.debug('Look, an event:' + body['event'])
        logger.debug('from:' + body['_meta']['routing_key'])

        dispatch_event = {
            "pull_request": handle_pr,
            "status": handle_status,
        }

        handler = dispatch_event.get(body['event'])
        if handler:
            session, git_gecko, git_wpt, gh_wpt, bz = setup(self.config)
            return handler(self.config, session, git_gecko, git_wpt, gh_wpt, bz, body)
        # TODO: other events to check if we can merge a PR
        # because of some update


def handle_pr(config, session, git_gecko, git_wpt, gh_wpt, bz, body):
    event = body['payload']
    pr_id = event['number']
    sync = get_sync(session, pr_id)

    gh_wpt.load_pull(event["pull_request"])

    if not sync:
        # If we don't know about this sync then it's a new thing that we should
        # set up state for
        if event["action"] == "opened":
            downstream.new_wpt_pr(config, session, git_gecko, git_wpt, bz, body)
    elif sync.direction == SyncDirection.upstream:
        # This is a PR we created, so ignore it for now
        pass
    elif sync.direction == SyncDirection.downstream
        if event["action"] == closed:
            # TODO - close the related bug, cancel try runs, etc.
            pass
        # It's a PR we already started to downstream, so update as appropriate
        # TODO


def pr_for_commit(git_wpt, rev):
    #TODO: Work out how to add these to the config when we set up the repo
    git_wpt.remotes.origin.fetch("+refs/pull/*/head:refs/remotes/origin/pr/*")
    branches = git_wpt.git.branch(rev, all=True, points_at=True)
    pr_id = None
    for item in branches.split("\n"):
        if item.startswith("remotes/origin/pr/"):
            pr_id = int(item.rsplit("/", 1)[1])
            break
    return pr_id


def handle_status(config, session, git_gecko, git_wpt, gh_wpt, bz, body):
    event = body["payload"]

    if event["context"] == "upstream/gecko":
        # Never handle changes to our own status
        return

    rev = event["sha"]
    pr_id = pr_for_commit(git_wpt, rev)

    if not pr_id:
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
        downstream.status_changed(config, session, bz, sync,
                                  event["context"], event["status"], event["url"])


class CommitHandler(Handler):
    def __init__(self, config):
        self.config = config

        self.integration_repos = {}
        for repo_name, url in config["sync"]["integration"].iteritems():
            url_parts = urlparse.urlparse(url)
            url = urlparse.urlunparse(("https",) + url_parts[1:])
            self.integration_repos[url] = repo_name

        self.landing_repo = config["sync"]["landing"]

    def __call__(self, body):
        data = body["payload"]["data"]
        repo_url = data["repo_url"]
        logger.debug("Commit landed in repo %s" % repo_url)
        if repo_url in self.integration_repos or repo_url == self.landing_repo:
            session, git_gecko, git_wpt, gh_wpt, bz = setup(self.config)
            if repo_url in self.integration_repos:
                repo_name = self.integration_repos[repo_url]
                upstream.integration_commit(self.config, session, git_gecko, git_wpt, gh_wpt,
                                            bz, repo_name)
            elif repo_url == self.landing_repo:
                upstream.landing_commit(self.config, session, git_gecko, git_wpt, gh_wpt, bz)


class TaskHandler(Handler):
    def __call__(self, body):
        logger.debug("Taskcluster task group finished %s" % body)
