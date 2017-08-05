# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

from __future__ import absolute_import, print_function, unicode_literals

"""Functionality to support VCS syncing for WPT."""

import logging
import os
import re
import subprocess
import sys
import shutil
import uuid
from ConfigParser import (
    RawConfigParser,
)

from mozvcssync.gitutil import GitCommand

import settings
import repos
from projectutil import Command

rev_re = re.compile("revision=(?P<rev>[0-9a-f]{40})")
logger = logging.getLogger('wpt-sync')


@settings.configure
def downstream(config, body):
    pr_id = body['payload']['pull_request']['number']
    # assuming:
    # - git cinnabar, checkout of the gecko repo,
    #     remotes configured, mercurial python lib
    git_gecko = repos.Gecko(config)
    git_wpt = repos.WebPlatformTests(config)

    get_pr(config['web-platform-tests']["repo"]["url"], git_gecko.root, pr_id)
    gecko_pr_branch = create_fresh_branch(git_gecko.root)
    copy_changes(git_wpt.root,
                 os.path.join(git_gecko.root, config["gecko"]["path"]["wpt"]))
    mach = Command('mach', c['path_to_gecko'])
    mach.get('wpt-manifest-update')
    is_changed = commit_changes(git_gecko.path, config["gecko"]["path"]["wpt"], "PR " + pr_id)
    if is_changed:
        push_to_try(git_gecko.root, gecko_pr_branch)


def load_config(path):
    c = RawConfigParser()
    c.read(path)
    wpt = 'web-platform-tests'

    d = {}
    d.update(c.items(wpt))

    d['pulse_port'] = c.getint(wpt, 'pulse_port')
    d['pulse_ssl'] = c.getboolean(wpt, 'pulse_ssl')

    return d


def configure_stdout():
    # Unbuffer stdout.
    sys.stdout = os.fdopen(sys.stdout.fileno(), 'w', 1)

    # Log to stdout.
    root = logging.getLogger()
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter('%(name)s %(message)s')
    handler.setFormatter(formatter)
    root.addHandler(handler)


def get_pr(git_source_url, git_repo_path, pr_id, ref='master'):
    """ Pull shallow repo and checkout given pr """
    git = GitCommand(os.path.abspath(git_repo_path))
    if not os.path.exists(git_repo_path):
        git.cmd(b'init', git_repo_path)

    git.cmd(b'checkout', b'master')
    git.cmd(b'clean', b'-xdf')
    git.cmd(b'pull', b'--no-tags', b'--ff-only', git_source_url,
            b'heads/%s:heads/%s' % (ref, ref))
    git.cmd(b'fetch', b'--no-tags', git_source_url,
            b'pull/%s/head:heads/pull_%s' % (pr_id, pr_id))
    git.cmd(b'checkout', b'pull_%s' % pr_id)
    git.cmd(b'merge', 'heads/%s' % ref)


def create_fresh_branch(git_repo_path, base="central", tip="central/branches/default/tip"):
    """reset repo and checkout a new branch for the new changes"""
    git = GitCommand(os.path.abspath(git_repo_path))
    git.cmd(b'checkout', base)
    git.cmd(b'pull')
    git.cmd(b'checkout', tip)
    branch = uuid.uuid4().hex
    git.cmd(b'checkout', b'-b', branch)

    return branch


def copy_changes(source, dest, ignore=None):
    # TODO instead of copying files or convertin/moving patches, find
    # a way to apply changesets from one repo to the other (git subtree,
    # read-tree, filter-branch?)
    source = os.path.abspath(source)
    dest = os.path.abspath(dest)
    if ignore is None:
        ignore = ['.git', 'css']

    if os.path.exists(dest):
        assert os.path.isdir(dest)
        shutil.rmtree(dest)

    def ignore_in_path(ignore_path, *patterns):
        patterns_fn = shutil.ignore_patterns(*patterns)

        def ignore(path, names):
            if path == ignore_path:
                return patterns_fn(path, names)
            return []
        return ignore

    shutil.copytree(source, dest, ignore=ignore_in_path(source, *ignore))


def commit_changes(git_repo_path, path, message):
    git = GitCommand(os.path.abspath(git_repo_path))
    # TODO nice commit message
    git.cmd(b'add', b'-A', path)
    if not git.get(b'diff', b'--cached', b'--name-only'):
        logger.info("Nothing to commit")
    git.cmd(b'commit', b'-m', message)
    return True


def get_affected_tests(path_to_wpt, revish=None):
    wpt = Command("wpt", path_to_wpt)
    wpt.get("manifest")
    affected = ["tests-affected", "--show-type", "--new"]
    if revish:
        affected.append(revish)
    s = wpt.get(*affected)
    tests_by_type = defaultdict(set)
    for item in s.strip().split("\n"):
        pair = item.strip().split("\t")
        assert len(pair) == 2
        tests_by_type[pair[1]].add(pair[0])
    return tests_by_type


def construct_try_message(tests_by_type):
    # TODO specify relevant platforms
    # try: -b do -p win32,win64,linux64,linux,macosx64 -u web-platform-tests[linux64-stylo,Ubuntu,10.10,Windows 7,Windows 8,Windows 10] -t none --artifact
    try_message = ("try: -b do -p linux,linux64 -u {test_jobs} "
                   "-t none --artifact --try-test-paths {prefixed_paths}")
    test_type_suite = {
        "testharness": "web-platform-tests",
        "reftest": "web-platform-tests-reftests",
        "wdspec": "web-platform-tests-wdspec",
    }
    test_data = {
        "test_jobs": [],
        "prefixed_paths": [],
    }
    for test_type, paths in tests_by_type.iteritems():
        suite = test_type_suite[test_type]
        if len(paths):
            test_data["test_jobs"].append(suite)
            test_data["test_jobs"].append(suite + "-e10s")
        for p in paths:
            test_data["prefixed_paths"].append(suite + ":" + p)
    test_data["test_jobs"] = ",".join(test_data["test_jobs"])
    test_data["prefixed_paths"] = ",".join(test_data["prefixed_paths"])
    return try_message.format(**test_data)


def push_to_try(git_repo_path, branch):
    # TODO determine affected tests (use new wpt command in upstream repo)
    affected_tests = [
        "testing/web-platform/tests/webdriver",
        "testing/web-platform/tests/2dcontext",
        "testing/web-platform/tests/cookies",
    ]
    results_url = None
    git = GitCommand(os.path.abspath(git_repo_path))
    # TODO specify relevant platforms
    try_message = ("try: -b do -p linux,linux64 -u web-platform-tests-1,web-platform-tests-e10s-1 "
                   "-t none --artifact --try-test-paths ")
    try_message += "".join(["web-platform-tests:" + t for t in affected_tests])
    git.cmd(b'checkout', branch)
    git.cmd(b'commit', b'--allow-empty', b'-m', try_message)
    try:
        output = git.get(b'push', b'try', stderr=subprocess.STDOUT)
        rev_match = rev_re.search(output)
        results_url = ("https://treeherder.mozilla.org/#/"
                       "jobs?repo=try&revision=") + rev_match.group('rev')
    finally:
        git.cmd(b'reset', b'HEAD~')
    # TODO also return task_group_id
    return results_url


if __name__ == '__main__':
    configure_stdout()
    c = load_config("./wpt-sync/sync/example-config.ini")
    t = get_affected_tests("/Users/mfrydrychowicz/dev/web-platform-tests", "a94ae52e4e21d6240fe1bd1b34c27e82ae159109")
    print(t)
    print(construct_try_message(t))
