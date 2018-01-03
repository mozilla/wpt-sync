import os

import filelock

import bug
import env
import gh
import handlers
import log
import repos
import settings
from worker import worker


logger = log.get_logger(__name__)

handler_map = None

_lock = None

@settings.configure
def setup_lock(config):
    global _lock
    if _lock is None:
        path = os.path.join(config["root"], "sync.lock")
        logger.info("Using lockfile at path %s" % path)
        _lock = filelock.FileLock(path)


def with_lock(f):
    global _lock
    def inner(*args, **kwargs):
        if _lock is None:
            setup_lock()
        try:
            with _lock:
                return f(*args, **kwargs)
        except Exception as e:
            logger.error(str(unicode(e).encode("utf8")))
            raise
    inner.__name__ = f.__name__
    inner.__doc__ = f.__doc__
    return inner


@settings.configure
def get_handlers(config):
    global handler_map
    if handler_map is None:
        handler_map = {
            "github": handlers.GitHubHandler(config),
            "push": handlers.PushHandler(config),
            "task": handlers.TaskHandler(config),
            "taskgroup": handlers.TaskGroupHandler(config),
        }
    return handler_map


@settings.configure
def setup(config):
    gecko_repo = repos.Gecko(config)
    git_gecko = gecko_repo.repo()
    wpt_repo = repos.WebPlatformTests(config)
    git_wpt = wpt_repo.repo()
    gh_wpt = gh.GitHub(config["web-platform-tests"]["github"]["token"],
                       config["web-platform-tests"]["repo"]["url"])

    bz = bug.Bugzilla(config)

    env.set_env(config, bz, gh_wpt)
    return git_gecko, git_wpt


@worker.task
@with_lock
def handle(task, body):
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


@worker.task
@with_lock
@settings.configure
def land(config):
    git_gecko, git_wpt = setup()
    handlers.LandingHandler(config)(git_gecko, git_wpt)


@worker.task
@with_lock
@settings.configure
def cleanup(config):
    git_gecko, git_wpt = setup()
    handlers.CleanupHandler(config)(git_gecko, git_wpt)
