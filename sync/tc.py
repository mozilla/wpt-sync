import os
import requests
import shutil
import time
import traceback
import uuid
from collections import defaultdict
from datetime import datetime, timedelta

import newrelic.agent
import slugid
import taskcluster

from . import log
from .env import Environment
from .errors import RetryableError
from .threadexecutor import ThreadExecutor
import six

MYPY = False
if MYPY:
    from typing import Any, Callable, Dict, Iterator, List, Optional, Set, Text, Tuple, Union
    Task = Dict[str, Dict[str, Any]]


TASKCLUSTER_ROOT_URL = "https://firefox-ci-tc.services.mozilla.com"
QUEUE_BASE = TASKCLUSTER_ROOT_URL + "/api/queue/v1/"
INDEX_BASE = TASKCLUSTER_ROOT_URL + "/api/index/v1/"
_DATE_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"

TREEHERDER_BASE = "https://treeherder.mozilla.org/"

SUCCESS = "completed"
FAIL = "failed"
EXCEPTION = "exception"
UNSCHEDULED = "unscheduled"
RUNNING = "running"
PENDING = "pending"

logger = log.get_logger(__name__)

env = Environment()


class TaskclusterClient:
    def __init__(self):
        self._queue = None

    @property
    def queue(self):
        # Only used for retriggers which always use the new URL
        if not self._queue:
            self._queue = taskcluster.Queue({
                "credentials": {
                    "clientId": env.config["taskcluster"]["client_id"],
                    "accessToken": env.config["taskcluster"]["token"]
                },
                "rootUrl": TASKCLUSTER_ROOT_URL,
            })
        return self._queue

    def retrigger(self, task_id, count=1, retries=5):
        logger.info("Retriggering task %s" % task_id)
        payload = self.queue.task(task_id)
        now = taskcluster.fromNow("0 days")
        created = datetime.strptime(payload["created"], _DATE_FMT)
        deadline = datetime.strptime(payload["deadline"], _DATE_FMT)
        expiration = datetime.strptime(payload["expires"], _DATE_FMT)
        to_dead = deadline - created
        to_expire = expiration - created
        payload["deadline"] = taskcluster.stringDate(
            taskcluster.fromNow("%d days %d seconds" % (to_dead.days, to_dead.seconds), now)
        )
        payload["expires"] = taskcluster.stringDate(
            taskcluster.fromNow("%d days %d seconds" % (to_expire.days, to_expire.seconds), now)
        )
        payload["created"] = taskcluster.stringDate(now)
        payload["retries"] = 0
        rv = []
        while count > 0:
            new_id = slugid.nice()
            r = retries
            while r > 0:
                try:
                    rv.append(self.queue.createTask(new_id, payload))
                    break
                except Exception as e:
                    r -= 1
                    logger.warning(traceback.format_exc(e))
            count -= 1
        return rv or None


def normalize_task_id(task_id):
    # type: (Text) -> Text
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


def parse_job_name(job_name):
    if job_name.startswith("test-"):
        job_name = job_name[len("test-"):]
    if "web-platform-tests" in job_name:
        job_name = job_name[:job_name.index("web-platform-tests")]
    job_name = job_name.rstrip("-")

    job_name = job_name.replace("/", "-")

    return job_name


def result_from_run(run):
    result_map = {"completed": "success",
                  "failed": "fail"}

    state = run.get("state")

    if state in result_map:
        return result_map[state]

    if state == "exception":
        if run.get("reasonResolved") == "canceled":
            return "canceled"
        if run.get("reasonResolved") == "superseded":
            return "superseded"
        return "exception"

    return "unknown"


