from __future__ import absolute_import
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
MYPY = False
if MYPY:
    from typing import List
    from datetime import datetime
    from typing import Any
    from typing import Dict
    from typing import Optional
    from typing import Text
    from typing import Tuple
    from typing import Union

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


class GitHub(object):
    def __init__(self, token, url):
        self.gh = github.Github(token)
        self.repo_name = urllib.parse.urlsplit(url).path.lstrip("/")
        self.pr_cache = {}
        self._repo = None

    def pr_url(self, pr_id):
        # type: (int) -> str
        return ("%s/pull/%s" %
                (env.config["web-platform-tests"]["repo"]["url"],
                 pr_id))

    def load_pull(self, data):
        pr = self.gh.create_from_raw_data(github.PullRequest.PullRequest, data)
        self.pr_cache[pr.number] = pr

    @property
    def repo(self):
        if self._repo is None:
            self._repo = self.gh.get_repo(self.repo_name)
        return self._repo

    def get_pull(self, id):
        id = int(id)
        if id not in self.pr_cache:
            self.pr_cache[id] = self.repo.get_pull(id)
        return self.pr_cache[id]

    def create_pull(self, title, body, base, head):
        try:
            pr = self.repo.create_pull(title=title, body=body, base=base, head=head)
            logger.info("Created PR %s" % pr.number)
        except github.GithubException:
            # Check if there's already a PR for this head
            user = self.repo_name.split("/")[0]
            pulls = self.repo.get_pulls(head="%s:%s" % (user, head))
            entries = list(pulls)
            if len(entries) == 0:
                raise
            elif len(entries) > 1:
                raise ValueError("Found multiple existing pulls for branch")
            pr = pulls[0]
        self.add_labels(pr.number, "mozilla:gecko-sync")
        self.pr_cache[pr.number] = pr
        return pr.number

    def get_branch(self, name):
        try:
            return self.repo.get_branch(name)
        except (github.GithubException, github.UnknownObjectException):
            return None

    def set_status(self, pr_id, status, target_url, description, context):
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

    def add_labels(self, pr_id, *labels):
        logger.debug("Adding labels %s to PR %s" % (", ".join(labels), pr_id))
        pr_id = self._convert_pr_id(pr_id)
        issue = self.repo.get_issue(pr_id)
        issue.add_to_labels(*labels)

    def remove_labels(self, pr_id, *labels):
        logger.debug("Removing labels %s from PR %s" % (labels, pr_id))
        pr_id = self._convert_pr_id(pr_id)
        issue = self.repo.get_issue(pr_id)
        for label in labels:
            try:
                issue.remove_from_labels(label)
            except github.GithubException as e:
                if e.data.get("message", "") != "Label does not exist":
                    logger.warning("Error handling label removal: %s" % e)
                    newrelic.agent.record_exception()

    def _convert_pr_id(self, pr_id):
        if not isinstance(pr_id, six.integer_types):
            try:
                pr_id = int(pr_id)
            except ValueError:
                raise ValueError('PR ID is not a valid number')
        return pr_id

    def required_checks(self, branch_name):
        branch = self.get_branch(branch_name)
        return branch._rawData["protection"]["required_status_checks"]["contexts"]

    def get_check_runs(self, pr_id):
        pr = self.get_pull(pr_id)
        check_runs = list(self._get_check_runs(pr.head.sha))
        required_contexts = self.required_checks(pr.base.ref)
        rv = {}
        id_by_name = {}
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

    def pull_state(self, pr_id):
        pr = self.get_pull(pr_id)
        if not pr:
            raise ValueError
        return pr.state

    def reopen_pull(self, pr_id):
        pr = self.get_pull(pr_id)
        if not pr:
            raise ValueError
        pr.edit(state="open")

    def close_pull(self, pr_id):
        pr = self.get_pull(pr_id)
        if not pr:
            raise ValueError
        # Perhaps?
        # issue = self.repo.get_issue(pr_id)
        # issue.add_to_labels("mozilla:backed-out")
        pr.edit(state="closed")

    def is_approved(self, pr_id):
        pr = self.get_pull(pr_id)
        reviews = pr.get_reviews()
        # We get a chronological list of all reviews, so we want to
        # check if the last review by any reviewer was in the approved
        # state
        review_by_reviewer = {}
        for review in reviews:
            review_by_reviewer[review.user.login] = review.state
        return "APPROVED" in list(review_by_reviewer.values())

    def merge_sha(self, pr_id):
        pr = self.get_pull(pr_id)
        if pr.merged:
            return pr.merge_commit_sha
        return None

    def is_mergeable(self, pr_id):
        # type: (int) -> bool
        mergeable = None
        count = 0
        while mergeable is None and count < 6:
            # GitHub sometimes doesn't have the mergability information ready;
            # In this case mergeable is None and we need to wait and try again
            pr = self.get_pull(pr_id)
            mergeable = pr.mergeable
            if mergeable is None:
                time.sleep(2**count)
                count += 1
                del self.pr_cache[pr_id]
        return bool(mergeable)

    def merge_pull(self, pr_id):
        pr = self.get_pull(pr_id)
        post_parameters = {"merge_method": "rebase"}
        headers, data = pr._requester.requestJsonAndCheck(
            "PUT",
            pr.url + "/merge",
            input=post_parameters
        )
        return data["sha"]

    def pr_for_commit(self, sha):
        logger.info("Looking up PR for commit %s" % sha)
        owner, repo = self.repo_name.split("/")
        prs = list(self.gh.search_issues(query="is:pr repo:%s/%s sha:%s" % (owner, repo, sha)))
        if len(prs) == 0:
            return

        if len(prs) > 1:
            logger.warning("Got multiple PRs related to commit %s: %s" %
                           (sha, ", ".join(str(item.number) for item in prs)))
            prs = sorted(prs, key=lambda x: x.number)

        return prs[0].number

    def get_pulls(self, minimum_id=None):
        for item in self.repo.get_pulls():
            if minimum_id and item.number < minimum_id:
                break
            yield item

    def get_commits(self, pr_id):
        return list(self.get_pull(pr_id).commits)

    def cleanup_pr_body(self, text):
        # type: (str) -> str
        r = re.compile(re.escape("<!-- Reviewable:start -->") + ".*" +
                       re.escape("<!-- Reviewable:end -->"), re.DOTALL)
        return r.sub("", text)

    def _construct_check_data(self,
                              name,  # type: str
                              commit_sha=None,  # type: str
                              check_id=None,  # type: Optional[int]
                              url=None,  # type: Text
                              external_id=None,  # type: Text
                              status=None,  # type: str
                              started_at=None,  # type: Optional[Any]
                              conclusion=None,  # type: str
                              completed_at=None,  # type: datetime
                              output=None,  # type: Union[Dict[str, Text], Dict[str, str]]
                              actions=None,  # type: Optional[Any]
                              ):
        # type: (...) -> Tuple[str, Dict[str, Optional[str]]]
        if check_id is not None and commit_sha is not None:
            raise ValueError("Only one of check_id and commit_sha may be supplied")

        if status is not None:
            if status not in ("queued", "in_progress", "completed"):
                raise ValueError("Invalid status %s" % status)

        if started_at is not None:
            started_at = started_at.isoformat()

        if status == "completed" and conclusion is None:
            raise ValueError("Got a completed status but no conclusion")

        if conclusion is not None and completed_at is None:
            raise ValueError("Got a conclusion but no completion time")

        if conclusion is not None:
            if conclusion not in ("success", "failure", "neutral", "cancelled",
                                  "timed_out", "action_required"):
                raise ValueError("Invalid conclusion %s" % conclusion)

        if completed_at is not None:
            completed_at = completed_at.isoformat()

        if output is not None:
            if not "title"in output:
                raise ValueError("Output requires a title")
            if not "summary"in output:
                raise ValueError("Output requires a summary")

        req_data = {
            "name": name,
        }

        for (name, value) in [("head_sha", commit_sha),
                              ("id", check_id),
                              ("url", url),
                              ("external_id", external_id),
                              ("status", status),
                              ("started_at", started_at),
                              ("conclusion", conclusion),
                              ("completed_at", completed_at),
                              ("output", output),
                              ("actions", actions)]:
            req_data[name] = value

        req_method = "POST" if check_id is None else "PATCH"

        return req_method, req_data

    def set_check(self, name, commit_sha=None, check_id=None, url=None, external_id=None,
                  status=None, started_at=None, conclusion=None, completed_at=None,
                  output=None, actions=None):

        req_method, req_data = self._construct_check_data(name, commit_sha, check_id,
                                                          url, external_id, status,
                                                          started_at, conclusion, completed_at,
                                                          output, actions)

        req_headers = {"Accept": "application/vnd.github.antiope-preview+json"}

        url = self.repo.url + "/check-runs"
        if check_id is not None:
            url += ("/%s" % check_id)

        headers, data = self.repo._requester.requestJsonAndCheck(
            req_method,
            url,
            input=req_data,
            headers=req_headers
        )

        # Not sure what to return here
        return data


