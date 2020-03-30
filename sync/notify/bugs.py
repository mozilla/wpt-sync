import json
import re
import os
import subprocess
from collections import defaultdict

import newrelic
from six import iteritems, itervalues

from .msg import detail_part
from .. import log
from ..bugcomponents import components_for_wpt_paths
from ..env import Environment
from ..lock import mut
from ..meta import Metadata
from ..projectutil import Mach

logger = log.get_logger(__name__)
env = Environment()

postscript = """Note: this bug is for tracking fixing the issues and is not
owned by the wpt sync bot.

This bug is linked to the relevant tests by an annotation in
https://github.com/web-platform-tests/wpt-metadata. These annotations
can be edited using the wpt interop dashboard
https://jgraham.github.io/wptdash/

If this bug is split into multiple bugs, please also update the
annotations, otherwise we are unable to track which wpt issues are
already triaged. Resolving as duplicate or closing this issue should
be cause the bot to automatically update or remove the annotation.
"""


def test_ids_to_paths(git_work, test_ids):
    mach = Mach(git_work.working_dir)
    data = {}
    min_idx = 0
    group = 100
    try:
        while min_idx < len(test_ids):
            data_str = mach.wpt_test_paths("--json", *test_ids[min_idx:min_idx + group])
            data.update(json.loads(data_str))
            min_idx += group
    except subprocess.CalledProcessError:
        newrelic.agent.record_exception()
        # Fall back to a manual mapping of test ids to paths
        data = fallback_test_ids_to_paths(test_ids)
    return data


def fallback_test_ids_to_paths(test_ids):
    """Fallback for known rules mapping test_id to path, for cases where we
    can't read the manifest"""
    data = defaultdict(list)
    any_re = re.compile(r"(.*)\.any.(?:[^\.]*\.)?html$")
    for test_id in test_ids:
        prefix = env.config["gecko"]["path"]["wpt"]
        suffix = test_id

        if test_id.startswith("/_mozilla/"):
            prefix = os.path.normpath(
                os.path.join(env.config["gecko"]["path"]["wpt"], "..", "mozilla", "tests"))
            suffix = suffix[len("/_mozilla"):]
        m = any_re.match(test_id)
        if m:
            suffix = m.groups()[0] + ".any.js"
        elif test_id.endswith(".worker.html"):
            suffix = test_id.rsplit(".", 1)[0] + ".js"
        elif test_id.endswith(".sharedworker.html"):
            suffix = test_id.rsplit(".", 1)[0] + ".js"
        elif test_id.endswith(".window.html"):
            suffix = test_id.rsplit(".", 1)[0] + ".js"

        path = prefix + suffix
        data[path].append(test_id)
    return data


@mut('sync')
def for_sync(sync, results):
    """Create the bugs for followup work for test problems found in a sync.

    This creates bugs that will be owned by the triage owner of the component
    for any followup work that's revealed by the changes in a sync. Currently
    this is for crashes and certain kinds of failures e.g. Firefox-only failures
    that are not already known in the wpt metadata.

    :returns: A dict {bug_id: bug_info} where bug_info is a list of test results
              that are included in the bug, each represented as a tuple
              (test_id, subtest, results, status)"""
    rv = {}

    new_crashes = list(results.iter_filter(lambda _test, _subtest, result:
                                           (result.has_crash("firefox") and
                                            not result.has_link(status="CRASH"))))

    new_failures = list(results.iter_filter(lambda _test, _subtest, result:
                                            ((result.has_regression("firefox") or
                                              result.has_new_non_passing("firefox") or
                                              result.is_browser_only_failure("firefox")) and
                                             not result.has_link())))

    if not new_failures and not new_crashes:
        return rv

    existing = sync.notify_bugs

    git_work = sync.gecko_worktree.get()
    path_prefix = env.config["gecko"]["path"]["wpt"]

    seen = set()

    for key, test_results, bug_data, link_status, require_opt_in in [
            ("crash", new_crashes, bug_data_crash, "CRASH", False),
            ("failure", new_failures, bug_data_failure, None, True)]:

        # Tests excluding those for which we already generated a bug
        test_results = [item for item in test_results
                        if (item[0], item[1]) not in seen]

        if not test_results:
            continue

        seen |= set((item[0], item[1]) for item in test_results)

        test_ids = list(set(test_id for test_id, _subtest, _result in test_results))
        test_id_by_path = test_ids_to_paths(git_work, test_ids)
        test_path_by_id = {}
        for path, ids in iteritems(test_id_by_path):
            for test_id in ids:
                test_path_by_id[test_id] = os.path.relpath(path, path_prefix)

        paths = set(itervalues(test_path_by_id))
        logger.info("Got paths %s" % (paths,))
        components = components_for_wpt_paths(git_work, paths)

        components_by_path = {}
        for component, paths in iteritems(components):
            for path in paths:
                components_by_path[path] = component

        test_results_by_component = defaultdict(list)

        for test_id, subtest, test_result in test_results:
            component = components_by_path[test_path_by_id[test_id]]
            test_results_by_component[component].append((test_id, subtest, test_result))

        opt_in_components = set(item.strip()
                                for item in
                                env.config["notify"].get("components", "").split(","))

        for component, test_results in iteritems(test_results_by_component):
            if component == "UNKNOWN":
                # For things with no component don't file a bug
                continue

            component_key = "%s :: %s" % (key, component)

            if require_opt_in and component not in opt_in_components:
                logger.info("Not filing bugs for component %s" % component)
                continue

            if component_key not in existing:
                product, component = component.split(" :: ")
                summary, comment = bug_data(sync,
                                            test_results,
                                            results.treeherder_url,
                                            results.wpt_sha)
                bug_id = make_bug(summary, comment, product, component, [sync.bug])
            else:
                bug_id = existing[component_key]

            rv[bug_id] = [item + (link_status,) for item in test_results]

    return rv


