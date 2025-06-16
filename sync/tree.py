import traceback

import requests

from . import log


logger = log.get_logger(__name__)


def get_tree_status(project: str) -> str:
    try:
        r = requests.get("https://treestatus.mozilla-releng.net/trees2")
        r.raise_for_status()
        tree_status = r.json().get("result", [])
        for s in tree_status:
            if s["tree"] == project:
                return s["status"]
        raise ValueError(f"No tree status for project {project}")
    except Exception:
        logger.warning(traceback.format_exc())
        return ""


def is_open(project: str) -> bool:
    return get_tree_status(project) == "open"
