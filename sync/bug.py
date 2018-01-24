import bugsy
import sys
import urlparse
import log
import re

from env import Environment

env = Environment()

logger = log.get_logger(__name__)


def bz_url_from_api_url(api_url):
    parts = urlparse.urlparse(api_url)
    bz_url = (parts.scheme, parts.netloc, "", "", "", "")
    return urlparse.urlunparse(bz_url)


def bug_number_from_url(url):
    return urlparse.parse_qs(urlparse.urlsplit(url).query).get("id")


status_re = re.compile("\[wptsync ([^\[ ]+)(?: ([^\[ ]+))?\]")


def get_sync_data(whiteboard):
    matches = status_re.findall(whiteboard)
    if matches:
        subtype, status = matches[0]
        if not status:
            status = None
        return subtype, status
    return None, None


def set_sync_data(whiteboard, subtype, status):
    if subtype is None:
        raise ValueError

    if status:
        text = "[wptsync %s %s]" % (subtype, status)
    else:
        text = "[wptsync %s]" % subtype
    new = status_re.sub(text, whiteboard)
    if new == whiteboard:
        new = whiteboard + text
    return new


class Bugzilla(object):
    bug_cache = {}

    def __init__(self, config):
        self.api_url = config["bugzilla"]["url"]
        self.bz_url = bz_url_from_api_url(self.api_url)
        self.bugzilla = bugsy.Bugsy(bugzilla_url=self.api_url,
                                    api_key=config["bugzilla"]["apikey"])

    def bugzilla_url(self, bug_id):
        return "%s/show_bug.cgi?id=%s" % (self.bz_url, bug_id)

    def id_from_url(self, url):
        if not url.startswith(self.bz_url):
            return None
        parts = urlparse.urlsplit(url)
        query = urlparse.parse_qs(parts.query)
        if "id" not in query or len(query["id"]) != 1:
            return None
        return query["id"][0]

    def _get_bug(self, bug_id):
        if bug_id not in self.bug_cache:
            try:
                bug = self.bugzilla.get(bug_id)
            except bugsy.BugsyException:
                return
            self.bug_cache[bug_id] = bug
        return self.bug_cache[bug_id]

    def comment(self, bug_id, text):
        bug = self._get_bug(bug_id)
        if bug is None:
            logger.error("Failed to find bug %s to add comment:\n%s" % (bug_id, text))
            return
        bug.add_comment(text)

    def new(self, summary, comment, product, component, whiteboard=None):
        bug = bugsy.Bug(self.bugzilla,
                        summary=summary,
                        product=product,
                        component=component)
        if whiteboard:
            self.set_whiteboard(bug.id, whiteboard)
        bug.add_comment(comment)

        self.bugzilla.put(bug)
        self.bug_cache[bug.id] = bug
        return bug.id

    def set_component(self, bug_id, product=None, component=None):
        bug = self._get_bug(bug_id)
        if bug is None:
            logger.error("Failed to find bug %s to set component: %s::%s" %
                         (bug_id, product, component))
            return

        if product is not None:
            bug.product = product
        if component is not None:
            bug.component = component

        if product is not None or component is not None:
            try:
                self.bugzilla.put(bug)
            except bugsy.BugsyException:
                logger.error("Failed to set component %s :: %s" % (bug.product, bug.component))

    def set_whiteboard(self, bug_id, whiteboard):
        bug = self._get_bug(bug_id)
        bug._bug["whiteboard"] = whiteboard
        self.bugzilla.put(bug)

    def get_whiteboard(self, bug_id):
        bug = self._get_bug(bug_id)
        return bug._bug.get("whiteboard", "")


class MockBugzilla(Bugzilla):
    def __init__(self, config):
        self.api_url = config["bugzilla"]["url"]
        self.bz_url = bz_url_from_api_url(self.api_url)
        self.output = sys.stdout
        self.known_bugs = []

    def _log(self, data):
        self.output.write(data)
        self.output.write("\n")

    def new(self, summary, comment, product, component, whiteboard=None):
        self._log("Creating a bug in component %s :: %s\nSummary: %s\nComment: %s\nWhiteboard: %s" % (
            product, component, summary, comment, whiteboard))
        if self.known_bugs:
            bug_id = self.known_bugs[-1] + 1
        else:
            bug_id = 100000
        self.known_bugs.append(bug_id)
        return str(bug_id)

    def comment(self, bug_id, text):
        self._log("Posting to bug %s:\n%s" % (bug_id, text))

    def set_component(self, bug_id, product=None, component=None):
        self._log("Setting bug %s product: %s component: %s" % (bug_id, product, component))

    def set_whiteboard(self, bug_id, whiteboard):
        self._log("Setting bug %s whiteboard: %s" % (bug_id, whiteboard))

    def get_whiteboard(self, bug_id):
        return "fake data"