class TaskGroup:
    def __init__(self, taskgroup_id, tasks=None):
        # type: (Text, Optional[Any]) -> None
        self.taskgroup_id = taskgroup_id
        self._tasks = tasks

    @property
    def tasks(self):
        # type: () -> List[Task]
        if self._tasks:
            return self._tasks

        list_url = QUEUE_BASE + "task-group/" + self.taskgroup_id + "/list"

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
        self._tasks = tasks
        return self._tasks

    def refresh(self):
        # type: () -> List[Task]
        self._tasks = None
        return self.tasks

    def tasks_by_id(self):
        # type: () -> Dict[Text, Task]
        return {item["status"]["taskId"]: item for item in self.tasks}

    def view(self, filter_fn=None):
        # type: (Optional[Callable]) -> TaskGroupView
        return TaskGroupView(self, filter_fn)


class TaskGroupView:
    def __init__(self, taskgroup, filter_fn):
        # type: (TaskGroup, Optional[Callable[[Task], bool]]) -> None
        self.taskgroup = taskgroup
        self.filter_fn = (filter_fn if filter_fn is not None
                          else lambda x: bool(x))  # type: Callable[[Task], bool]
        self._tasks = None  # type: Optional[List[Task]]

    def __bool__(self):
        return bool(self.tasks)

    def __len__(self):
        # type: () -> int
        return len(self.tasks)

    def __iter__(self):
        # type: () -> Iterator[Task]
        yield from self.tasks

    @property
    def tasks(self):
        # type: () -> List[Task]
        if self._tasks:
            return self._tasks

        self._tasks = [item for item in self.taskgroup.tasks
                       if self.filter_fn(item)]
        assert self._tasks is not None
        return self._tasks

    def refresh(self):
        self._tasks = None
        self.taskgroup.refresh()
        return self.tasks

    def incomplete_tasks(self,
                         allow_unscheduled=False,  # type: bool
                         ):
        # type: (...) -> Iterator[Task]
        tasks_by_id = self.taskgroup.tasks_by_id()
        for task in self.tasks:
            if task_is_incomplete(task, tasks_by_id, allow_unscheduled):
                yield task

    def failed_builds(self):
        # type: () -> TaskGroupView
        """Return the builds that failed"""
        builds = self.filter(is_build)
        return builds.filter(is_status_fn({FAIL, EXCEPTION}))

    def filter(self, filter_fn):
        # type: (Callable[[Task], bool]) -> TaskGroupView
        def combined_filter(task  # type: Task
                            ):
            # type: (...) -> bool
            return self.filter_fn(task) and filter_fn(task)

        return self.taskgroup.view(combined_filter)

    def is_complete(self, allow_unscheduled=False):
        # type: (bool) -> bool
        return not any(self.incomplete_tasks(allow_unscheduled))

    def by_name(self):
        # type: () -> Dict[Text, List[Task]]
        rv = defaultdict(list)
        for task in self.tasks:
            name = task.get("task", {}).get("metadata", {}).get("name")
            if name:
                rv[name].append(task)
        return rv

    @newrelic.agent.function_trace()
    def download_logs(self, destination,  # type: Text
                      file_names,  # type: List[Text]
                      retry=5  # type: int
                      ):
        # type (...) -> None
        if not os.path.exists(destination):
            os.makedirs(destination)

        if not file_names:
            return []

        logger.info("Downloading logs to %s" % destination)
        t0 = time.time()
        executor = ThreadExecutor(8, work_fn=get_task_artifacts, init_fn=start_session)
        errors = executor.run([((), {
            "destination": destination,
            "task": item,
            "file_names": file_names,
            "retry": retry
        }) for item in self.tasks])
        # TODO: not sure if we can avoid tolerating some errors here, but
        # there is probably some sign of badness less than all the downloads
        # erroring
        for error in errors:
            logger.warning(traceback.format_exc(error))
        if len(errors) == len(self.tasks):
            raise RetryableError("Downloading logs all failed")
        logger.info("Downloading logs took %s" % (time.time() - t0))


def start_session():
    return {"session": requests.Session()}


