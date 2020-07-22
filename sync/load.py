from __future__ import absolute_import

from . import log
from .env import Environment

MYPY = False
if MYPY:
    from typing import Dict, Iterable, Optional, Set, Text
    from git.repo.base import Repo
    from sync.sync import SyncProcess

env = Environment()

logger = log.get_logger(__name__)


def get_pr_sync(git_gecko,  # type: Repo
                git_wpt,  # type: Repo
                pr_id,  # type: Text
                log=True,  # type: bool
                ):
    # type: (...) -> Optional[SyncProcess]
    from . import downstream
    from . import upstream

    sync = downstream.DownstreamSync.for_pr(git_gecko, git_wpt, pr_id)
    if not sync:
        sync = upstream.UpstreamSync.for_pr(git_gecko, git_wpt, pr_id)
    if log:
        if sync:
            logger.info("Got sync %r for PR %s" % (sync, pr_id))
        else:
            logger.info("No sync found for PR %s" % pr_id)
    return sync


def get_bug_sync(git_gecko,  # type: Repo
                 git_wpt,  # type: Repo
                 bug_number,  # type: Text
                 statuses=None  # type: Optional[Iterable[Text]]
                 ):
    # type: (...) -> Dict[Text, Set[SyncProcess]]
    from . import downstream
    from . import landing
    from . import upstream

    syncs = landing.LandingSync.for_bug(git_gecko, git_wpt, bug_number,
                                        statuses=statuses)
    if not syncs:
        syncs = upstream.UpstreamSync.for_bug(git_gecko, git_wpt, bug_number,
                                              statuses=statuses)
    if not syncs:
        syncs = downstream.DownstreamSync.for_bug(git_gecko, git_wpt, bug_number,
                                                  statuses=statuses)

    assert isinstance(syncs, dict)
    return syncs


def get_syncs(git_gecko,  # type: Repo
              git_wpt,  # type: Repo
              sync_type,  # type: Text
              obj_id,  # type: Text
              status=None,  # type: Optional[Text]
              seq_id=None  # type: Optional[Text]
              ):
    # type: (...) -> Set[SyncProcess]
    from . import downstream
    from . import landing
    from . import upstream

    cls_types = {
        u"downstream": downstream.DownstreamSync,
        u"landing": landing.LandingSync,
        u"upstream": upstream.UpstreamSync
    }
    cls = cls_types[sync_type]
    syncs = cls.load_by_obj(git_gecko, git_wpt, obj_id, seq_id=seq_id)
    if status:
        syncs = {sync for sync in syncs if sync.status == status}
    return syncs
