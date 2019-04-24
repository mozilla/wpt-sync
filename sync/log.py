import logging
import os
import sys
from logging import handlers

from dockerflow.logging import JsonLogFormatter

import settings

root = logging.getLogger()


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

    basic_formatter = logging.Formatter('[%(asctime)s] %(levelname)s:%(name)s:%(message)s')
    stream_handler.setFormatter(basic_formatter)

    file_handler = handlers.TimedRotatingFileHandler(os.path.join(log_dir, "sync.log"),
                                                     when="D", utc=True)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(basic_formatter)

    json_formatter = JsonLogFormatter()

    json_file_handler = handlers.TimedRotatingFileHandler(os.path.join(log_dir, "sync.json.log"),
                                                          when="D", utc=True)
    json_file_handler.setLevel(logging.DEBUG)
    json_file_handler.setFormatter(json_formatter)

    root_logger.addHandler(stream_handler)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(json_file_handler)

    lock_logger = logging.getLogger("filelock")
    lock_logger.setLevel(logging.INFO)


def get_logger(name):
    logger = logging.getLogger(name)
    return logger


setup()
