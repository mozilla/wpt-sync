import bugsy
import sys


class Bugzilla(object):
    def __init__(self, config):
        self.bugzilla = bugsy.Bugsy(bugzilla_url=config["bugzilla"]["url"],
                                    api_key=config["bugzilla"]["apikey"])

    def comment(self, bug_id, text):
        bug = self.bugzilla.get(bug_id)
        bug.add_comment(text)


class MockBugzilla(object):
    def __init__(self, config):
        self.output = sys.stdout

    def _log(self, data):
        self.output.write(data)
        self.output.write("\n")

    def comment(self, bug_id, text):
        self._log("Posting to bug %s:\n%s" % (bug_id, text))
