import traceback

import requests

from . import log
from .env import Environment


logger = log.get_logger(__name__)

env = Environment()


def get_tree_status(project: str) -> str:
    try:
        r = requests.get(env.config["lando"]["tree_status_url"])
        r.raise_for_status()
        tree_status = r.json().get("result", {})
        if project in tree_status:
            return tree_status[project]["status"]
        raise ValueError(f"No tree status for project {project}")
    except Exception:
        logger.warning(traceback.format_exc())
        return ""


def is_open(project: str) -> bool:
    return get_tree_status(project) == "open"
