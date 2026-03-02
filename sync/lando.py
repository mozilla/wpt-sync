import json
import urllib.request

from .env import Environment

env = Environment()


def hg2git(hg_hash: str) -> str:
    response = urllib.request.urlopen(env.config["lando"]["api_url"] + "/hg2git/firefox/" + hg_hash)  # nosec B310
    data = response.read()
    map = json.loads(data.decode("utf-8"))
    assert isinstance(map, dict)
    assert isinstance(map["git_hash"], str)

    return map["git_hash"]


def git2hg(git_hash: str) -> str:
    response = urllib.request.urlopen(
        env.config["lando"]["api_url"] + "/git2hg/firefox/" + git_hash
    )  # nosec B310
    data = response.read()
    map = json.loads(data.decode("utf-8"))
    assert isinstance(map, dict)
    assert isinstance(map["hg_hash"], str)

    return map["hg_hash"]
