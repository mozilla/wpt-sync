from . import log
from .env import Environment

MYPY = False
if MYPY:
    from typing import Dict, Iterable, Optional, Set, Text
    from git.repo.base import Repo
    from sync.sync import SyncProcess

env = Environment()

logger = log.get_logger(__name__)


def get_pr_sync(git_gecko: Repo,
                git_wpt: Repo,
                pr_id: int,
                log: bool = True,
                ) -> Optional[SyncProcess]:
    from . import downstream
    from . import upstream

    sync = downstream.DownstreamSync.for_pr(git_gecko, git_wpt, pr_id)
    if not sync:
        sync = upstream.UpstreamSync.for_pr(git_gecko, git_wpt, pr_id)
    if log:
        if sync:
            logger.info(f"Got sync {sync!r} for PR {pr_id}")
        else:
            logger.info("No sync found for PR %s" % pr_id)
    return sync


def get_bug_sync(git_gecko: Repo,
                 git_wpt: Repo,
                 bug_number: int,
                 statuses: Optional[Iterable[Text]] = None
                 ) -> Dict[Text, Set[SyncProcess]]:
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


def get_syncs(git_gecko: Repo,
              git_wpt: Repo,
              sync_type: Text,
              obj_id: int,
              status: Optional[Text] = None,
              seq_id: Optional[int] = None
              ) -> Set[SyncProcess]:
    from . import downstream
    from . import landing
    from . import upstream

    cls_types = {
        "downstream": downstream.DownstreamSync,
        "landing": landing.LandingSync,
        "upstream": upstream.UpstreamSync
    }
    cls = cls_types[sync_type]
    syncs = cls.load_by_obj(git_gecko, git_wpt, obj_id, seq_id=seq_id)
    if status:
        syncs = {sync for sync in syncs if sync.status == status}
    return syncs
