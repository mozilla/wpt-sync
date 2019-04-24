import os
import traceback
import time

import filelock

import bug
import env
import gh
import handlers
import log
import repos
import settings
from errors import RetryableError
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
            # If some other task is waiting on the lock, give it a chance to
            # run before this task completes. That means that manual commands
            # will always  win over celery tasks, which we want
            time.sleep(0.1)
        except Exception as e:
            logger.error(str(unicode(e).encode("utf8")))
            logger.error("".join(traceback.format_exc(e)))
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
    logger.info("Gecko repository: %s" % git_gecko.working_dir)
    logger.info("wpt repository: %s" % git_wpt.working_dir)
    logger.info("Tasks enabled: %s" % (", ".join(config["sync"]["enabled"].keys())))

    return git_gecko, git_wpt


@worker.task(bind=True, max_retries=6, retry_backoff=60, retry_backoff_max=3840)
@with_lock
def handle(self, task, body):
    handlers = get_handlers()
    if task in handlers:
        logger.info("Running task %s" % task)
        git_gecko, git_wpt = setup()
        try:
            handlers[task](git_gecko, git_wpt, body)
        except RetryableError as e:
            self.retry(exc=e.wrapped)
        except Exception as e:
            logger.error(body)
            logger.error("".join(traceback.format_exc(e)))
            raise
    else:
        logger.error("No handler for %s" % task)


@worker.task(bind=True, max_retries=6, retry_backoff=60, retry_backoff_max=3840)
@with_lock
@settings.configure
def land(self, config):
    git_gecko, git_wpt = setup()
    try:
        handlers.LandingHandler(config)(git_gecko, git_wpt)
    except RetryableError as e:
        self.retry(exc=e.wrapped)


@worker.task
@with_lock
@settings.configure
def cleanup(config):
    git_gecko, git_wpt = setup()
    handlers.CleanupHandler(config)(git_gecko, git_wpt)


@worker.task
@with_lock
@settings.configure
def retrigger(config):
    git_gecko, git_wpt = setup()
    handlers.RetriggerHandler(config)(git_gecko, git_wpt)
