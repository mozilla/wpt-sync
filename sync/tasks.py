import bug
import gh
import handlers
import log
import model
import repos
from model import session_scope
import settings
from worker import worker

logger = log.get_logger(__name__)

handler_map = None


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
    model.configure(config)
    session = model.session(expire_on_commit=False)

    gecko_repo = repos.Gecko(config)
    git_gecko = gecko_repo.repo()
    wpt_repo = repos.WebPlatformTests(config)
    git_wpt = wpt_repo.repo()
    gh_wpt = gh.GitHub(config["web-platform-tests"]["github"]["token"],
                       config["web-platform-tests"]["repo"]["url"])

    bz = bug.MockBugzilla(config)

    return session, git_gecko, git_wpt, gh_wpt, bz


def try_task(f):
    def inner(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except Exception as e:
            logger.error(str(unicode(e).encode("utf8")))
            raise
    inner.__name__ = f.__name__
    inner.__doc__ = f.__doc__
    return inner


@worker.task
@try_task
def handle(task, body):
    handlers = get_handlers()
    if task in handlers:
        logger.info("Running task %s" % task)
        session, git_gecko, git_wpt, gh_wpt, bz = setup()
        try:
            handlers[task](session, git_gecko, git_wpt, gh_wpt, bz, body)
        except Exception:
            logger.error(body)
            raise
    else:
        logger.error("No handler for %s" % task)


@worker.task
@try_task
@settings.configure
def land(config):
    session, git_gecko, git_wpt, gh_wpt, bz = setup()
    handlers.LandingHandler(config)(session, git_gecko, git_wpt, gh_wpt, bz)


@worker.task
@try_task
@settings.configure
def cleanup(config):
    session, git_gecko, git_wpt, gh_wpt, bz = setup()
    handlers.CleanupHandler(config)(session, git_gecko, git_wpt, gh_wpt, bz)
