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

    git_work = sync.gecko_worktree.get()
    path_prefix = env.config["gecko"]["path"]["wpt"]

    seen = set()

    for test_results, bug_data, link_status, require_opt_in in [
            (new_crashes, bug_data_crash, "CRASH", False),
            (new_failures, bug_data_failure, None, True)]:

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

            if require_opt_in and component not in opt_in_components:
                logger.info("Not filing bugs for component %s" % component)
                continue

            product, component = component.split(" :: ")
            summary, comment = bug_data(sync, test_results)
            bug_id = make_bug(summary, comment, product, component, [sync.bug])
            rv[bug_id] = [item + (link_status,) for item in test_results]

    return rv


def bug_data_crash(sync, test_results):
    summary = "New wpt crashes from PR %s" % sync.pr

    comment = """The following tests have crashes in the CI runs for wpt PR %s:
%s

(getting the crash signature into these bug reports is a TODO; sorry)

These updates will be on mozilla-central once bug %s lands.

""" % (sync.pr,
       detail_part(None, test_results, None, "head", False),
       sync.bug)

    comment += postscript

    return summary, comment


def bug_data_failure(sync, test_results):
    summary = "New wpt failures from PR %s" % sync.pr

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

    comment = """The following tests have untriaged failures in the CI runs for wpt PR %s:

%s
These updates will be on mozilla-central once bug %s lands.

""" % (sync.pr, "\n".join(detail_msg), sync.bug)

    comment += postscript

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
