import git


def is_ancestor(git_obj, rev, branch):
    try:
        git_obj.git.merge_base(rev, branch, is_ancestor=True)
    except git.GitCommandError:
        return False
    return True
