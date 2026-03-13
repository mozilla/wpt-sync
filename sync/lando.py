import requests

from .env import Environment

env = Environment()


def hg2git(hg_hash: str) -> str:
    response = requests.get(env.config["lando"]["api_url"] + "/hg2git/firefox/" + hg_hash)
    data = response.json()
    assert isinstance(data, dict)
    assert isinstance(data["git_hash"], str)

    return data["git_hash"]


def git2hg(git_hash: str) -> str:
    response = requests.get(env.config["lando"]["api_url"] + "/git2hg/firefox/" + git_hash)
    data = response.json()
    assert isinstance(data, dict)
    assert isinstance(data["hg_hash"], str)

    return data["hg_hash"]
