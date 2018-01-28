import itertools
import random
import re
import urlparse
import sys

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
        if id not in self.pr_cache:
            self.pr_cache[id] = self.repo.get_pull(int(id))
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

    def get_statuses(self, pr_id):
        pr = self.get_pull(pr_id)
        head_commit = self.repo.get_commit(pr.head.ref)

        return head_commit.get_statuses()

    def pull_state(self, pr_id):
        pr = self.get_pull(pr_id)
        if not pr:
            raise ValueError
        return pr.state

    def close_pull(self, pr_id):
        pr = self.get_pull(pr_id)
        if not pr:
            raise ValueError
        # Perhaps?
        # issue = self.repo.get_issue(pr_id)
        # issue.add_to_labels("mozilla:backed-out")
        pr.edit(state="closed")

    def is_mergeable(self, pr_id):
        pr = self.get_pull(pr_id)
        return pr.mergeable

    def merge_pull(self, pr_id):
        pr = self.get_pull(pr_id)
        post_parameters = {"merge_method": "rebase"}
        headers, data = pr._requester.requestJsonAndCheck(
            "PUT",
            pr.url + "/merge",
            input=post_parameters
        )
        return data["sha"]

    def approve_pull(self, pr_id):
        pr = self.get_pull(pr_id)
        post_parameters = {"body": "Reviewed upstream",
                           "event": "APPROVE"}
        headers, data = pr._requester.requestJsonAndCheck(
            "PUT",
            pr.url + "/reviews",
            input=post_parameters
        )

    def status_checks_pass(self, pr_id):
        pr = self.get_pull(pr_id)
        if not pr.mergeable:
            return False
        statuses = self.get_statuses(pr_id)
        latest = {}
        for status in statuses:
            if status.context not in latest:
                latest[status.context] = status.state

        return all(status == "success" for status in latest.itervalues())

    def pr_for_commit(self, sha):
        logger.info("Looking up PR for commit %s" % sha)
        owner, repo = self.repo_name.split("/")
        prs = list(self.gh.search_issues(query="is:pr repo:%s/%s %s" % (owner, repo, sha)))
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
        return self.prs.get(id)

    def create_pull(self, title, body, base, head, _commits=None, _id=None):
        if _id is None:
            id = self._id.next()
        else:
            id = _id
        assert id not in self.prs
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
            "state": "open",
            "mergeable": True,
            "approved": True,
            "_commits": _commits,
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

    def get_statuses(self, pr_id):
        pr = self.get_pull(pr_id)
        if pr:
            self._log("Got status for PR %s " % pr_id)
            return pr["_commits"][-1]["_statuses"]

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

    def merge_pull(self, pr_id):
        pr = self.get_pull(pr_id)
        if self.is_mergeable:
            pr.merged = True
        else:
            # TODO: raise the right kind of error here
            raise ValueError
        self._log("Merged PR with id %s" % pr_id)

    def approve_pull(self, pr_id):
        pr = self.get_pull(pr_id)
        pr.approved = True

    def pr_for_commit(self, sha):
        return self.commit_prs.get(sha)

    def get_pulls(self, minimum_id=None):
        for number in self.prs:
            if minimum_id and number >= minimum_id:
                yield self.get_pull(number)
