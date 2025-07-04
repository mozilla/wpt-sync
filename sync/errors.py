from . import log
from typing import Any
from typing import Optional

logger = log.get_logger(__name__)


class AbortError(Exception):
    def __init__(
        self, msg: str, cleanup: Optional[Any] = None, set_flag: Optional[Any] = None
    ) -> None:
        Exception.__init__(self, msg)
        self.message = msg
        self.cleanup = cleanup
        self.set_flag = set_flag


class RetryableError(Exception):
    def __init__(self, wrapped: Exception):
        self.wrapped = wrapped

    def __getattr__(self, name: str) -> Any:
        return getattr(self.wrapped, name)
