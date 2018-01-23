# wptsync User Guide

This document is intended to document common scenarios encountered
with the wptsync service and explain how to deal with each one.

## Notes

Many of the workflows documented below involve running `wptsync`
subcommands. These may appear to hang shortly after starting. This is
because accessing to the git repositories are protected by a coarse
lock, so only one task may run at a time. This means that `wptsync`
commands have to wait for whatever the main sync process is currently
running to end before they are able to operate.

## Logs

The sync logs are stored in `/app/workspace/logs/sync.log`. The
scheduling of periodic tasks is logged in
`/app/workspace/logs/celerybeat.log`, but note that the actual
operations performed will be in the `sync.log` file.

## Examining Status

To see the status of all in-progress syncs run:

```
wptsync list
```

To see more detail on the current progress of a specific sync, run

```
wptsync detail <subtype> <id>
```

where `<subtype>` is the sync type i.e. `upstream`, `downstream` or
`landing` and `obj_id` is the bug number for upstream or landing syncs
and the PR number for downstream syncs.

### Using git

In some cases it may be desirable to directly examine the stored state
for each sync. This state is all stored in the git repository and may
be examined using normal git commands. In order to do this it's
necessary to understand the storage structure.

Metadata about syncs is stored in commit objects. Each sync
corresponds to a separate history in the git repository. The syncs are
referred to by refs under the `refs/syncs/` namespace, with the
standard format

```
refs/syncs/<type>/<subtype>/<status>/<id>[/<seq_id>]
```

For example a downstream sync for pr `1234` would have a corresponding
ref:

```
refs/syncs/sync/downstream/open/1234
```

Try pushes have their metadata stored in the same way, for example:

```
refs/syncs/try/downstream/open/1234/0
```

In addition syncs usually have a corresponding ref pointing to the
corresponding gecko commits. In this case that would be a ref of the
form:

```
refs/heads/sync/downstream/open/1234
```

In order to view the metadata for a commit one can use `git show` i.e.

```
git show refs/syncs/sync/downstream/open/1234/0
```

In addition `git log` can be used to see the history of changes to the
commit metadata, which may be useful when debugging issues. To examine
which refs exist, use `git for-each-ref`. For example to look for all
the open downstream syncs, one can use:

```
git for-each-ref 'refs/syncs/sync/downstream/open/*
```

### Using GitHub

All upstream syncs are expected to have the label
`mozilla:gecko-sync`. These can be viewed on GitHub with the query:

[https://github.com/w3c/web-platform-tests/pulls?q=is%3Apr+is%3Aopen+label%3Amozilla%3Agecko-sync](https://github.com/w3c/web-platform-tests/pulls?q=is%3Apr+is%3Aopen+label%3Amozilla%3Agecko-sync)

# Deleting Syncs

In some cases if a sync gets into a bad state the easiest option is to
delete it and start again. In this case there are two options. One is
to use the `wptsync delete` command as follows:

```
wptsync delete <subtype> <obj_id>
```

e.g.

```
wptsync delete downstream 1234
```

If this doesn't work (or, currently, for deleting try pushes), the
other option is to delete the corresponding git reference directly
e.g.

```
git update-ref -d refs/syncs/try/downstream/1234/0
```

# Merge Conflicts

When local changes conflict with those upstream, or vice-versa, we get
a merge conflict, which prevents creating the sync branch. This
requires manual fixup to resolve the merge conflict.

For VCS operations, the merge creates a worktree with a location like
```
/app/workspace/work/<repo>/<subtype>/<id>
```

For example for the downstream sync for PR 1234, that would be

```
/app/workspace/work/gecko/downstream/1234
```

When a patch fails to apply, it will leave two files in the root of
the destination work tree `<sha1>.diff` and `<sha1>.message`. **It is
important the when the conflict is resolved the corresponding commit
has the commit message given in `<sha1>.message`**. If this doesn't
happen the sync process will become confused about which commits are
applied.

To attempt reapplying a diff to gecko that comes from upstream, use a
git command like

```
git apply --reject --directory=testing/web-platform/tests <sha1.diff>
```

To attempt to reapply a diff to upstream that comes from gecko, use a
git command like

```
git apply --reject -p4 <sha1.diff>
```

When the patch is successfully applied to the index, the commit must be
created using the command

```
git commit -F <sha1.message>
```

After applying the commit, the sync can be restarted using the command

```
wptsync pr
```

for downstream syncs, and

```
wptsync bug
```

for upstream or landing syncs. If these command are run in the
worktree for the sync they should auto-detect the correct bug or pr
id, otherwise this must be provided as a positional command line
argument.

# Manually Triggering Updates

In some situations, a sync may be stalled because data is missing
e.g. an event was incorrectly processed. Depending on the exact
situation there are various commands that are appropriate.

## Updating from GitHub

If the sync missed GitHub events, the command

```
wptsync update
```

will read all the PR data on GitHub and attempt to update the local
state to match.

## Updating from Taskcluster

If events from Taskcluster were missed so that syncs are missing, the
latest state can be pulled in using

```
wptsync update-tasks

```

## Updating Individual Syncs

Individual syncs can be updated uing either the

```
wptsync pr <pr_id>
```

Or

```
wptsync bug <bug_id>
```

commands. Note that these generally don't check taskcluster.

# Running the landing code

If the landing code has to be run manually, this can be done using

```
wptsync landing
```

# Cleanup / Dealing with Low Disk Space

The periodic cleanup task is expected to run once an hour. This
deletes worktrees for completed syncs, and worktrees for open syncs
that have not been updated for a long time. It's possible to run the
cleanup code on request using

```
wptsync cleanup
```

If for some reason this is inefficient, it should be safe to delete
worktree directories manually as long as they are not actively being
used (they are simply recreated on demand). The best way to do this is
to stop the sync service and delete the required directories. In order
to understand where disk space is being used in case of low storage,
it's useful to use the command

```
du -h -d4 /app/workspace
```

which will summarise the disk usage up to four levels deep in the
directory tree.