def get_task_artifacts(destination,  # type: Text
                       task,  # type: Task
                       file_names,  # type: List[Text]
                       session,  # type: Optional[requests.Session]
                       retry  # type: int
                       ):
    status = task.get("status", {})
    if not status.get("runs"):
        logger.debug("No runs for task %s" % status["taskId"])
        return
    artifacts_base_url = QUEUE_BASE + "task/%s/artifacts" % status["taskId"]
    try:
        artifacts = fetch_json(artifacts_base_url, session=session)
    except requests.HTTPError as e:
        logger.warning(str(e))
    artifact_urls = ["{}/{}".format(artifacts_base_url, item["name"])
                     for item in artifacts["artifacts"]
                     if any(item["name"].endswith("/" + file_name)
                            for file_name in file_names)]

    run = status["runs"][-1]
    if "_log_paths" not in run:
        run["_log_paths"] = {}
    for url in artifact_urls:
        params = {
            "task": status["taskId"],
            "file_name": url.rsplit("/", 1)[1]
        }
        log_name = "{task}_{file_name}".format(**params)
        success = False
        logger.debug(f"Trying to download {url}")
        log_path = os.path.abspath(os.path.join(destination, log_name))
        if not os.path.exists(log_path):
            success = download(url, log_path, retry, session=session)
        else:
            success = True
        if not success:
            logger.warning(f"Failed to download log from {url}")
        run["_log_paths"][params["file_name"]] = log_path


def task_is_incomplete(task,  # type: Task
                       tasks_by_id,  # type: Dict[Text, Task]
                       allow_unscheduled,  # type: bool
                       ):
    # type: (...) -> bool
    status = task.get("status", {}).get("state", PENDING)
    if status in (PENDING, RUNNING):
        return True
    elif status == UNSCHEDULED:
        if not allow_unscheduled:
            return True
        # If the task is unscheduled, we may regard it as complete if
        # all dependencies are complete
        # TODO: is there a race condition here where dependencies can be
        # complete and successful but this task has not yet been scheduled?

        # A task can depend on its image; it's OK to ignore this for our purposes
        image = task.get("task", {}).get("payload", {}).get("image", {}).get("taskId")
        dependencies = [item for item in task.get("task", {}).get("dependencies", [])
                        if item != image]
        if not dependencies:
            return True
        # Not sure how to handle a case where a dependent doesn't exist,  ignore it
        return any(task_is_incomplete(tasks_by_id[dependent_id], tasks_by_id,
                                      allow_unscheduled)
                   for dependent_id in dependencies
                   if dependent_id in tasks_by_id)
    return False


def is_suite(suite,  # type: Text
             task,  # type: Dict[Text, Dict[Text, Any]]
             ):
    # type: (...) -> bool
    t = task.get("task", {}).get("extra", {}).get("suite", {})
    if isinstance(t, dict):
        t = t.get("name", "")
    return t.startswith(suite)


def is_suite_fn(suite):
    # type: (str) -> Callable
    return lambda x: is_suite(suite, x)


def check_tag(task,  # type: Task
              tag,  # type: Text
              ):
    # type: (...) -> bool
    tags = task.get("task", {}).get("tags")
    if tags:
        return tags.get("kind") == tag
    return False


def is_test(task):
    # type: (Task) -> bool
    return check_tag(task, "test")


def is_build(task):
    # type: (Task) -> bool
    return check_tag(task, "build")


def is_status(statuses,  # type: Union[Set[Text], Text]
              task,  # type: Task
              ):
    # type: (...) -> bool
    state = task.get("status", {}).get("state")  # type: Optional[Text]
    return state is not None and state in statuses


def is_status_fn(statuses):
    # type: (Union[Set[Text], Text]) -> Callable
    if isinstance(statuses, (str, str)):
        statuses = {statuses}
    return lambda x: is_status(statuses, x)


