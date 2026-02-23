import json
import urllib.request

from .env import Environment
from . import log

env = Environment()

logger = log.get_logger(__name__)


def hg2git(hg_hash: str) -> str:
    response = urllib.request.urlopen(env.config["lando"]["api_url"] + "/hg2git/firefox/" + hg_hash)
    data = response.read()
    map = json.loads(data.decode("utf-8"))
    assert isinstance(map, dict)
    assert isinstance(map["git_hash"], str)

    return map["git_hash"]


def git2hg(git_hash: str) -> str:
    response = urllib.request.urlopen(
        env.config["lando"]["api_url"] + "/git2hg/firefox/" + git_hash
    )
    data = response.read()
    map = json.loads(data.decode("utf-8"))
    assert isinstance(map, dict)
    assert isinstance(map["hg_hash"], str)

    return map["hg_hash"]
