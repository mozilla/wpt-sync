import os

from sync import base, repos, worktree
from sync.lock import SyncLock


def test_create_delete(env, git_gecko, hg_gecko_upstream):
    git_gecko.remotes.mozilla.fetch()
    process_name = base.ProcessName("sync", "downstream", "1", "0")
    wt = worktree.Worktree(git_gecko, process_name)
    with SyncLock.for_process(process_name) as lock:
        base.BranchRefObject.create(lock, git_gecko, process_name, "FETCH_HEAD")
        with wt.as_mut(lock):
            wt.get()

            assert os.path.exists(wt.path)
            # This is a gecko repo so it ought to have created a state directory
            state_path = repos.Gecko.get_state_path(env.config, wt.path)
            assert os.path.exists(state_path)

            wt.delete()
            assert not os.path.exists(wt.path)
            assert not os.path.exists(state_path)
