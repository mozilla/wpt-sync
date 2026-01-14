import requests

from .env import Environment
from . import log
from . import tc

env = Environment()

logger = log.get_logger(__name__)


def hg2git(hg_hash: str) -> str:
    session = requests.Session()
    try:
        response = tc.fetch_json(
            env.config["lando"]["api_url"] + env.config["lando"]["hg2git"] + hg_hash,
            session=session,
        )
    except requests.HTTPError as e:
        logger.warning(str(e))

    assert isinstance(response, dict)
    assert isinstance(response["git_hash"], str)

    return response["git_hash"]


def git2hg(git_hash: str) -> str:
    session = requests.Session()
    try:
        response = tc.fetch_json(
            env.config["lando"]["api_url"] + env.config["lando"]["git2hg"] + git_hash,
            session=session,
        )
    except requests.HTTPError as e:
        logger.warning(str(e))

    assert isinstance(response, dict)
    assert isinstance(response["hg_hash"], str)

    return response["hg_hash"]
