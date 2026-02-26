import json
import urllib.request

from .env import Environment
from . import log

env = Environment()

logger = log.get_logger(__name__)


def git2hg(git_hash: str) -> str:
    response = urllib.request.urlopen(
        env.config["lando"]["api_url"] + "/git2hg/firefox/" + git_hash
    )  # nosec B310
    data = response.read()
    map = json.loads(data.decode("utf-8"))
    assert isinstance(map, dict)
    assert isinstance(map["hg_hash"], str)

    return map["hg_hash"]
