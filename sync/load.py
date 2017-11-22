import downstream
import landing
import upstream


def get_pr_sync(git_gecko, git_wpt, pr_id):
    sync = None
    sync = downstream.DownstreamSync.for_pr(git_gecko, git_wpt, pr_id)
    if not sync:
        sync = upstream.UpstreamSync.for_pr(git_gecko, git_wpt, pr_id)
    return sync


def get_bug_sync(git_gecko, git_wpt, bug_number):
    sync = None
    sync = landing.LandingSync.for_bug(git_gecko, git_wpt, bug_number)
    if not sync:
        sync = upstream.UpstreamSync.for_bug(git_gecko, git_wpt, bug_number)
    if not sync:
        sync = downstream.DownstreamSync.for_bug(git_gecko, git_wpt, bug_number)
    return sync


def get_syncs(git_gecko, git_wpt, sync_type, obj_id, status="*"):
    cls_types = {
        "downstream": downstream.DownstreamSync,
        "landing": landing.LandingSync,
        "upstream": upstream.UpstreamSync
    }
    cls = cls_types[sync_type]
    return cls.load_all(git_gecko, git_wpt, obj_id=obj_id, status=status)

