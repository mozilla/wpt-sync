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
        head_commit.create_status(status,
                                  target_url=target_url,
                                  description=description,
                                  context=context)

    def close_pull(self, pr_id):
        pr = self.get_pull(pr_id)
        if not pr:
            raise ValueError
        # Perhaps?
        # issue = self.repo.get_issue(pr_id)
        # issue.add_to_labels("mozilla:backed-out")
        pr.edit(state="closed")
