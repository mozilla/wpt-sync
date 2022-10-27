import base64
import re
import sys
import traceback

import bugsy
import newrelic
import six
from six.moves import urllib

from . import log
from .env import Environment

MYPY = False
if MYPY:
    from typing import Any, Dict, List, Optional, Set, Text, Tuple, Union
    from bugsy import Bug

env = Environment()

logger = log.get_logger(__name__)


max_comment_length = 65535

# Hack because bugsy has an incomplete list of statuses
if "REOPENED" not in bugsy.bug.VALID_STATUS:
    bugsy.bug.VALID_STATUS.append("REOPENED")

# Trying to send this field makes bug creation fail
if "cc_detail" in bugsy.bug.ARRAY_TYPES:
    bugsy.bug.ARRAY_TYPES.remove("cc_detail")


def bz_url_from_api_url(api_url):
    if api_url is None:
        return None
    parts = urllib.parse.urlparse(api_url)
    bz_url = (parts.scheme, parts.netloc, "", "", "", "")
    return urllib.parse.urlunparse(bz_url)


def bug_number_from_url(url: Text) -> Optional[Text]:
    if url is None:
        return None
    bugs = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("id")
    if bugs:
        return bugs[0]
    return None


status_re = re.compile(r"\[wptsync ([^\[ ]+)(?: ([^\[ ]+))?\]")


def get_sync_data(whiteboard: Text) -> Tuple[Optional[Text], Optional[Text]]:
    matches = status_re.findall(whiteboard)
    if matches:
        subtype, status = matches[0]
        if not status:
            status = None
        return subtype, status
    return None, None


def set_sync_data(whiteboard: Text, subtype: Optional[Text], status: Optional[Text]) -> Text:
    if subtype is None:
        raise ValueError

    if status:
        text = "[wptsync {} {}]".format(subtype, status)
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
        text = text[:max_comment_length - 3] + "[\u2026]"
        logger.error("Truncating comment that exceeds maximum length")
    return text


class Bugzilla:
    bug_cache: Dict[int, Bug] = {}

    def __init__(self, config):
        self.api_url = config["bugzilla"]["url"]
        self.bz_url = bz_url_from_api_url(self.api_url)
        self.bugzilla = bugsy.Bugsy(bugzilla_url=self.api_url,
                                    api_key=config["bugzilla"]["apikey"])
        if "flags" not in self.bugzilla.DEFAULT_SEARCH:
            self.bugzilla.DEFAULT_SEARCH += ["flags"]

    def bug_ctx(self, bug_id: int) -> BugContext:
        return BugContext(self, bug_id)

    def bugzilla_url(self, bug_id: int) -> Text:
        return "{}/show_bug.cgi?id={}".format(self.bz_url, bug_id)

    def id_from_url(self, url, bz_url=None):
        if bz_url is None:
            bz_url = self.bz_url
        if not url.startswith(bz_url):
            return None
        parts = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qs(parts.query)
        if "id" not in query or len(query["id"]) != 1:
            return None
        return query["id"][0]

    def _get_bug(self, bug_id: int) -> Optional[Bug]:
        if bug_id not in self.bug_cache:
            try:
                bug = self.bugzilla.get(bug_id)
            except bugsy.BugsyException:
                logger.error("Failed to retrieve bug with id %s" % bug_id)
                return None
            except Exception as e:
                logger.error("Failed to retrieve bug with id {}: {}".format(bug_id, e))
                newrelic.agent.record_exception()
                return None

            self.bug_cache[bug_id] = bug
        return self.bug_cache[bug_id]

    def comment(self,
                bug_id: int,
                comment: Text,
                **kwargs: Any
                ) -> None:
        bug = self._get_bug(bug_id)
        if bug is None:
            logger.error("Failed to find bug {} to add comment:\n{}".format(bug_id, comment))
            return
        body = {
            "comment": check_valid_comment(comment)
        }
        body.update(kwargs)
        self.bugzilla.request('bug/{}/comment'.format(bug.id),
                              method='POST', json=body)

    def new(self,
            summary: Text,
            comment: Text,
            product: Text,
            component: Text,
            whiteboard: Optional[Text] = None,
            priority: Optional[Text] = None,
            url: Optional[Text] = None,
            bug_type: Text = "task",
            assign_to_sync: bool = True
            ):
        # type (...) -> int
        bug = bugsy.Bug(self.bugzilla,
                        type=bug_type,
                        summary=summary,
                        product=product,
                        component=component)
        if assign_to_sync:
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

    def set_component(self,
                      bug: Union[Bug, int],
                      product: Optional[Text] = None,
                      component: Optional[Text] = None
                      ):
        # type (...) -> None
        if not isinstance(bug, bugsy.Bug):
            bug = self._get_bug(bug)
        if bug is None:
            logger.error("Failed to find bug %s to set component: %s::%s" %
                         (bug, product, component))
            return

        if product is not None:
            bug.product = product
        if component is not None:
            bug.component = component

        if product is not None or component is not None:
            try:
                self.bugzilla.put(bug)
            except bugsy.BugsyException:
                logger.error("Failed to set component {} :: {}".format(bug.product, bug.component))

    def set_whiteboard(self,
                       bug: Union[Bug, int],
                       whiteboard: Text
                       ):
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
            logger.warning("Problem setting Bug {} Whiteboard: {}".format(bug.id, e))
            newrelic.agent.record_exception()

    def get_whiteboard(self, bug: Union[Bug, int]) -> Optional[Text]:
        if not isinstance(bug, bugsy.Bug):
            bug = self._get_bug(bug)
        if not bug:
            return None
        return bug._bug.get("whiteboard", "")

    def get_status(self, bug: Union[Bug, int]) -> Optional[Tuple[Text, Text]]:
        if not isinstance(bug, bugsy.Bug):
            bug = self._get_bug(bug)
        if not bug:
            return None
        return (bug.status, bug.resolution)

    def set_status(self,
                   bug: Union[Bug, int],
                   status: Text,
                   resolution: Optional[Text] = None
                   ) -> None:
        if not isinstance(bug, bugsy.Bug):
            bug = self._get_bug(bug)
        if not bug:
            return None
        bug.status = status
        if resolution is not None:
            assert status == "RESOLVED"
            bug.resolution = resolution
        self.bugzilla.put(bug)

    def get_dupe(self, bug_id: Union[Bug, int]) -> Optional[int]:
        if not isinstance(bug_id, bugsy.Bug):
            bug = self._get_bug(bug_id)
        else:
            bug = bug_id
        if not bug:
            return None
        return bug._bug.get("dupe_of")


