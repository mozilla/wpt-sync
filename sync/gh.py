import itertools
import random
import urlparse

import github


class GitHub(object):
    def __init__(self, token, url):
        self.gh = github.Github(token)
        self.repo_name = urlparse.urlsplit(url).path.lstrip("/")
        self.pr_cache = {}
        self._repo = None

    @property
    def repo(self):
        if self._repo is None:
            self._repo = self.gh.get_repo(self.repo_name)
        return self._repo

    def get_pull(self, id):
        if id not in self.pr_cache:
            self.pr_cache[id] = self.repo.get_pull(id)
        return self.pr_cache[id]

    def create_pull(self, title, body, base, head):
        pr = self.repo.create_pull(title=title, body=body, base=base, head=head)
        self.pr_cache[pr.id] = pr
        return pr

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

    def get_statuses(self, pr_id):
        pr = self.get_pull(pr_id)
        head_commit = self.repo.get_commit(pr.head.ref)

        return head_commit.get_statuses()

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
            if status.context not in latest and status.context != "upstream/gecko":
                latest[status.context] = status.status

        return all(status.status == "success" for status in latest.itervalues())


class AttrDict(dict):
    def __getattr__(self, name):
        if name in self:
            return self[name]
        else:
            raise AttributeError(name)


class MockGitHub(object):
    def __init__(self, token, url):
        self.prs = {}
        self._id = itertools.count(1)

    def _log(self, data):
        self.output.write(data)
        self.output.write("\n")

    def get_pull(self, id):
        self._log("Getting PR %s" % id)
        return self.prs.get(id)

    def create_pull(self, title, body, base, head):
        id = self._id.next()
        data = AttrDict(**{
            "number": id,
            "title": title,
            "body": body,
            "base": base,
            "head": head,
            "merged": False,
            "state": "open",
            "mergable": True,
            "approved": True,
            "_commits": [AttrDict(**{"sha": "%040x" % random.randbits(160),
                                     "message": "Test commit",
                                     "_statuses": []})],
        })
        self.prs[id] = data
        self._log("Created PR with id %s" % id)
        return id

    def get_branch(self, name):
        # For now we are only using this to check a branch exists
        self._log("Checked branch %s" % name)
        return True

    def set_status(self, pr_id, status, target_url, description, context):
        pr = self.get_pull(pr_id)
        for item in pr._commits[0]._statuses:
            if item.context == context and item.description == description:
                item.status = status
                item.target_url = target_url
                self._log("Set status on PR %s to %s" % (pr_id, status))
            return
        pr._commits[0]._statuses.append(AttrDict(status=status,
                                                 target_url=target_url,
                                                 description=description,
                                                 context=context))
        self._log("Set status on PR %s to %s" % (pr_id, status))

    def get_statuses(self, pr_id):
        pr = self.get_pull(pr_id)
        if pr:
            self._log("Got status for PR %s " % pr_id)
            return pr["_commits"][0]["_statuses"]

    def close_pull(self, pr_id):
        pr = self.get_pull(pr_id)
        if not pr:
            raise ValueError
        pr["state"] = "closed"

    def is_mergeable(self, pr_id):
        pr = self.get_pull(pr_id)
        return (pr.mergeable and
                all(item.status == "success" for item in pr._commits[0]._statuses) and
                pr.approved)

    def merge_pull(self, pr_id):
        pr = self.get_pull(pr_id)
        if self.is_mergeable:
            pr.merged = True
        else:
            # TODO: raise the right kind of error here
            raise ValueError

    def approve_pull(self, pr_id):
        pr = self.get_pull(pr_id)
        pr.approved = True
