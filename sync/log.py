import logging
import os
import sys
from logging import handlers

import settings

root = logging.getLogger()


@settings.configure
def setup(config, force=False):
    # Add a handler for stdout on the root logger
    log_dir = os.path.join(config["root"],
                           config["paths"]["logs"])
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    lock_logger = logging.getLogger("filelock")
    lock_logger.setLevel(logging.INFO)

    if root_logger.handlers and not force:
        return

    while root_logger.handlers:
        # If we already have handlers set up for the root logger, don't add more
        root_logger.removeHandler(root_logger.handlers[0])

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setLevel(logging.INFO)

    basic_formatter = logging.Formatter('[%(asctime)s] %(levelname)s:%(name)s:%(message)s')
    stream_handler.setFormatter(basic_formatter)

    file_handler = handlers.TimedRotatingFileHandler(os.path.join(log_dir, "sync.log"),
                                                     when="midnight",
                                                     utc=True)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(basic_formatter)

    root_logger.addHandler(stream_handler)
    root_logger.addHandler(file_handler)


def get_logger(name):
    logger = logging.getLogger(name)
    return logger


setup()
