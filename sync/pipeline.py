import inspect
import traceback

import log

logger = log.get_logger(__name__)


class AbortError(Exception):
    def __init__(self, msg, cleanup=None, set_flag=None):
        Exception.__init__(self, msg)
        self.cleanup = cleanup
        self.set_flag = set_flag
