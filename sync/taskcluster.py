import os
import requests
import shutil
import traceback
import uuid

import slugid

import log

QUEUE_BASE = "https://queue.taskcluster.net/v1/"
ARTIFACTS_BASE = "https://public-artifacts.taskcluster.net/"

logger = log.get_logger(__name__)


def normalize_task_id(task_id):
    # For some reason, pulse doesn't get the real
    # task ID, but some alternate encoding of it that doesn't
    # work anywhere else. So we have to first convert to the canonical
    # form.
    task_id = task_id.split("/", 1)[0]
    try:
        task_uuid = uuid.UUID(task_id)
    except ValueError:
        # This is probably alrady in the canonoical form
        return task_id

    return slugid.encode(task_uuid)


def is_suite(task, suite):
    t = task.get("task", {}).get("extra", {}).get("suite", {}).get("name", "")
    return t.startswith(suite)


def is_build(task):
    tags = task.get("tags")
    if tags:
        return tags.get("kind") == "build"


def filter_suite(tasks, suite):
    # expects return value from get_tasks_in_group
    return [t for t in tasks if is_suite(t, suite)]


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
    return wpt_tasks


def download_logs(tasks, destination, retry=5, raw=True, report=True):
    if not os.path.exists(destination):
        os.makedirs(destination)
    file_names = []
    if report:
        file_names.append("wptreport.json")
    if raw:
        file_names.append("wpt_raw.log")

    if not file_names:
        return []

    urls = [ARTIFACTS_BASE + "{task}/{run}/public/test_info//%s" % file_name
            for file_name in file_names]
    for task in tasks:
        status = task.get("status", {})
        for run in status.get("runs", []):
            for url in urls:
                params = {
                    "task": status["taskId"],
                    "run": run["runId"],
                }
                run["_log_paths"] = {}
                params["file_name"] = url.rsplit("/", 1)[1]
                log_url = url.format(**params)
                log_name = "{task}_{run}_{file_name}".format(**params)
                success = False
                logger.debug("Trying to download {}".format(log_url))
                log_path = os.path.abspath(os.path.join(destination, log_name))
                if not os.path.exists(log_path):
                    success = download(log_url, log_path, retry)
                else:
                    success = True
                if not success:
                    logger.warning("Failed to download log from {}".format(log_url))
                run["_log_paths"][params["file_name"]] = log_path


def download(log_url, log_path, retry):
    while retry > 0:
        try:
            logger.debug("Downloading from %s" % log_url)
            r = requests.get(log_url, stream=True)
            tmp_path = log_path + ".tmp"
            with open(tmp_path, 'wb') as f:
                r.raw.decode_content = True
                shutil.copyfileobj(r.raw, f)
            os.rename(tmp_path, log_path)
            return True
        except Exception as e:
            logger.warning(traceback.format_exc(e))
            retry -= 1
    return False
