import re
import os
from ast import literal_eval
from collections import defaultdict
from lib2to3 import pygram, pytree, patcomp
from lib2to3.pgen2 import driver

from . import log
from env import Environment
from .projectutil import Mach

logger = log.get_logger(__name__)
env = Environment()

# Copied from mozpack.path
re_cache = {}


def match(path, pattern):
    '''
    Return whether the given path matches the given pattern.
    An asterisk can be used to match any string, including the null string, in
    one part of the path:
        'foo' matches '*', 'f*' or 'fo*o'
    However, an asterisk matching a subdirectory may not match the null string:
        'foo/bar' does *not* match 'foo/*/bar'
    If the pattern matches one of the ancestor directories of the path, the
    patch is considered matching:
        'foo/bar' matches 'foo'
    Two adjacent asterisks can be used to match files and zero or more
    directories and subdirectories.
        'foo/bar' matches 'foo/**/bar', or '**/bar'
    '''
    if not pattern:
        return True
    if pattern not in re_cache:
        p = re.escape(pattern)
        p = re.sub(r'(^|\\\/)\\\*\\\*\\\/', r'\1(?:.+/)?', p)
        p = re.sub(r'(^|\\\/)\\\*\\\*$', r'(?:\1.+)?', p)
        p = p.replace(r'\*', '[^/]*') + '(?:/.*)?$'
        re_cache[pattern] = re.compile(p)
    return re_cache[pattern].match(path) is not None


def remove_obsolete(path, moves=None):
    files_pattern = "with_stmt< 'with' power< 'Files' trailer< '(' arg=any any* ')' > any* > any* >"
    base_dir = os.path.dirname(path) or "."
    d = driver.Driver(pygram.python_grammar,
                      convert=pytree.convert)
    tree = d.parse_file(path)
    pc = patcomp.PatternCompiler()
    pat = pc.compile_pattern(files_pattern)

    unmatched_patterns = set()
    node_patterns = {}

    for node in tree.children:
        match_values = {}
        if pat.match(node, match_values):
            path_pat = literal_eval(match_values['arg'].value)
            unmatched_patterns.add(path_pat)
            node_patterns[path_pat] = (node, match_values)

    for base_path, dirs, files in os.walk(base_dir):
        for filename in files:
            path = os.path.relpath(os.path.join(base_path, filename),
                                   base_dir)
            assert ".." not in path
            if path[:2] == "./":
                path = path[2:]
            for pattern in unmatched_patterns.copy():
                if match(path, pattern):
                    unmatched_patterns.remove(pattern)

    if moves:
        moved_patterns = compute_moves(moves, unmatched_patterns)
        unmatched_patterns -= set(moved_patterns.keys())
        for old_pattern, new_pattern in moved_patterns.iteritems():
            node, match_values = node_patterns[old_pattern]
            arg = match_values["arg"]
            arg.replace(arg.__class__(arg.type, '"%s"' % new_pattern))

    for pattern in unmatched_patterns:
        logger.debug("Removing %s" % pattern)
        node_patterns[pattern][0].remove()

    return unicode(tree)


def compute_moves(moves, unmatched_patterns):
    updated_patterns = {}
    dest_paths = defaultdict(list)
    for pattern in unmatched_patterns:
        # Make things simpler by only considering patterns matching subtrees
        # or single-file patterns
        if "*" in pattern and not pattern.endswith("/**"):
            continue
        for from_path, to_path in moves.iteritems():
            if match(from_path, pattern):
                dest_paths[pattern].append(to_path)

    for pattern, paths in dest_paths.iteritems():
        if "*" not in pattern:
            assert len(paths) == 1
            updated_patterns[pattern] = paths[0]
        elif pattern.endswith("/**"):
            prefix = os.path.commonprefix(paths)
            if not prefix:
                continue
            if not prefix.endswith("/"):
                prefix = os.path.dirname(prefix)
                if not prefix:
                    continue
                prefix += "/"

            updated_patterns[pattern] = prefix + "**"

    return updated_patterns


def get(git_gecko, files_changed, default):
    if not files_changed:
        return default

    path_prefix = env.config["gecko"]["path"]["wpt"]
    paths = [os.path.join(path_prefix, item) for item in files_changed]

    mach = Mach(git_gecko.working_dir)
    output = mach.file_info("bugzilla-component", *paths)

    components = defaultdict(int)
    current = None
    for line in output.split("\n"):
        if line.startswith(" "):
            assert current is not None
            components[current] += 1
        else:
            current = line.strip()

    if not components:
        return default

    components = sorted(components.items(), key=lambda x: -x[1])
    component = components[0][0]
    if component == "UNKNOWN" and len(components) > 1:
        component = components[1][0]

    if component == "UNKNOWN":
        return default

    return component.split(" :: ")
