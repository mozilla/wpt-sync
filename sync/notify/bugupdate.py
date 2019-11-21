from collections import defaultdict
from datetime import datetime
from six import iteritems

import bugsy

from ..base import ProcessName, ProcessData
from ..env import Environment
from ..lock import mut, MutGuard, ProcLock
from ..meta import Metadata

env = Environment()

bugzilla_url = "https://bugzilla.mozilla.org"


def from_iso_str(datetime_str):
    return datetime.strptime(datetime_str,
                             '%Y-%m-%dT%H:%M:%S.%f')


class ProcData(ProcessData):
    obj_type = "proc"


class TriageBugs(object):
    process_name = ProcessName("proc", "bugzilla", 0, 0)

    def __init__(self, repo):
        self._lock = None
        self.data = ProcData(repo, self.process_name)
        # TODO: not sure the locking here is correct
        self.wpt_metadata = Metadata(self.process_name)
        self._last_update = None

    def as_mut(self, lock):
        return MutGuard(lock, self, [self.data,
                                     self.wpt_metadata])

    @property
    def lock_key(self):
        return (self.process_name.subtype,
                self.process_name.obj_id)

    @property
    def last_update(self):
        if self._last_update is None and "last_update" in self.data:
            self._last_update = from_iso_str(self.data["last-update"])
        return self._last_update

    @last_update.setter
    @mut()
    def last_update(self, value):
        self.data["last-update"] = value.isoformat()
        self._last_update = None

    def meta_links(self):
        rv = defaultdict(list)
        for link in self.wpt_metadata.iterbugs(test_id=None,
                                               product="firefox",
                                               prefixes=(bugzilla_url,)):
            bug = int(env.bz.id_from_url(link.url, bugzilla_url))
            rv[bug].append(link)
        return rv

    def updated_bugs(self, bug_ids):
        """Get a list of all bugs which are associated with wpt results and have had their
        resolution changed since the last update time

        :param bug_ids: List of candidate bugs to check
        """
        rv = []

        params = {}
        update_date = None
        if self.last_update:
            update_date = self.last_update.strftime("%Y-%m-%d")
            params["chfieldfrom"] = update_date

        if bug_ids:
            # TODO: this could make the query over-long; we should probably split
            # into multiple queries
            params["bug_id"] = ",".join(str(item) for item in bug_ids)

        search_resp = env.bz.bugzilla.session.get("%s/rest/bug" % bugzilla_url,
                                                  params=params)
        search_resp.raise_for_status()
        search_data = search_resp.json()
        if self.last_update:
            history_params = {"new_since": update_date}
        else:
            history_params = {}
        for bug in search_data.get("bugs", []):
            if (not self.last_update or
                from_iso_str(bug["last_change_time"]) > self.last_update):

                history_resp = env.bz.bugzilla.session.get(
                    "%s/rest/bug/%s/history" % (bugzilla_url, bug["id"]),
                    params=history_params)
                history_resp.raise_for_status()
                history_data = history_resp.json()
                bugs = history_data.get("bugs")
                if not bugs:
                    continue
                assert len(bugs) == 1
                for entry in bugs[0].get("history", []):
                    if not self.last_update or from_iso_str(entry["when"]) > self.last_update:
                        if any(change["field_name"] == "resolution" for change in entry["changes"]):
                            rv.append(bugsy.Bug(env.bz.bugzilla, **bug))
                            continue
        return rv


def update_triage_bugs(git_gecko, comment=True):
    triage_bugs = TriageBugs(git_gecko)

    run_time = datetime.now()
    meta_links = triage_bugs.meta_links()

    updates = {}

    for bug in triage_bugs.updated_bugs(meta_links.keys()):
        if bug.resolution == "INVALID":
            updates[bug.id] = None
        elif bug.resolution == "DUPLICATE":
            duped_to = bug._bug["dupe_of"]
            while duped_to:
                final_bug = duped_to
                duped_to = env.bz.get_dupe(final_bug)
            updates[bug.id] = final_bug

        # TODO: handle some more cases here. Notably where the bug is marked as
        # FIXED, but the tests don't actually pass

    removed_by_bug = {}

    with ProcLock.for_process(TriageBugs.process_name) as lock:
        with triage_bugs.as_mut(lock):
            for old_bug, new_bug in iteritems(updates):
                links = meta_links[old_bug]
                if new_bug is None:
                    removed_by_bug[old_bug] = links
                    for item in links:
                        item.delete()
                else:
                    new_url = env.bz.bugzilla_url(new_bug)
                    for link in links:
                        link.url = new_url
            triage_bugs.last_update = run_time

    # Now that the above change is commited, add some comments to bugzilla for the
    # case where we removed URLs
    comments = {}
    for bug, old_links in iteritems(removed_by_bug):
        comments[bug] = comment_removed(bug, old_links, submit_comment=comment)

    return updates, comments


def comment_removed(bug_id, links, submit_comment=True):
    by_test = defaultdict(list)
    for link in links:
        by_test[link.test_id].append(link)

    triage_lines = []
    for test_id, links in sorted(iteritems(by_test)):
        triage_lines.append(test_id)
        for link in links:
            parts = []
            if link.subtest:
                parts.append("subtest: %s" % link.subtest)
            if link.status:
                parts.append("status: %s" % link.status)
            if not parts:
                parts.append("Parent test, any status")
            triage_lines.append("  %s" % " ".join(parts))

    with env.bz.bug_ctx(bug_id) as bug:
        comment = """Resolving bug as invalid removed the following wpt triage data:

%s""" % "\n".join(triage_lines)
        if submit_comment:
            bug.add_comment(comment)

    return comment
