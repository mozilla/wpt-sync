import itertools
import random
import re
import time
from io import StringIO

import github
import newrelic
import six
from six.moves import urllib


from . import log
from .env import Environment
from datetime import datetime
from github.Branch import Branch
from github.Commit import Commit
from github.PullRequest import PullRequest
from github.Repository import Repository
from typing import Any, Dict, List, Optional, Tuple, Union

logger = log.get_logger(__name__)
env = Environment()


class CheckRun(github.GithubObject.NonCompletableGithubObject):
    def _initAttributes(self):
        self._id = github.GithubObject.NotSet
        self._status = github.GithubObject.NotSet
        self._name = github.GithubObject.NotSet
        self._conclusion = github.GithubObject.NotSet
        self._url = github.GithubObject.NotSet

    def _useAttributes(self, attributes):
        if "id" in attributes:
            self._id = self._makeIntAttribute(attributes["id"])
        if "status" in attributes:
            self._status = self._makeStringAttribute(attributes["status"])
        if "name" in attributes:
            self._name = self._makeStringAttribute(attributes["name"])
        if "conclusion" in attributes:
            self._conclusion = self._makeStringAttribute(attributes["conclusion"])
        if "url" in attributes:
            self._url = self._makeStringAttribute(attributes["url"])
        if "head_sha" in attributes:
            self._head_sha = self._makeStringAttribute(attributes["head_sha"])

    @property
    def id(self):
        return self._id.value

    @property
    def status(self):
        return self._status.value

    @property
    def name(self):
        return self._name.value

    @property
    def conclusion(self):
        return self._conclusion.value

    @property
    def url(self):
        return self._url.value

    @property
    def head_sha(self):
        return self._head_sha.value


