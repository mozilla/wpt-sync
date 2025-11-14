import requests
from . import tc

from . import log

LANDO_API_URL = "https://lando.moz.tools/api/"

logger = log.get_logger(__name__)


def hg2git(hg_hash: str) -> str:
    session = requests.Session()
    try:
        response = tc.fetch_json(LANDO_API_URL + "hg2git/firefox/" + hg_hash, session=session)
    except requests.HTTPError as e:
        logger.warning(str(e))

    assert isinstance(response, dict)
    assert isinstance(response["git_hash"], str)

    return response["git_hash"]


def git2hg(git_hash: str) -> str:
    session = requests.Session()
    try:
        response = tc.fetch_json(LANDO_API_URL + "git2hg/firefox/" + git_hash, session=session)
    except requests.HTTPError as e:
        logger.warning(str(e))

    assert isinstance(response, dict)
    assert isinstance(response["hg_hash"], str)

    return response["hg_hash"]