class LengthCappedStringBuilder(object):
    def __init__(self, max_length):
        """Builder for a string that must not exceed a given length"""
        self.max_length = max_length
        self.data = []
        self.current_length = 0

    def append(self, other):
        """Add a string the end of the data. Returns True if the add was
        a success i.e. the new string is under the length limit, otherwise
        False"""
        len_other = len(other)
        if len_other + self.current_length > self.max_length:
            return False
        self.data.append(other)
        self.current_length += len_other
        return True

    def has_capacity(self, chars):
        """Check if we have chars remaining capacity in the string"""
        return self.current_length + chars <= self.max_length

    def get(self):
        """Return the complete string"""
        return "".join(self.data)


def split_id(test_id):
    """Convert a test id into a list of path parts, preserving the hash
    and query fragments on the final part.

    Unlike urlparse we don't split out the query or fragment, we want to
    preserve the invariant that "/".join(split_id(test_id)) == test_id

    :param test_id: The id of a test consisting of a url piece containing
                    a path and optionally a query and/or fragment
    :returns: [id_parts] consisting of all the path parts split on /
              with the final element retaining any non-path parts
    """
    parts = test_id.split("/")
    last = None
    for i, part in enumerate(parts):
        if "#" in part or "?" in part:
            last = i
            break
    if last:
        name = "/".join(parts[i:])
        parts = parts[:i]
        parts.append(name)
    return tuple(parts)


def get_common_prefix(test_ids):
    """Given a list of test ids, return the paths split into directory parts,
    and the longest common prefix directory shared by all the inputs.

    :param test_ids: - List of test_ids
    :returns: ([split_name], common_prefix) The unique test_ids split on / and
              the longest path prefix shared by all test ids (excluding filename
              parts
    """
    test_ids = list(test_ids)
    common_prefix = split_id(test_ids[0])[:-1]
    seen_names = set()
    split_names = []
    for test_id in test_ids:
        split_name = split_id(test_id)
        if split_name in seen_names:
            continue
        seen_names.add(split_name)
        split_names.append(split_name)
    common_prefix = os.path.commonprefix([item[:-1] for item in split_names])
    return split_names, common_prefix


def make_summary(test_results, prefix, max_length=255, max_tests=3):
    """Construct a summary for the bugs based on the test results.

    The approach here is to start building the string up using the
    LengthCappedStringBuilder and when we get to an optional part check
    if we have the capacity to add that part in, otherwise use an
    alternative.

    :param test_results: List of (test_id, subtest, result)
    :param prefix: String prefix to use at the start of the summary
    :param max_length: Maximum length of the summary to create
    :param max_tests: Maximum number of tests names to include in the
                      output
    :returns: String containing a constructed bug summary
    """
    if len(prefix) > max_length:
        raise ValueError("Prefix is too long")

    # Start with the prefix
    summary = LengthCappedStringBuilder(max_length)
    summary.append(prefix)

    # If we can fit some of the common path prefix, add that
    split_names, common_test_prefix = get_common_prefix(item[0] for item in test_results)
    joiner = " in "
    if not summary.has_capacity(len(joiner) + len(common_test_prefix[0]) + 1):
        return summary.get()

    # Keep adding as much of the common path prefix as possible
    summary.append(joiner)
    for path_part in common_test_prefix:
        if not summary.append("%s/" % path_part):
            return summary.get()

    test_names = ["/".join(item[len(common_test_prefix):]) for item in split_names]

    # If there's a single test name add that and we're done
    if len(test_names) == 1:
        summary.append(test_names[0])
        return summary.get()

    # If there are multiple test names, add up to max_tests of those names
    # and a suffix
    prefix = " ["
    # suffix is ", and N others]", N is at most len(test_results) so reserve that many
    # characters
    tests_remaining = len(test_names)
    suffix_length = len(", and  others]") + len(str(tests_remaining))
    if summary.has_capacity(len(test_names[0]) + len(prefix) + suffix_length):
        summary.append(prefix)
        summary.append(test_names[0])
        tests_remaining -= 1
        for test_name in test_names[1:max_tests]:
            if summary.has_capacity(2 + len(test_name) + suffix_length):
                summary.append(", %s" % test_name)
                tests_remaining -= 1
        if tests_remaining > 0:
            summary.append(", and %s others]" % tests_remaining)
        else:
            summary.append("]")
    else:
        # If we couldn't fit any test names in try just adding the number of tests
        summary.append(" [%s tests]" % tests_remaining)
    return summary.get()


