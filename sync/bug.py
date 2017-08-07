import bugsy
import sys


class Bugzilla(object):
    def __init__(self, config):
        self.bug_cache = {}
        self.bugzilla = bugsy.Bugsy(bugzilla_url=config["bugzilla"]["url"],
                                    api_key=config["bugzilla"]["apikey"])

    def _get_bug(self, bug_id):
        if bug_id not in self.bug_cache:
            bugs = bugsy.Search(self.bugzilla).bug_numbers(bug_id).search()
            if not bugs:
                return
            self.bug_cache[bug_id] = bugs[0]
        return self.bug_cache[bug_id]

    def comment(self, bug_id, text):
        bug = self.bugzilla.get(bug_id)
        bug.add_comment(text)

    def new(self, summary, comment, product, component):
        bug = bugsy.Bug(self.bugzilla,
                        summary=summary,
                        product=product,
                        component=component)
        bug.add_comment(comment)

        self.bugzilla.put(bug)
        self.bug_cache[bug.id] = bug
        return bug.id

    def set_component(self, bug_id, product=None, component=None):
        bug = self._get_bug(bug_id)
        if product is not None:
            bug.product = product
        if component is not None:
            bug.component = component

        if product is not None or component is not None:
            self.bugzilla.put(bug)


class MockBugzilla(object):
    def __init__(self, config):
        self.output = sys.stdout

    def _log(self, data):
        self.output.write(data)
        self.output.write("\n")

    def new(self, summary, comment, product, component):
        self._log("Creating a bug in component %s :: %s\nSummary: %s\nComment: %s" % (
            product, component, summary, comment))

    def comment(self, bug_id, text):
        self._log("Posting to bug %s:\n%s" % (bug_id, text))

    def set_component(self, bug_id, product=None, component=None):
        self._log("Setting bug %s product: %s component: %s" % (bug_id, product, component))
