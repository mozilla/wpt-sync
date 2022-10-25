from . import log
MYPY = False
if MYPY:
    from typing import Any
    from typing import Optional
    from typing import Text

logger = log.get_logger(__name__)


class AbortError(Exception):
    def __init__(self, msg, cleanup=None, set_flag=None):
        # type: (Text, Optional[Any], Optional[Any]) -> None
        Exception.__init__(self, msg)
        self.message = msg
        self.cleanup = cleanup
        self.set_flag = set_flag


class RetryableError(Exception):
    def __init__(self, wrapped):
        self.wrapped = wrapped

    def __getattr__(self, name):
        return getattr(self.wrapped, name)
