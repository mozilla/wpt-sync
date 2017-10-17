import os
import requests
import shutil
import traceback

import log

QUEUE_BASE = "https://queue.taskcluster.net/v1/"
ARTIFACTS_BASE = "https://public-artifacts.taskcluster.net/"

logger = log.get_logger("upstream")


def is_suite(task, suite):
    t = task.get("task", {})
    metadata = t.get("metadata")
    if metadata and metadata.get("name"):
        return suite in metadata["name"]


def is_build(task):
    tags = task.get("tags")
    if tags:
        return tags.get("kind") == "build"


def is_completed(task):
    status = task.get("status")
    if status:
        return status.get("state") == "completed"


def filter_suite(tasks, suite):
    # expects return value from get_tasks_in_group
    return [t for t in tasks if is_suite(t, suite)]


def filter_completed(tasks):
    # expects return value from get_tasks_in_group
    # completed implies that it did not fail
    return [t for t in tasks if is_completed(t)]


def get_tasks_in_group(group_id):
    list_url = QUEUE_BASE + "task-group/" + group_id + "/list"

    r = requests.get(list_url, params={
        "limit": 200
    })
    reply = r.json()
    tasks = reply["tasks"]
    while "continuationToken" in reply:
        r = requests.get(list_url, params={
            "limit": 200,
            "continuationToken": reply["continuationToken"]
        })
        reply = r.json()
        tasks += reply["tasks"]
    return tasks


def get_wpt_tasks(taskgroup_id):
    tasks = get_tasks_in_group(taskgroup_id)
    wpt_tasks = filter_suite(tasks, "web-platform-tests")
    wpt_completed = filter_completed(wpt_tasks)
    return wpt_completed, wpt_tasks


def download_logs(tasks, destination, retry=5):
    if not os.path.exists(destination):
        os.makedirs(destination)
    url = ARTIFACTS_BASE + "{task}/{run}/public/test_info//wpt_raw.log"
    log_files = []
    for task in tasks:
        status = task.get("status", {})
        for run in status.get("runs", []):
            params = {
                "task": status["taskId"],
                "run": run["runId"],
            }
            log_url = url.format(**params)
            log_name = "live_backing-{task}_{run}.log".format(**params)
            success = False
            logger.debug("Trying to download {}".format(log_url))
            log_path = os.path.abspath(os.path.join(destination, log_name))
            if os.path.exists(log_path):
                continue
            while not success and retry > 0:
                try:
                    r = requests.get(log_url, stream=True)
                    tmp_path = log_path + ".tmp"
                    with open(tmp_path, 'wb') as f:
                        r.raw.decode_content = True
                        shutil.copyfileobj(r.raw, f)
                    os.rename(tmp_path, log_path)
                    success = True
                    log_files.append(log_path)
                except Exception as e:
                    logger.warning(traceback.format_exc(e))
                    retry -= 1
            if not success:
                logger.warning("Failed to download log from {}".format(log_url))
    return log_files
