import log

from env import Environment

env = Environment()

logger = log.get_logger(__name__)


def get_pr_sync(git_gecko, git_wpt, pr_id):
    import downstream
    import upstream

    sync = None
    sync = downstream.DownstreamSync.for_pr(git_gecko, git_wpt, pr_id)
    if not sync:
        sync = upstream.UpstreamSync.for_pr(git_gecko, git_wpt, pr_id)
    if sync:
        logger.info("Got sync %r for PR %s" % (sync, pr_id))
    else:
        logger.info("No sync found for PR %s" % pr_id)
    return sync


def get_bug_sync(git_gecko, git_wpt, bug_number, statuses=None):
    import downstream
    import landing
    import upstream

    syncs = None
    syncs = landing.LandingSync.for_bug(git_gecko, git_wpt, bug_number,
                                        statuses=statuses)
    if not syncs:
        syncs = upstream.UpstreamSync.for_bug(git_gecko, git_wpt, bug_number,
                                              statuses=statuses)
    if not syncs:
        syncs = downstream.DownstreamSync.for_bug(git_gecko, git_wpt, bug_number,
                                                  statuses=statuses)
    if syncs:
        all_syncs = []
        for item in syncs.itervalues():
            all_syncs.extend(item)
        logger.info("Got syncs %r for bug %s" % (all_syncs, bug_number))
    else:
        logger.info("No sync found for bug %s" % bug_number)
    return syncs


def get_syncs(git_gecko, git_wpt, sync_type, obj_id, status="*"):
    import downstream
    import landing
    import upstream

    cls_types = {
        "downstream": downstream.DownstreamSync,
        "landing": landing.LandingSync,
        "upstream": upstream.UpstreamSync
    }
    cls = cls_types[sync_type]
    return cls.load_all(git_gecko, git_wpt, obj_id=obj_id, status=status)