class AttrDict(dict):
    def __getattr__(self, name):
        # type: (str) -> Any
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

    def _log(self, data):
        # type: (str) -> None
        data = six.ensure_text(data)
        self.output.write(data)
        self.output.write(u"\n")

    def get_pull(self, id):
        # type: (Union[int, str]) -> AttrDict
        self._log("Getting PR %s" % id)
        return self.prs.get(int(id))

    def create_pull(self,
                    title,  # type: Text
                    body,  # type: Text
                    base,  # type: str
                    head,  # type: Text
                    _commits=None,  # type: Optional[List[AttrDict]]
                    _id=None,  # type: Optional[Any]
                    _user=None,  # type: Optional[str]
                    ):
        # type: (...) -> int
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

    def get_branch(self, name):
        # type: (str) -> bool
        # For now we are only using this to check a branch exists
        self._log("Checked branch %s" % name)
        return True

    def set_status(self, pr_id, status, target_url, description, context):
        # type: (int, str, str, str, str) -> None
        pr = self.get_pull(pr_id)
        pr._commits[-1]._statuses.insert(0, AttrDict(state=status,
                                                     target_url=target_url,
                                                     description=description,
                                                     context=context))
        self._log("Set status on PR %s to %s" % (pr_id, status))

    def add_labels(self, pr_id, *labels):
        # type: (str, *str) -> None
        self.get_pull(pr_id)["labels"].extend(labels)

    def remove_labels(self, pr_id, *labels):
        # type: (str, *str) -> None
        pr = self.get_pull(pr_id)
        pr["labels"] = [item for item in pr["labels"] if item not in labels]

    def load_pull(self, data):
        # type: (Dict[str, Any]) -> None
        pr = self.get_pull(data["number"])
        pr.merged = data["merged"]
        pr.state = data["state"]

    def required_checks(self, branch_name):
        return ["wpt-decision-task", "sink-task"]

    def get_check_runs(self, pr_id):
        pr = self.get_pull(pr_id)
        rv = {}
        if pr:
            self._log("Got status for PR %s " % pr_id)
            for item in pr["_commits"][-1]["_checks"]:
                rv[item["name"]] = item.copy()
                del rv[item["name"]]["name"]
        return rv

    def pull_state(self, pr_id):
        # type: (int) -> str
        pr = self.get_pull(pr_id)
        if not pr:
            raise ValueError
        return pr["state"]

    def reopen_pull(self, pr_id):
        # type: (int) -> None
        pr = self.get_pull(pr_id)
        if not pr:
            raise ValueError
        pr["state"] = "open"

    def close_pull(self, pr_id):
        # type: (int) -> None
        pr = self.get_pull(pr_id)
        if not pr:
            raise ValueError
        pr["state"] = "closed"

    def merge_sha(self, pr_id):
        # type: (int) -> Optional[Any]
        pr = self.get_pull(pr_id)
        if pr.merged:
            return pr.merge_commit_sha
        return None

    def merge_pull(self, pr_id):
        # type: (int) -> None
        pr = self.get_pull(pr_id)
        if self.is_mergeable:
            pr.merged = True
        else:
            # TODO: raise the right kind of error here
            raise ValueError
        pr["merged_by"] = {"login": env.config["web-platform-tests"]["github"]["user"]}
        self._log("Merged PR with id %s" % pr_id)

    def is_approved(self, pr_id):
        pr = self.get_pull(pr_id)
        return pr._approved

    def pr_for_commit(self, sha):
        # type: (Text) -> Optional[int]
        return self.commit_prs.get(sha)

    def get_pulls(self, minimum_id=None):
        for number in self.prs:
            if minimum_id and number >= minimum_id:
                yield self.get_pull(number)

    def set_check(self,
                  name,  # type: str
                  check_id=None,  # type: Optional[int]
                  commit_sha=None,  # type: str
                  url=None,  # type: Text
                  external_id=None,  # type: Text
                  status=None,  # type: str
                  started_at=None,  # type: Optional[Any]
                  conclusion=None,  # type: str
                  completed_at=None,  # type: datetime
                  output=None,  # type: Union[Dict[str, Text], Dict[str, str]]
                  actions=None,  # type: Optional[Any]
                  ):
        # type: (...) -> Dict[str, int]

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
