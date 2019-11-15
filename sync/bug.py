import base64
import log
import re
import sys
import traceback
import urlparse

import bugsy
import newrelic

from env import Environment

env = Environment()

logger = log.get_logger(__name__)


max_comment_length = 65535

# Hack because bugsy has an incomplete list of statuses
if "REOPENED" not in bugsy.bug.VALID_STATUS:
    bugsy.bug.VALID_STATUS.append("REOPENED")


def bz_url_from_api_url(api_url):
    if api_url is None:
        return None
    parts = urlparse.urlparse(api_url)
    bz_url = (parts.scheme, parts.netloc, "", "", "", "")
    return urlparse.urlunparse(bz_url)


def bug_number_from_url(url):
    if url is None:
        return None
    bugs = urlparse.parse_qs(urlparse.urlsplit(url).query).get("id")
    if bugs:
        return bugs[0]


status_re = re.compile(r"\[wptsync ([^\[ ]+)(?: ([^\[ ]+))?\]")


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
    separator = ', ' if len(whiteboard) else ''
    if new == whiteboard:
        new = whiteboard + separator + text
    return new


def check_valid_comment(text):
    if len(text) > max_comment_length:
        # The maximum comment length is in "characters", not bytes
        text = text[:max_comment_length - 3] + u"[\u2026]"
        logger.error("Truncating comment that exceeds maximum length")
    return text


class Bugzilla(object):
    bug_cache = {}

    def __init__(self, config):
        self.api_url = config["bugzilla"]["url"]
        self.bz_url = bz_url_from_api_url(self.api_url)
        self.bugzilla = bugsy.Bugsy(bugzilla_url=self.api_url,
                                    api_key=config["bugzilla"]["apikey"])
        if "flags" not in self.bugzilla.DEFAULT_SEARCH:
            self.bugzilla.DEFAULT_SEARCH += ["flags"]

    def bug_ctx(self, bug_id):
        return BugContext(self, bug_id)

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
                logger.error("Failed to retrieve bug with id %s" % bug_id)
                return
            except Exception as e:
                logger.error("Failed to retrieve bug with id %s: %s" % (bug_id, e))
                newrelic.agent.record_exception()
                return

            self.bug_cache[bug_id] = bug
        return self.bug_cache[bug_id]

    def comment(self, bug_id, comment, **kwargs):
        bug = self._get_bug(bug_id)
        if bug is None:
            logger.error("Failed to find bug %s to add comment:\n%s" % (bug_id, comment))
            return
        body = {
            "comment": check_valid_comment(comment)
        }
        body.update(kwargs)
        self.bugzilla.request('bug/{}/comment'.format(bug.id),
                              method='POST', json=body)

    def new(self, summary, comment, product, component, whiteboard=None, priority=None,
            url=None):
        bug = bugsy.Bug(self.bugzilla,
                        type="task",
                        summary=summary,
                        product=product,
                        component=component)
        # Self-assign bugs by default to get them off triage radars
        bz_username = env.config["bugzilla"]["username"]
        if bz_username:
            bug._bug["assigned_to"] = bz_username
        bug.add_comment(comment)
        if priority is not None:
            if priority not in ("P1", "P2", "P3", "P4", "P5"):
                raise ValueError("Invalid bug priority %s" % priority)
            bug._bug["priority"] = priority
        if whiteboard:
            bug._bug["whiteboard"] = whiteboard
        if url:
            bug._bug["url"] = url

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

    def set_whiteboard(self, bug, whiteboard):
        if not isinstance(bug, bugsy.Bug):
            bug = self._get_bug(bug)
        if not bug:
            return None
        bug._bug["whiteboard"] = whiteboard
        try:
            self.bugzilla.put(bug)
        except bugsy.errors.BugsyException:
            logger.warning(traceback.format_exc())
        except Exception as e:
            logger.warning("Problem setting Bug %s Whiteboard: %s" % (bug.id, e))
            newrelic.agent.record_exception()

    def get_whiteboard(self, bug):
        if not isinstance(bug, bugsy.Bug):
            bug = self._get_bug(bug)
        if not bug:
            return None
        return bug._bug.get("whiteboard", "")

    def get_status(self, bug):
        if not isinstance(bug, bugsy.Bug):
            bug = self._get_bug(bug)
        return (bug.status, bug.resolution)

    def set_status(self, bug, status, resolution=None):
        if not isinstance(bug, bugsy.Bug):
            bug = self._get_bug(bug)
        bug.status = status
        if resolution is not None:
            assert status == "RESOLVED"
            bug.resolution = resolution
        self.bugzilla.put(bug)


