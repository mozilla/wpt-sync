import traceback

import requests

import log


logger = log.get_logger("tree")


def get_tree_status(project):
    try:
        r = requests.get("https://treestatus.mozilla-releng.net/trees2")
        r.raise_for_status()
        tree_status = r.json().get("result", [])
        for s in tree_status:
            if s["tree"] == project:
                return s["status"]
    except Exception as e:
        logger.warning(traceback.format_exc(e))


def is_open(project):
    return get_tree_status(project) == "open"