class GitHub:
    def __init__(self, token: str, url: str) -> None:
        self.gh = github.Github(token)
        self.repo_name = urllib.parse.urlsplit(url).path.lstrip("/")
        self.pr_cache: Dict[int, PullRequest] = {}
        self._repo: Optional[Repository] = None

    def pr_url(self, pr_id: int) -> str:
        return ("%s/pull/%s" %
                (env.config["web-platform-tests"]["repo"]["url"],
                 pr_id))

    def load_pull(self, data: Dict[str, Any]) -> None:
        pr = self.gh.create_from_raw_data(github.PullRequest.PullRequest, data)
        self.pr_cache[pr.number] = pr

    @property
    def repo(self) -> Repository:
        if self._repo is None:
            self._repo = self.gh.get_repo(self.repo_name)
        assert self._repo is not None
        return self._repo

    def get_pull(self, id: int) -> PullRequest:
        id = int(id)
        if id not in self.pr_cache:
            pr = self.repo.get_pull(id)
            if pr is None:
                raise ValueError("No pull request with id %s" % id)
            self.pr_cache[id] = pr
        return self.pr_cache[id]

    def create_pull(self,
                    title: str,
                    body: str,
                    base: str,
                    head: str
                    ) -> int:
        try:
            pr = self.repo.create_pull(title=title, body=body, base=base, head=head)
            logger.info("Created PR %s" % pr.number)
        except github.GithubException:
            # Check if there's already a PR for this head
            user = self.repo_name.split("/")[0]
            pulls = self.repo.get_pulls(head=f"{user}:{head}")
            entries = list(pulls)
            if len(entries) == 0:
                raise
            elif len(entries) > 1:
                raise ValueError("Found multiple existing pulls for branch")
            pr = pulls[0]
        self.add_labels(pr.number, "mozilla:gecko-sync")
        self.pr_cache[pr.number] = pr
        return pr.number

    def has_branch(self, name: str) -> bool:
        return self._get_branch(name) is not None

    def _get_branch(self, name: str) -> Optional[Branch]:
        try:
            return self.repo.get_branch(name)
        except (github.GithubException, github.UnknownObjectException):
            return None

    def get_status(self,
                   pr_id: int,
                   context: str
                   ) -> Optional[str]:
        pr = self.get_pull(pr_id)
        head_commit = self.repo.get_commit(pr.head.ref)
        statuses = [item for item in head_commit.get_statuses()
                    if item.context == context]
        statuses.sort(key=lambda x: -x.id)
        if statuses:
            return statuses[0].state
        return None

    def set_status(self,
                   pr_id: int,
                   status: str,
                   target_url: Optional[str],
                   description: Optional[str],
                   context: str
                   ):
        pr = self.get_pull(pr_id)
        head_commit = self.repo.get_commit(pr.head.ref)
        kwargs = {}
        if target_url is not None:
            kwargs["target_url"] = target_url
        if description is not None:
            kwargs["description"] = description
        head_commit.create_status(status,
                                  context=context,
                                  **kwargs)

    def add_labels(self,
                   pr_id: int,
                   *labels: str
                   ):
        logger.debug("Adding labels {} to PR {}".format(", ".join(labels), pr_id))
        pr_id = self._convert_pr_id(pr_id)
        issue = self.repo.get_issue(pr_id)
        issue.add_to_labels(*labels)

    def remove_labels(self,
                      pr_id: int,
                      *labels: str
                      ):
        logger.debug(f"Removing labels {labels} from PR {pr_id}")
        pr_id = self._convert_pr_id(pr_id)
        issue = self.repo.get_issue(pr_id)
        for label in labels:
            try:
                issue.remove_from_labels(label)
            except github.GithubException as e:
                if e.data.get("message", "") != "Label does not exist":
                    logger.warning("Error handling label removal: %s" % e)
                    newrelic.agent.record_exception()

    def _convert_pr_id(self,
                       pr_id: Union[str, int]
                       ):
        # (...) -> int
        if not isinstance(pr_id, int):
            try:
                pr_id = int(pr_id)
            except ValueError:
                raise ValueError('PR ID is not a valid number')
        return pr_id

    def required_checks(self, branch_name: str) -> List[str]:
        branch = self._get_branch(branch_name)
        if branch is None:
            # TODO: Maybe raise an exception here
            return []
        return (branch.raw_data
                .get("protection", {})
                .get("required_status_checks", {})
                .get("contexts", []))

    def get_check_runs(self, pr_id: int) -> Dict[str, Dict[str, Any]]:
        pr = self.get_pull(pr_id)
        check_runs = list(self._get_check_runs(pr.head.sha))
        required_contexts = self.required_checks(pr.base.ref)
        rv: Dict[str, Dict[str, Any]] = {}
        id_by_name: Dict[str, int] = {}
        for item in check_runs:
            if item.name in id_by_name and item.id < id_by_name[item.name]:
                continue
            id_by_name[item.name] = item.id
            rv[item.name] = {
                "status": item.status,
                "conclusion": item.conclusion,
                "url": item.url,
                "required": item.name in required_contexts,
                "head_sha": item.head_sha
            }
        return rv

    def _get_check_runs(self, sha1, check_name=None):
        query = []
        if check_name:
            query.append(("check_name", check_name))
        commit = self.repo.get_commit(sha1)
        url = commit._parentUrl(commit.url) + "/" + commit.sha + "/check-runs"
        headers = {"Accept": "application/vnd.github.antiope-preview+json"}
        return github.PaginatedList.PaginatedList(CheckRun,
                                                  commit._requester,
                                                  url,
                                                  query,
                                                  headers=headers,
                                                  list_item="check_runs")

    def pull_state(self, pr_id: int) -> str:
        pr = self.get_pull(pr_id)
        return pr.state

    def reopen_pull(self, pr_id: int) -> None:
        pr = self.get_pull(pr_id)
        pr.edit(state="open")

    def close_pull(self, pr_id: int) -> None:
        pr = self.get_pull(pr_id)
        # Perhaps?
        # issue = self.repo.get_issue(pr_id)
        # issue.add_to_labels("mozilla:backed-out")
        pr.edit(state="closed")

    def is_approved(self, pr_id: int) -> bool:
        pr = self.get_pull(pr_id)
        reviews = pr.get_reviews()
        # We get a chronological list of all reviews, so we want to
        # check if the last review by any reviewer was in the approved
        # state
        review_by_reviewer = {}
        for review in reviews:
            review_by_reviewer[review.user.login] = review.state
        return "APPROVED" in list(review_by_reviewer.values())

    def merge_sha(self, pr_id: int) -> Optional[str]:
        pr = self.get_pull(pr_id)
        if pr.merged:
            return pr.merge_commit_sha
        return None

    def is_mergeable(self, pr_id: int) -> bool:
        mergeable = None
        count = 0
        while mergeable is None and count < 6:
            # GitHub sometimes doesn't have the mergability information ready;
            # In this case mergeable is None and we need to wait and try again
            pr = self.get_pull(pr_id)
            mergeable = pr is not None and pr.mergeable
            if mergeable is None:
                time.sleep(2**count)
                count += 1
                del self.pr_cache[pr_id]
        return bool(mergeable)

    def merge_pull(self, pr_id: int) -> str:
        pr = self.get_pull(pr_id)
        merge_status = pr.merge(merge_method="rebase")
        return merge_status.sha

    def pr_for_commit(self, sha: str) -> Optional[int]:
        logger.info("Looking up PR for commit %s" % sha)
        owner, repo = self.repo_name.split("/")
        prs = list(self.gh.search_issues(query=f"is:pr repo:{owner}/{repo} sha:{sha}"))
        if len(prs) == 0:
            return None

        if len(prs) > 1:
            logger.warning("Got multiple PRs related to commit %s: %s" %
                           (sha, ", ".join(str(item.number) for item in prs)))
            prs = sorted(prs, key=lambda x: x.number)

        return prs[0].number

    def get_commits(self, pr_id: int) -> List[Commit]:
        return list(self.get_pull(pr_id).get_commits())

    def cleanup_pr_body(self, text: Optional[str]) -> Optional[str]:
        if text is None:
            return None
        r = re.compile(re.escape("<!-- Reviewable:start -->") + ".*" +
                       re.escape("<!-- Reviewable:end -->"), re.DOTALL)
        return r.sub("", text)

    def _construct_check_data(self,
                              name: str,
                              commit_sha: Optional[str] = None,
                              check_id: Optional[int] = None,
                              url: Optional[str] = None,
                              external_id: Optional[str] = None,
                              status: Optional[str] = None,
                              started_at: Optional[datetime] = None,
                              conclusion: Optional[str] = None,
                              completed_at: Optional[datetime] = None,
                              output: Optional[Dict[str, str]] = None,
                              actions: Optional[Any] = None,
                              ) -> Tuple[str, Dict[str, Any]]:
        if check_id is not None and commit_sha is not None:
            raise ValueError("Only one of check_id and commit_sha may be supplied")

        if status is not None:
            if status not in ("queued", "in_progress", "completed"):
                raise ValueError("Invalid status %s" % status)

        if started_at is not None:
            started_at_text: Optional[str] = started_at.isoformat()
        else:
            started_at_text = None

        if status == "completed" and conclusion is None:
            raise ValueError("Got a completed status but no conclusion")

        if conclusion is not None and completed_at is None:
            raise ValueError("Got a conclusion but no completion time")

        if conclusion is not None:
            if conclusion not in ("success", "failure", "neutral", "cancelled",
                                  "timed_out", "action_required"):
                raise ValueError("Invalid conclusion %s" % conclusion)

        if completed_at is not None:
            completed_at_text = completed_at.isoformat()

        if output is not None:
            if "title" not in output:
                raise ValueError("Output requires a title")
            if "summary" not in output:
                raise ValueError("Output requires a summary")

        req_data: Dict[str, Any] = {
            "name": name,
        }

        for (name, value) in [("head_sha", commit_sha),
                              ("id", check_id),
                              ("url", url),
                              ("external_id", external_id),
                              ("status", status),
                              ("started_at", started_at_text),
                              ("conclusion", conclusion),
                              ("completed_at", completed_at_text),
                              ("output", output),
                              ("actions", actions)]:
            req_data[name] = value

        req_method = "POST" if check_id is None else "PATCH"

        return req_method, req_data

    def set_check(self,
                  name: str,
                  commit_sha: Optional[str] = None,
                  check_id: Optional[int] = None,
                  url: Optional[str] = None,
                  external_id: Optional[str] = None,
                  status: Optional[str] = None,
                  started_at: Optional[datetime] = None,
                  conclusion: Optional[str] = None,
                  completed_at: Optional[datetime] = None,
                  output: Optional[Dict[str, str]] = None,
                  actions: Optional[List[str]] = None
                  ) -> Dict[str, Any]:
        req_method, req_data = self._construct_check_data(name, commit_sha, check_id,
                                                          url, external_id, status,
                                                          started_at, conclusion, completed_at,
                                                          output, actions)

        req_headers = {"Accept": "application/vnd.github.antiope-preview+json"}

        req_url = self.repo.url + "/check-runs"
        if check_id is not None:
            req_url += ("/%s" % check_id)

        headers, data = self.repo._requester.requestJsonAndCheck(  # type: ignore
            req_method,
            req_url,
            input=req_data,
            headers=req_headers
        )

        # Not sure what to return here
        return data