class BugContext:
    def __init__(self,
                 bugzilla: Bugzilla,
                 bug_id: int
                 ):
        self.bugzilla = bugzilla
        self.bug_id = bug_id

    def __enter__(self) -> BugContext:
        self.bug: Bug = self.bugzilla._get_bug(self.bug_id)
        self._comments = None
        self.comment: Optional[Dict[Text, Any]] = None
        self.attachments: List[Dict[Text, Any]] = []
        self.depends: Dict[Text, List[int]] = {"add": [], "remove": []}
        self.blocks: Dict[Text, List[int]] = {"add": [], "remove": []}
        self.dirty: Set[Text] = set()

        return self

    def __exit__(self, *args: Any, **kwargs: Any) -> None:
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
                self.bug.depends_on.extend(self.depends["add"])
                for item in self.depends["remove"]:
                    if item in self.bug.depends_on:
                        self.bug.depends_on.remove(item)
            if "blocks" in self.dirty:
                self.bug.blocks.extend(self.blocks["add"])
                for item in self.blocks["remove"]:
                    if item in self.bug.blocks:
                        self.bug.blocks.remove(item)
            if self.dirty:
                self.bugzilla.bugzilla.put(self.bug)

        # Poison the object so it can't be used outside a context manager
        self.bug = None

    def __setitem__(self,
                    name: Text,
                    value: Any
                    ):
        if name == "comment":
            return self.add_comment(value)
        self.bug._bug[name] = value
        self.dirty.add(name)

    def add_comment(self,
                    comment: Text,
                    check_dupe: bool = True,
                    comment_tags: List[Text] = None,
                    is_private: bool = False,
                    is_markdown: bool = False
                    ):
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
        # type () -> List[bugsy.Comment]
        if self._comments is None:
            self._comments = self.bug.get_comments()
        return self._comments

    def needinfo(self, *requestees: Text) -> None:
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

    def add_attachment(self,
                       data: bytes,
                       file_name: Text,
                       summary: Text,
                       content_type: Text = "text/plain",
                       comment=None,  # type Optional[Text]
                       is_patch: bool = False,
                       is_private: bool = False,
                       is_markdown: bool = False,
                       flags: Optional[List[Text]] = None
                       ):
        body: Dict[Text, Any] = {
            "data": base64.encodestring(data).decode("ascii"),
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

        self.attachments.append(body)
        self.dirty.add("attachment")

    def add_depends(self, bug_id: int) -> None:
        self.depends["add"].append(bug_id)
        self.dirty.add("depends")

    def remove_depends(self, bug_id: int) -> None:
        self.depends["remove"].append(bug_id)
        self.dirty.add("depends")

    def add_blocks(self, bug_id: int) -> None:
        self.blocks["add"].append(bug_id)
        self.dirty.add("blocks")

    def remove_blocks(self, bug_id: int) -> None:
        self.blocks["remove"].append(bug_id)
        self.dirty.add("blocks")


class MockBugzilla(Bugzilla):
    def __init__(self, config):
        self.api_url = config["bugzilla"]["url"]
        self.bz_url = bz_url_from_api_url(self.api_url)
        self.output = sys.stdout
        self.known_bugs = []
        self.dupes = {}

    def _log(self, data: Union[Text, bytes]) -> None:
        data = six.ensure_text(data)
        self.output.write(data)
        self.output.write("\n")

    def bug_ctx(self, bug_id: int) -> BugContext:
        return MockBugContext(self, bug_id)

    def new(self,
            summary: Text,
            comment: Text,
            product: Text,
            component: Text,
            whiteboard: Optional[Text] = None,
            priority: Optional[Text] = None,
            url: Optional[Text] = None,
            bug_type: Text = "task",
            assign_to_sync: bool = True,
            ) -> int:
        self._log("Creating a bug in component {product} :: {component}\nSummary: {summary}\n"
                  "Comment: {comment}\nWhiteboard: {whiteboard}\nPriority: {priority}\n"
                  "URL: {url}\nType: {bug_type}\nAssign to sync: {assign_to_sync}".format(
                      product=product,
                      component=component,
                      summary=summary,
                      comment=comment,
                      whiteboard=whiteboard,
                      priority=priority,
                      url=url,
                      bug_type=bug_type,
                      assign_to_sync=assign_to_sync))
        if self.known_bugs:
            bug_id = self.known_bugs[-1] + 1
        else:
            bug_id = 100000
        self.known_bugs.append(bug_id)
        return bug_id

    def comment(self,
                bug_id: int,
                comment: Text,
                **kwargs: Any
                ) -> None:
        self._log("Posting to bug {}:\n{}".format(bug_id, comment))

    def set_component(self, bug_id: int, product: Optional[Text] = None, component: Optional[Text] = None) -> None:
        self._log("Setting bug {} product: {} component: {}".format(bug_id, product, component))

    def set_whiteboard(self, bug_id: int, whiteboard: Text) -> None:
        self._log("Setting bug {} whiteboard: {}".format(bug_id, whiteboard))

    def get_whiteboard(self, bug: Union[Bug, int]) -> Text:
        return "fake data"

    def get_status(self, bug: Union[Bug, int]) -> Tuple[Text, Text]:
        return ("NEW", "")

    def set_status(self, bug: Union[Bug, int], status: Text, resolution: Optional[Text] = None) -> None:
        self._log("Setting bug {} status {}".format(bug, status))

    def get_dupe(self, bug: Union[Bug, int]) -> Optional[int]:
        return self.dupes.get(bug)


class MockBugContext(BugContext):
    def __init__(self, bugzilla: MockBugzilla, bug_id: int) -> None:
        self.bugzilla = bugzilla
        self.bug_id = bug_id

    def __enter__(self) -> BugContext:
        self.changes: List[Text] = []
        self.comment: Optional[Dict[Text, Any]] = None
        return self

    def __exit__(self, *args: Any, **kwargs: Any) -> None:
        for item in self.changes:
            self.bugzilla._log("%s\n" % item)  # type: ignore

    def __setitem__(self, name: Text, value: Text) -> None:
        self.changes.append("Setting bug {} {} {}".format(self.bug_id,
                                                          name, value))

    def add_comment(self,
                    comment: Text,
                    check_dupe: bool = True,
                    comment_tags: List[Text] = None,
                    is_private: bool = False,
                    is_markdown: bool = False
                    ) -> None:
        if self.comment is not None:
            raise ValueError("Can only set one comment per bug")
        self.comment = {"comment": comment,
                        "is_markdown": is_markdown,
                        "is_private": is_private}
        if comment_tags is not None:
            self.comment["comment_tags"] = comment_tags

    def get_comments(self):
        # type () -> List[bugsy.Comment]
        return []

    def needinfo(self, *requestees: Text) -> None:
        for requestee in requestees:
            self.changes.append("Setting bug {} needinfo {}".format(self.bug_id, requestee))

    def add_attachment(self,
                       data: bytes,
                       file_name: Text,
                       summary: Text,
                       content_type: Text = "text/plain",
                       comment=None,  # type Optional[Text]
                       is_patch: bool = False,
                       is_private: bool = False,
                       is_markdown: bool = False,
                       flags: Optional[List[Text]] = None
                       ):
        body: Dict[Text, Any] = {
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

    def add_depends(self, bug_id: int) -> None:
        self.changes.append("Setting bug {} add_depends {}".format(self.bug_id, bug_id))

    def remove_depends(self, bug_id: int) -> None:
        self.changes.append("Setting bug {} remove_depends {}".format(self.bug_id, bug_id))

    def add_blocks(self, bug_id: int) -> None:
        self.changes.append("Setting bug {} add_blocks {}".format(self.bug_id, bug_id))

    def remove_blocks(self, bug_id: int) -> None:
        self.changes.append("Setting bug {} remove_blocks {}".format(self.bug_id, bug_id))
