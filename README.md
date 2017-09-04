# Implementation strategy

## Downstreaming

* Given an upstream PR

* Create a bug in a component determined by the files changed

* Wait until it is approved or the Travis status passes

* Create a local Try run based on mozilla-central for an artifact
  build + the changes, and run only tests that changed

* Update local metadata for the expectation changes.

* Run a stability checking run with --rebuild=10

* Use the results of this second try run to disable any obviously-unstable tests

* Repeat as required for new pushes to the PR (should reuse metadata
  but not disabled tests)

### Disaster Recovery

* Miss the PR opening
 - Should get later events with the PR; notice we don't have a record
   of it and start the sync process above.

* Miss the Travis status changing or the PR being approved
* PR is merged without a clean travis run or approval (by an
  admin).
  - Start the downstreaming process at the point the PR is merged.

* Rebasing the changes onto m-c causes a merge conflict.
 - This implies that we are upstreaming something that will also have
   a merge conflict.
 - One option is to fix on our upstreaming branch and then wait until
   we get a push that will rebase cleanly. But then we miss out on
   early metadata generation.
 - Could fix locally and continue the process, which would allow us
   to update metadata at the expense of double work (we may have to
   fix the conflict *again* when we deal with a push).
 - Maybe want a command to continue the process after manual rebase.

* Error on Try (e.g. build failed)
  - Manual rebase and repush? Maybe want a command for this so we
    update the task that we are waiting for

* Error updating metadata / disabling tests
  - Needs manual investigation and fixup. Might need to update the
    status of the sync to say we have metadata.

* Change breaks the runner
  - Need to notice that this happened. Probably need to make some
    local fixup and ensure that this is  upstreamed asap.

## Upstreaming

* See a push to mozilla-inbound or autoland touching
  testing/web-platform-tests

* Check all pushes since last merge to central to eliminate backouts

* If a previous sync push was backed out, close the related PR.

* Rebase the changes onto latest wpt-master (alternative: use last
  sync push. Means we shouldn't get rebase errors, but might get merge
  conflicts in the PR).

* Create a remote branch with the commits

* Create a PR for the remote branch and auto approve the commits

* Wait for the upstream CI to pass

* Wait for the change to land on m-c

* Merge the PR

* If commits land directly on m-c we start the
  process above, but for mozilla central, using the last incoming
  merge as the start point.

### Disaster Recovery

* Miss a push to mozilla-inbound, or don't process it before it's
  merged to central.
 - OK if we see another push before the next merge to central. Maybe
   instead of using the last merge to central, record the last
   upstream-landed sync commit. But we have problems if we see the
   same commits on autoland and inbound. Or use the pushlog to
   recover.

* Changes don't rebase cleanly onto upstream.
 - Need to fix this up at some point. Best option is probably to
   start on the same revision as we are synced at and if a rebase
   fails, open the PR based on the current sync commit and fix it
   upstream. Then use *those* commits when reapplying onto master,
   during push rather than just the local ones. There is still a race
   condition there of course (the faster syncs are, the less common
   this will be).




## Push

* On a timer, check if new commits have landed upstream.

* For each commit, map to the PR that generated it, if any

* Check if we have a sync for the PR that is completed (or
  upstream). If so mark the corresponding commits as importable

* Find the last commit such that all earlier commits are either
  already imported or importable.

* For each merge commit of a PR that is an ancestor-or-self of the
  last importable commit, copy the upstream tree corresponding to that
  commit over to the tip of mozilla-inbound.

* Apply any local changes that have not yet upstreamed.

* Apply any metadata changes for the PR.

* Update the test manifest.

* Land the changes in mozilla central.

### Disaster recovery

* Commits with no corresponding PR
 - Continue like normal. Should consider an extra metadata update
   cycle in this case, but defering that for now on the assumption
   that such commits are probably mostly fixing minor lint errors, not
   changing test expectations, and are rare enough that we can fixup
   inbound if required.

* Error applying unlanded upstream commits onto inbound.
 - Manual fixup. Need to be able to resume the process once this fixup
   is complete.

* Subsequent changes invalidate metadata update.

 - Initially assume this is rare and can be dealt with as fixups after
   landing. If the problem persists then consider running a metadata
   update step after finalising the set of commits (although there is
   obvously still a race condition here since that takes finite time
   to run).

# General problems

* GitHub is down
  - Retry tasks. This blocks many things, so just retrying everything
    should be fine. Maybe pause if we think this is happening?