class BugContext(object):
    def __init__(self, bugzilla, bug_id):
        self.bugzilla = bugzilla
        self.bug_id = bug_id

        # These are set to usable values when we enter the context manager
        self.bug = None
        self._comments = None
        self.comment = None
        self.attachments = None
        self.depends = None
        self.blocks = None
        self.dirty = None

    def __enter__(self):
        self.bug = self.bugzilla._get_bug(self.bug_id)
        self._comments = None
        self.comment = None
        self.attchements = []
        self.depends = {}
        self.blocks = {}
        self.dirty = set()

        return self

    def __exit__(self, *args, **kwargs):
        if self.dirty:
            # Apparently we can't add comments atomically with other changes
            if "comment" in self.dirty:
                self.dirty.remove("comment")
                if self.comment is not None:
                    self.bugzilla.comment(self.bug_id, **self.comment)
            if "attachment" in self.dirty:
                for attachment in self.attachments:
                    self.bugzilla.bugzilla.request('bug/{}/attachment'.format(self.bug._bug['id']),
                                                   method='POST', json=attachment)

            if "depends" in self.dirty:
                self.bug._bug["depends_on"] = self.depends
            if "blocks" in self.dirty:
                self.bug._bug["blocks"] = self.blocks
            if self.dirty:
                self.bugzilla.bugzilla.put(self.bug)

        # Poison the object so it can't be used outside a context manager
        self.bug = None

    def __setitem__(self, name, value):
        if name == "comment":
            return self.add_comment(value)
        self.bug._bug[name] = value
        self.dirty.add(name)

    def add_comment(self,
                    comment,
                    check_dupe=True,
                    comment_tags=None,
                    is_private=False,
                    is_markdown=False):
        if self.comment is not None:
            raise ValueError("Can only set one comment per bug")
        comment = check_valid_comment(comment)
        if check_dupe:
            comments = self.get_comments()
            for item in comments:
                if item.text == comment:
                    return False
        self.comment = {"comment": comment,
                        "is_markdown": is_markdown,
                        "is_private": is_private}
        if comment_tags is not None:
            self.comment["comment_tags"] = comment_tags
        self.dirty.add("comment")
        return True

    def get_comments(self):
        if self._comments is None:
            self._comments = self.bug.get_comments()
        return self._comments

    def needinfo(self, *requestees):
        if not requestees:
            return
        flags = self.bug._bug.get("flags", [])
        existing = {item["requestee"] for item in flags
                    if item["name"] == "needinfo" and
                    item["status"] == "?"}
        for requestee in requestees:
            if requestee not in existing:
                flags.append({
                    'name': 'needinfo',
                    'requestee': requestee,
                    'status': '?',
                })
        self.bug._bug["flags"] = flags
        self.dirty.add("flags")

    def add_attachment(self, data, file_name, summary, content_type="text/plain",
                       comment=None, is_patch=False, is_private=False, is_markdown=None,
                       flags=None):
        body = {
            "data": base64.encodestring(data),
            "file_name": file_name,
            "summary": summary,
            "content_type": content_type
        }
        if comment:
            body["comment"] = comment
        if is_patch:
            body["is_patch"] = is_patch
        if is_private:
            body["is_private"] = is_private
        if is_markdown:
            body["is_markdown"] = is_markdown
        if flags:
            body["flags"] = flags

        self.attachements.append(body)
        self.dirty.add("attachment")

    def add_depends(self, bug_id):
        if "add" not in self.depends:
            self.depends["add"] = []
        self.depends["add"].append(bug_id)
        self.dirty.add("depends")

    def remove_depends(self, bug_id):
        if "remove" not in self.depends:
            self.depends["remove"] = []
        self.depends["remove"].append(bug_id)
        self.dirty.add("depends")

    def add_blocks(self, bug_id):
        if "add" not in self.blocks:
            self.blocks["add"] = []
        self.blocks["add"].append(bug_id)
        self.dirty.add("blocks")

    def remove_blocks(self, bug_id):
        if "remove" not in self.blocks:
            self.blocks["remove"] = []
        self.blocks["remove"].append(bug_id)
        self.dirty.add("blocks")


