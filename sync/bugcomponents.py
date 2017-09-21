from lib2to3 import pygram, pytree, patcomp
from lib2to3.pgen2 import driver
from ast import literal_eval
import re
import os

from . import log


logger = log.get_logger(__name__)

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


def remove_obsolete(path):
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
            node_patterns[path_pat] = node

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

    for pattern in unmatched_patterns:
        logger.debug("Removing %s" % pattern)
        node_patterns[pattern].remove()

    return unicode(tree)
