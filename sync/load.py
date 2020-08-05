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
                pr_id,  # type: int
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
                 bug_number,  # type: int
                 statuses=None  # type: Optional[Iterable[Text]]
                 ):
    # type: (...) -> Dict[Text, Set[SyncProcess]]
    from . import downstream
    from . import landing
    from . import upstream

    syncs = landing.LandingSync.for_bug(git_gecko,
                                        git_wpt,
                                        bug_number,
                                        statuses=statuses,
                                        flat=False)
    if not syncs:
        syncs = upstream.UpstreamSync.for_bug(git_gecko,
                                              git_wpt,
                                              bug_number,
                                              statuses=statuses,
                                              flat=False)
    if not syncs:
        syncs = downstream.DownstreamSync.for_bug(git_gecko,
                                                  git_wpt,
                                                  bug_number,
                                                  statuses=statuses,
                                                  flat=False)

    return syncs


def get_syncs(git_gecko,  # type: Repo
              git_wpt,  # type: Repo
              sync_type,  # type: Text
              obj_id,  # type: int
              status=None,  # type: Optional[Text]
              seq_id=None  # type: Optional[int]
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
