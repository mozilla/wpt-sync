import logging
import os
from logging import handlers
from celery.utils.log import get_task_logger

import settings

root = logging.getLogger()

configured = set()

# Add a handler for stdout on the root logger
logging.basicConfig(level=logging.DEBUG)


def setup_handlers(config, logger):
    name = logger.name
    base_name = name.split(".")[0]

    if base_name not in configured:
        log_dir = os.path.join(config["root"],
                               config["paths"]["logs"])
        if not os.path.exists(log_dir):
            os.makedirs(log_dir)
        base_logger = logging.getLogger(base_name)
        handler = handlers.WatchedFileHandler(os.path.join(log_dir,
                                                           "%s.log" % base_name))
        handler.setLevel(logging.DEBUG)
        base_logger.addHandler(handler)
        configured.add(base_name)
    configured.add(name)


@settings.configure
def get_logger(config, name):
    logger = get_task_logger(name)
    if name not in configured:
        setup_handlers(config, logger)
    return logger
