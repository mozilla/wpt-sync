import itertools
import random
import re
import sys
import time
import urlparse

import github
import log
from env import Environment

logger = log.get_logger(__name__)
env = Environment()


class GitHub(object):
    def __init__(self, token, url):
        self.gh = github.Github(token)
        self.repo_name = urlparse.urlsplit(url).path.lstrip("/")
        self.pr_cache = {}
        self._repo = None

    def pr_url(self, pr_id):
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
            entries = list(pulls[:2])
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
        issue = self.repo.get_issue(pr_id)
        issue.add_to_labels(*labels)

    @staticmethod
    def _summary_state(statuses):
        states = {item.state for item in statuses}
        overall = None
        if states == {"success"}:
            overall = "success"
        elif "pending" in states or states == set():
            overall = "pending"
        if "failure" in states or "error" in states:
            overall = "failure"
        return overall

    def get_combined_status(self, pr_id, exclude=None):
        if exclude is None:
            exclude = set()
        pr = self.get_pull(pr_id)
        combined = self.repo.get_commit(pr.head.sha).get_combined_status()
        statuses = [item for item in combined.statuses if item.context not in exclude]
        state = combined.state if not exclude else GitHub._summary_state(statuses)
        return state, statuses

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
        return "APPROVED" in review_by_reviewer.values()

    def merge_sha(self, pr_id):
        pr = self.get_pull(pr_id)
        if pr.merged:
            return pr.merge_commit_sha
        return None

    def is_mergeable(self, pr_id):
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
                del self.pr_cache[self.pr]
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
        r = re.compile(re.escape("<!-- Reviewable:start -->") + ".*" +
                       re.escape("<!-- Reviewable:end -->"), re.DOTALL)
        return r.sub("", text)


class AttrDict(dict):
    def __getattr__(self, name):
        if name in self:
            return self[name]
        else:
            raise AttributeError(name)


class MockGitHub(GitHub):
    def __init__(self):
        self.prs = {}
        self.commit_prs = {}
        self._id = itertools.count(1)
        self.output = sys.stdout

    def _log(self, data):
        self.output.write(data)
        self.output.write("\n")

    def get_pull(self, id):
        self._log("Getting PR %s" % id)
        return self.prs.get(int(id))

    def create_pull(self, title, body, base, head, _commits=None, _id=None,
                    _user=None):
        if _id is None:
            id = self._id.next()
        else:
            id = int(_id)
        assert id not in self.prs
        if _user is None:
            _user = env.config["web-platform-tests"]["github"]["user"]
        if _commits is None:
            _commits = [AttrDict(**{"sha": "%040x" % random.getrandbits(160),
                                    "message": "Test commit",
                                    "_statuses": []})]
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
        # For now we are only using this to check a branch exists
        self._log("Checked branch %s" % name)
        return True

    def set_status(self, pr_id, status, target_url, description, context):
        pr = self.get_pull(pr_id)
        pr._commits[-1]._statuses.insert(0, AttrDict(state=status,
                                                     target_url=target_url,
                                                     description=description,
                                                     context=context))
        self._log("Set status on PR %s to %s" % (pr_id, status))

    def add_labels(self, pr_id, *labels):
        self.get_pull(pr_id)["labels"].extend(labels)

    def get_combined_status(self, pr_id, exclude=None):
        if exclude is None:
            exclude = set()
        pr = self.get_pull(pr_id)
        if pr:
            self._log("Got status for PR %s " % pr_id)
            statuses = pr["_commits"][-1]["_statuses"]
            latest = {}
            for item in statuses:
                if item.context not in latest:
                    latest[item.context] = item
            statuses = [item for item in latest.values() if item.context not in exclude]
            state = GitHub._summary_state(statuses)
            return state, statuses

    def pull_state(self, pr_id):
        pr = self.get_pull(pr_id)
        if not pr:
            raise ValueError
        return pr["state"]

    def reopen_pull(self, pr_id):
        pr = self.get_pull(pr_id)
        if not pr:
            raise ValueError
        pr["state"] = "open"

    def close_pull(self, pr_id):
        pr = self.get_pull(pr_id)
        if not pr:
            raise ValueError
        pr["state"] = "closed"

    def merge_sha(self, pr_id):
        pr = self.get_pull(pr_id)
        if pr.merged:
            return pr.merge_commit_sha
        return None

    def merge_pull(self, pr_id):
        pr = self.get_pull(pr_id)
        if self.is_mergeable:
            pr.merged = True
        else:
            # TODO: raise the right kind of error here
            raise ValueError
        self._log("Merged PR with id %s" % pr_id)

    def is_approved(self, pr_id):
        pr = self.get_pull(pr_id)
        return pr._approved

    def pr_for_commit(self, sha):
        return self.commit_prs.get(sha)

    def get_pulls(self, minimum_id=None):
        for number in self.prs:
            if minimum_id and number >= minimum_id:
                yield self.get_pull(number)
