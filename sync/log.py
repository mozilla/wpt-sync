import logging
import os
from logging import handlers

import settings

root = logging.getLogger()

configured = set()

# Add a handler for stdout on the root logger
logging.basicConfig(level=logging.DEBUG)


def setup_handlers(config, logger):
    name = logger.name
    base_name = name.split(".")[0]

    if base_name not in configured:
        base_logger = logging.getLogger(base_name)
        handler = handlers.WatchedFileHandler(os.path.join(config["paths"]["logs"], "%s.log" % base_name))
        handler.setLevel(logging.DEBUG)
        base_logger.addHandler(handler)
        configured.add(base_name)
    configured.add(name)


@settings.configure
def get_logger(config, name):
    logger = logging.getLogger(name)
    if name not in configured:
        setup_handlers(config, logger)
    return logger