class AttrDict(dict):
    def __getattr__(self, name: str) -> Any:
        if name in self:
            return self[name]
        else:
            raise AttributeError(name)


class MockGitHub(GitHub):
    def __init__(self):
        self.prs = {}
        self.commit_prs = {}
        self._id = itertools.count(1)
        self.output = StringIO()
        self.checks = {}

    def _log(self, data: str) -> None:
        data = six.ensure_text(data)
        self.output.write(data)
        self.output.write("\n")

    @property
    def repo(self):
        raise NotImplementedError

    def get_pull(self, id: int) -> Any:
        self._log("Getting PR %s" % id)
        return self.prs.get(int(id))

    def create_pull(self,
                    title: str,
                    body: str,
                    base: str,
                    head: str,
                    _commits: Optional[List[AttrDict]] = None,
                    _id: Optional[int] = None,
                    _user: Optional[str] = None,
                    ) -> int:
        if _id is None:
            id = next(self._id)
        else:
            id = int(_id)
        assert id not in self.prs
        if _user is None:
            _user = env.config["web-platform-tests"]["github"]["user"]
        if _commits is None:
            _commits = [AttrDict(**{"sha": "%040x" % random.getrandbits(160),
                                    "message": "Test commit",
                                    "_statuses": [],
                                    "_checks": []})]
        data = AttrDict(**{
            "number": id,
            "title": title,
            "body": body,
            "base": {"ref": base},
            "head": head,
            "merged": False,
            "merge_commit_sha": "%040x" % random.getrandbits(160),
            "state": "open",
            "mergeable": True,
            "_approved": True,
            "_commits": _commits,
            "user": {
                "login": _user
            },
            "labels": []
        })
        self.prs[id] = data
        for commit in _commits:
            self.commit_prs[commit["sha"]] = id
        self._log("Created PR with id %s" % id)
        return id

    def has_branch(self, name: str) -> bool:
        self._log("Checked branch %s" % name)
        return True

    def add_labels(self, pr_id: int, *labels: str) -> None:
        self.get_pull(pr_id)["labels"].extend(labels)

    def remove_labels(self, pr_id: int, *labels: str) -> None:
        pr = self.get_pull(pr_id)
        pr["labels"] = [item for item in pr["labels"] if item not in labels]

    def load_pull(self, data: Dict[str, Any]) -> None:
        pr = self.get_pull(data["number"])
        pr.merged = data["merged"]
        pr.state = data["state"]

    def required_checks(self, branch_name: str) -> List[str]:
        return ["wpt-decision-task", "sink-task"]

    def get_check_runs(self, pr_id: int) -> Dict[str, Dict[str, Any]]:
        pr = self.get_pull(pr_id)
        rv = {}
        if pr:
            self._log("Got status for PR %s " % pr_id)
            for item in pr["_commits"][-1]["_checks"]:
                rv[item["name"]] = item.copy()
                del rv[item["name"]]["name"]
        return rv

    def pull_state(self, pr_id: int) -> str:
        pr = self.get_pull(pr_id)
        if not pr:
            raise ValueError
        return pr["state"]

    def reopen_pull(self, pr_id: int) -> None:
        pr = self.get_pull(pr_id)
        if not pr:
            raise ValueError
        pr["state"] = "open"

    def close_pull(self, pr_id: int) -> None:
        pr = self.get_pull(pr_id)
        if not pr:
            raise ValueError
        pr["state"] = "closed"

    def is_approved(self, pr_id: int) -> bool:
        pr = self.get_pull(pr_id)
        return pr._approved

    def merge_sha(self, pr_id: int) -> Optional[str]:
        pr = self.get_pull(pr_id)
        if pr.merged:
            return pr.merge_commit_sha
        return None

    def merge_pull(self, pr_id: int) -> str:
        pr = self.get_pull(pr_id)
        if self.is_mergeable:
            pr.merged = True
        else:
            # TODO: raise the right kind of error here
            raise ValueError
        pr["merged_by"] = {"login": env.config["web-platform-tests"]["github"]["user"]}
        self._log("Merged PR with id %s" % pr_id)
        return pr.merge_commit_sha

    def pr_for_commit(self, sha: str) -> Optional[int]:
        return self.commit_prs.get(sha)

    def get_pulls(self, minimum_id=None):
        for number in self.prs:
            if minimum_id and number >= minimum_id:
                yield self.get_pull(number)

    def get_status(self,
                   pr_id: int,
                   context: str
                   ):
        # type (...) -> Optional[Text]
        pr = self.get_pull(pr_id)
        statuses = [item for item in pr._commits[-1]._statuses
                    if item.context == context]
        statuses.sort(key=lambda x: -x.id)
        if statuses:
            return statuses[0].state
        return None

    def set_status(self,
                   pr_id: int,
                   status: str,
                   target_url: Optional[str],
                   description: Optional[str],
                   context: str
                   ):
        pr = self.get_pull(pr_id)
        head_commit = pr._commits[-1]
        kwargs = {
            "state": status,
            "context": context,
            "id": len(pr._commits[-1]._statuses),
        }
        if target_url is not None:
            kwargs["target_url"] = target_url
        if description is not None:
            kwargs["description"] = description
        head_commit._statuses.append(AttrDict(**kwargs))

    def set_check(self,
                  name: str,
                  commit_sha: Optional[str] = None,
                  check_id: Optional[int] = None,
                  url: Optional[str] = None,
                  external_id: Optional[str] = None,
                  status: Optional[str] = None,
                  started_at: Optional[datetime] = None,
                  conclusion: Optional[str] = None,
                  completed_at: Optional[datetime] = None,
                  output: Optional[Dict[str, str]] = None,
                  actions: Optional[List[str]] = None,
                  ) -> Dict[str, Any]:

        req_method, req_data = self._construct_check_data(name, commit_sha, check_id,
                                                          url, external_id, status,
                                                          started_at, conclusion, completed_at,
                                                          output, actions)

        if req_data["head_sha"] not in self.checks:
            assert req_method == "POST"
            check_id = len(self.checks)
        else:
            assert req_method == "PATCH"
            check_id = self.checks["head_sha"][0]

        self.checks[req_data["head_sha"]] = (check_id, req_method, req_data)
        return {"id": check_id}
