import logging
import os
import sys
from logging import handlers
from celery.utils.log import get_task_logger

import settings

root = logging.getLogger()

configured = set()

@settings.configure
def setup(config):
    # Add a handler for stdout on the root logger
    log_dir = os.path.join(config["root"],
                           config["paths"]["logs"])
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setLevel(logging.INFO)

    file_handler = handlers.TimedRotatingFileHandler(os.path.join(log_dir, "sync.log"),
                                                     when="D", utc=True)
    file_handler.setLevel(logging.DEBUG)


    root_logger.addHandler(stream_handler)
    root_logger.addHandler(file_handler)


def get_logger(name):
    logger = logging.getLogger(name)
    return logger


setup()