class MockBugzilla(Bugzilla):
    def __init__(self, config):
        self.api_url = config["bugzilla"]["url"]
        self.bz_url = bz_url_from_api_url(self.api_url)
        self.output = sys.stdout
        self.known_bugs = []

    def _log(self, data):
        self.output.write(data)
        self.output.write("\n")

    def bug_ctx(self, bug_id):
        return MockBugContext(self, bug_id)

    def new(self, summary, comment, product, component, whiteboard=None, priority=None,
            url=None):
        self._log("Creating a bug in component %s :: %s\nSummary: %s\nComment: %s\n"
                  "Whiteboard: %s\nPriority: %s URL: %s" %
                  (product, component, summary, comment, whiteboard, priority, url))
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

    def get_whiteboard(self, bug):
        return "fake data"

    def get_status(self, bug):
        return ("NEW", None)

    def set_status(self, bug, status):
        self._log("Setting bug %s status %s" % (bug, status))


class MockBugContext(object):
    def __init__(self, bugzilla, bug_id):
        self.bugzilla = bugzilla
        self.bug_id = bug_id
        self.changes = None
        self.comment = None

    def __enter__(self):
        self.changes = []
        self.comment = None
        return self

    def __exit__(self, *args, **kwargs):
        for item in self.changes:
            self.bugzilla._log("%s\n" % item)

    def __setitem__(self, name, value):
        self.changes.append("Setting bug %s %s %s" % (self.bug_id,
                                                      name, value))

    def needinfo(self, *requestees):
        for requestee in requestees:
            self.changes.append("Setting bug %s needinfo %s" % (self.bug_id, requestee))

    def add_comment(self,
                    comment,
                    check_dupe=True,
                    comment_tags=None,
                    is_private=False,
                    is_markdown=False):
        if self.comment is not None:
            raise ValueError("Can only set one comment per bug")
        self.comment = {"comment": comment,
                        "is_markdown": is_markdown,
                        "is_private": is_private}
        if comment_tags is not None:
            self.comment["comment_tags"] = comment_tags

    def get_comments(self):
        return []

    def add_attachment(self, data, file_name, summary, content_type="text/plain",
                       comment=None, is_patch=False, is_private=False, is_markdown=False,
                       flags=None):
        body = {
            "data": base64.encodestring(data),
            "file_name": file_name,
            "summary": summary,
            "content_type": content_type
        }
        if comment:
            body["comment"] = comment
        if is_patch:
            body["is_patch"] = is_patch
        if is_private:
            body["is_private"] = is_private
        if is_markdown:
            body["is_markdown"] = is_markdown
        if flags:
            body["flags"] = flags
        self.changes.append("Setting bug %s add_attachment: %r" %
                            (self.bug_id, body))

    def add_depends(self, bug_id):
        self.changes.append("Setting bug %s add_depends %s" % (self.bug_id, bug_id))

    def remove_depends(self, bug_id):
        self.changes.append("Setting bug %s remove_depends %s" % (self.bug_id, bug_id))

    def add_blocks(self, bug_id):
        self.changes.append("Setting bug %s add_blocks %s" % (self.bug_id, bug_id))

    def remove_blocks(self, bug_id):
        self.changes.append("Setting bug %s remove_blocks %s" % (self.bug_id, bug_id))