def bug_data_crash(sync, test_results, treeherder_url, wpt_sha):
    summary = make_summary(test_results,
                           "New wpt crashes")

    comment = """Syncing wpt \
[PR %(pr_id)s](https://github.com/web-platform-tests/wpt/pull/%(pr_id)s)\
 found new crashes in CI

# Affected Tests

%(details)s

# CI Results

[Gecko CI (Treeherder)](%(treeherder_url)s)
[GitHub PR Head](https://wpt.fyi/results/?sha=%(wpt_sha)s&label=pr_head)

# Notes

Getting the crash signature into these bug reports is a TODO; sorry

These updates will be on mozilla-central once bug %(sync_bug_id)s lands.

%(postscript)s""" % {"pr_id": sync.pr,
                     "details": detail_part(None, test_results, None, "head", False),
                     "treeherder_url": treeherder_url,
                     "wpt_sha": wpt_sha,
                     "sync_bug_id": sync.bug,
                     "postscript": postscript}

    return summary, comment


def bug_data_failure(sync, test_results, treeherder_url, wpt_sha):
    summary = make_summary(test_results,
                           "New wpt failures")

    by_type = defaultdict(list)
    for (test, subtest, result) in test_results:
        if result.is_browser_only_failure("firefox"):
            by_type["firefox-only"].append((test, subtest, result))
        elif result.has_regression("firefox"):
            by_type["regression"].append((test, subtest, result))
        elif result.has_new_non_passing("firefox"):
            by_type["new-non-passing"].append((test, subtest, result))

    detail_msg = []
    for (details_type, test_results, include_other_browser) in [
            ("Firefox-only failures", by_type["firefox-only"], False),
            ("Tests with a Worse Result After Changes", by_type["regression"], True),
            ("New Tests That Don't Pass", by_type["new-non-passing"], True)]:
        if not test_results:
            continue
        detail_msg.append(detail_part(details_type, test_results, None, "head",
                                      include_other_browser))

    comment = """Syncing wpt \
[PR %(pr_id)s](https://github.com/web-platform-tests/wpt/pull/%(pr_id)s) \
found new untriaged test failures in CI

# Tests Affected

%(details)s

# CI Results

[Gecko CI (Treeherder)](%(treeherder_url)s)
[GitHub PR Head](https://wpt.fyi/results/?sha=%(wpt_sha)s&label=pr_head)

# Notes

These updates will be on mozilla-central once bug %(sync_bug_id)s lands.

%(postscript)s""" % {"pr_id": sync.pr,
                     "details": "\n".join(detail_msg),
                     "treeherder_url": treeherder_url,
                     "wpt_sha": wpt_sha,
                     "sync_bug_id": sync.bug,
                     "postscript": postscript}

    return summary, comment


def make_bug(summary, comment, product, component, depends):
    bug_id = env.bz.new(summary, comment, product, component, whiteboard="[wpt]",
                        bug_type="defect")
    with env.bz.bug_ctx(bug_id) as bug:
        for item in depends:
            bug.add_depends(item)
    return bug_id


@mut('sync')
def update_metadata(sync, bugs):
    # TODO: Ensure that the metadata is added to the meta repo
    if not bugs:
        return

    metadata = Metadata.for_sync(sync, create_pr=True)
    with metadata.as_mut(sync._lock):
        for bug_id, test_results in iteritems(bugs):
            for (test_id, subtest, results, status) in test_results:
                metadata.link_bug(test_id,
                                  env.bz.bugzilla_url(bug_id),
                                  product="firefox",
                                  subtest=subtest,
                                  status=status)
