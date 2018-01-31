import os
import requests
import shutil
import traceback
import urlparse
import uuid
from datetime import datetime, timedelta

import slugid

import log
from env import Environment

QUEUE_BASE = "https://queue.taskcluster.net/v1/"
ARTIFACTS_BASE = "https://public-artifacts.taskcluster.net/"
TREEHERDER_BASE = "https://treeherder.mozilla.org/"

logger = log.get_logger(__name__)

env = Environment()


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


def is_complete(tasks):
    return not any(task.get("status", {}).get("state", "pending") in ("pending", "running")
                   for task in tasks)


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
    logger.info("Getting wpt tasks for taskgroup %s" % taskgroup_id)
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
    logger.info("Downloading logs to %s" % destination)
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


def parse_job_name(job_name):
    if job_name.startswith("test-"):
        job_name = job_name[len("test-"):]
    if "web-platform-tests" in job_name:
        job_name = job_name[:job_name.index("web-platform-tests")]
    job_name = job_name.rstrip("-")

    job_name = job_name.replace("/", "-")

    return job_name


def fetch_json(url, params=None):
    headers = {
        'Accept': 'application/json',
        'User-Agent': 'wpt-sync',
    }
    response = requests.get(url=url, params=params, headers=headers, timeout=30)
    response.raise_for_status()
    return response.json()


def get_taskgroup_id(project, revision):
    resultset_url = urlparse.urljoin(TREEHERDER_BASE,
                                     "/api/project/%s/resultset/" % project)
    resultset_params = {
        'revision': revision,
    }

    revision_data = fetch_json(resultset_url, resultset_params)
    result_set = revision_data["results"][0]["id"]

    jobs_url = urlparse.urljoin(TREEHERDER_BASE, "/api/project/%s/jobs/" % project)
    jobs_params = {
        'result_set_id': result_set,
        'count': 2000,
        'exclusion_profile': 'false',
        'job_type_name': "Gecko Decision Task",
    }
    jobs_data = fetch_json(jobs_url, params=jobs_params)

    if not jobs_data["results"]:
        logger.info("No decision task found for %s %s" % (project, revision))
        return None, None

    if len(jobs_data["results"]) > 1:
        logger.warning("Multiple decision tasks found for %s" % revision)

    job_id = jobs_data["results"][-1]["id"]

    job_url = urlparse.urljoin(TREEHERDER_BASE, "/api/project/%s/jobs/%s/" %
                               (project, job_id))
    job_data = fetch_json(job_url)

    return normalize_task_id(job_data["taskcluster_metadata"]["task_id"]), job_data["result"]


def cleanup():
    base_path = os.path.join(env.config["root"], env.config["paths"]["try_logs"])
    for repo_dir in os.listdir(base_path):
        repo_path = os.path.join(base_path, repo_dir)
        if not os.path.isdir(repo_path):
            continue
        for rev_dir in os.listdir(repo_path):
            rev_path = os.path.join(repo_path, rev_dir)
            if not os.path.isdir(rev_path):
                continue
            now = datetime.now()
            # Data hasn't been touched in five days
            if (datetime.fromtimestamp(os.stat(rev_path).st_mtime) <
                now - timedelta(days=5)):
                logger.info("Removing downloaded logs without recent activity %s" % rev_path)
                shutil.rmtree(rev_path)
