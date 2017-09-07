from worker import worker
import handlers
from settings import configure
import log

logger = log.get_logger(__name__)

handler_map = None


@configure
def get_handlers(config):
    global handler_map
    if handler_map is None:
        handler_map = {
            "github": handlers.GitHubHandler(config),
            "push": handlers.PushHandler(config),
            "task": handlers.TaskGroupHandler(config)
        }
    return handler_map


@worker.task
def handle(task, body):
    try:
        handlers = get_handlers()
        if task in handlers:
            logger.info("Running task %s" % task)
            handlers[task](body)
        else:
            logger.error("No handler for %s" % task)
    except Exception as e:
        print str(unicode(e).encode("utf8"))
        raise
