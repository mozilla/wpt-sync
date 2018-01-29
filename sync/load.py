import downstream
import landing
import log
import upstream

from env import Environment

env = Environment()

logger = log.get_logger(__name__)


def get_pr_sync(git_gecko, git_wpt, pr_id):
    sync = None
    sync = downstream.DownstreamSync.for_pr(git_gecko, git_wpt, pr_id)
    if not sync:
        sync = upstream.UpstreamSync.for_pr(git_gecko, git_wpt, pr_id)
    if sync:
        logger.info("Got sync %r for PR %s" % (sync, pr_id))
    else:
        logger.info("No sync found for PR %s" % pr_id)
    return sync


def get_bug_sync(git_gecko, git_wpt, bug_number):
    sync = None
    sync = landing.LandingSync.for_bug(git_gecko, git_wpt, bug_number)
    if not sync:
        sync = upstream.UpstreamSync.for_bug(git_gecko, git_wpt, bug_number)
    if not sync:
        sync = downstream.DownstreamSync.for_bug(git_gecko, git_wpt, bug_number)
    if sync:
        logger.info("Got sync %r for bug %s" % (sync, bug_number))
    else:
        logger.info("No sync found for bug %s" % bug_number)
    return sync


def get_syncs(git_gecko, git_wpt, sync_type, obj_id, status="*"):
    cls_types = {
        "downstream": downstream.DownstreamSync,
        "landing": landing.LandingSync,
        "upstream": upstream.UpstreamSync
    }
    cls = cls_types[sync_type]
    return cls.load_all(git_gecko, git_wpt, obj_id=obj_id, status=status)