def lookup_index(index_name):
    # type: (Text) -> Optional[Text]
    if index_name is None:
        return None

    idx_url = INDEX_BASE + "task/" + index_name
    resp = requests.get(idx_url)
    resp.raise_for_status()
    idx = resp.json()
    task_id = idx.get("taskId")

    if task_id:
        return task_id
    logger.warning("Task not found from index: {}\n{}".format(index_name, idx.get("message", "")))
    return task_id


def lookup_treeherder(project, revision):
    # type: (Text, Text) -> Optional[Text]
    push_data = fetch_json(TREEHERDER_BASE + f"api/project/{project}/push/",
                           params={"revision": revision})

    pushes = push_data.get("results", [])
    push_id = pushes[0].get("id") if pushes else None
    if push_id is None:
        return None

    jobs_data = fetch_json(TREEHERDER_BASE + "api/jobs/",
                           {"push_id": push_id})
    property_names = jobs_data["job_property_names"]
    idx_name = property_names.index("job_type_name")
    idx_task = property_names.index("task_id")
    decision_tasks = [item for item in jobs_data["results"]
                      if item[idx_name] == "Gecko Decision Task"]
    return decision_tasks[-1][idx_task]


def get_task(task_id):
    # type: (Text) -> Optional[Dict[Text, Any]]
    if task_id is None:
        return
    task_url = QUEUE_BASE + "task/" + task_id
    r = requests.get(task_url)
    task = r.json()
    if task.get("taskGroupId"):
        return task
    logger.warning("Task {} not found: {}".format(task_id, task.get("message", "")))
    return None


def get_task_status(task_id):
    # type: (Text) -> Optional[Dict[Text, Any]]
    if task_id is None:
        return
    status_url = f"{QUEUE_BASE}task/{task_id}/status"
    r = requests.get(status_url)
    status = r.json()
    if status.get("status"):
        return status["status"]
    logger.warning("Task {} not found: {}".format(task_id, status.get("message", "")))
    return None


def download(log_url, log_path, retry, session=None):
    # type: (Text, Text, int, Optional[requests.Session]) -> bool
    if session is None:
        session = requests.Session()
    while retry > 0:
        try:
            logger.debug("Downloading from %s" % log_url)
            t0 = time.time()
            resp = session.get(log_url, stream=True)
            if resp.status_code < 200 or resp.status_code >= 300:
                logger.warning("Download failed with status %s" % resp.status_code)
                retry -= 1
                continue
            tmp_path = log_path + ".tmp"
            with open(tmp_path, 'wb') as f:
                resp.raw.decode_content = True
                shutil.copyfileobj(resp.raw, f)
            os.rename(tmp_path, log_path)
            logger.debug("Download took %s" % (time.time() - t0))
            return True
        except Exception:
            logger.warning(traceback.format_exc())
            retry -= 1
    return False


def fetch_json(url,  # type: Text
               params=None,  # type: Dict[Text, Text]
               session=None  # type: requests.Session
               ):
    # type (...) -> Union[Dict[Text, Any], List[Any]]
    if session is None:
        session = requests.Session()
    t0 = time.time()
    logger.debug("Getting json from %s" % url)
    headers = {
        'Accept': 'application/json',
        'User-Agent': 'wpt-sync',
    }
    resp = session.get(url=url, params=params, headers=headers, timeout=30)
    resp.raise_for_status()
    logger.debug("Getting json took %s" % (time.time() - t0))
    return resp.json()


def get_taskgroup_id(project, revision):
    # type: (Text, Text) -> Tuple[Text, Text, List[Dict[Text, Any]]]
    idx = f"gecko.v2.{project}.revision.{revision}.firefox.decision"
    try:
        task_id = lookup_index(idx)
    except requests.HTTPError:
        task_id = lookup_treeherder(project, revision)
    if task_id is None:
        raise ValueError("Failed to look up task id from index %s" % idx)
    status = get_task_status(task_id)
    if status is None:
        raise ValueError("Failed to look up status for task %s" % task_id)

    state = status["state"]
    runs = status["runs"]

    return (task_id, state, runs)


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
