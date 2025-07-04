from __future__ import annotations
from . import log
from .env import Environment

from typing import Iterable, Mapping, Optional, TYPE_CHECKING
from git.repo.base import Repo

if TYPE_CHECKING:
    from sync.sync import SyncProcess
    from . import downstream
    from . import landing
    from . import upstream


env = Environment()

logger = log.get_logger(__name__)


def get_pr_sync(
    git_gecko: Repo,
    git_wpt: Repo,
    pr_id: int,
    log: bool = True,
) -> Optional[upstream.UpstreamSync | downstream.DownstreamSync]:
    from . import downstream
    from . import upstream

    sync: Optional[upstream.UpstreamSync | downstream.DownstreamSync]
    sync = downstream.DownstreamSync.for_pr(git_gecko, git_wpt, pr_id)
    if not sync:
        sync = upstream.UpstreamSync.for_pr(git_gecko, git_wpt, pr_id)
    if log:
        if sync:
            logger.info(f"Got sync {sync!r} for PR {pr_id}")
        else:
            logger.info("No sync found for PR %s" % pr_id)
    return sync


def get_bug_sync(
    git_gecko: Repo, git_wpt: Repo, bug_number: int, statuses: Iterable[str] | None = None
) -> Mapping[
    str, set[downstream.DownstreamSync] | set[landing.LandingSync] | set[upstream.UpstreamSync]
]:
    from . import downstream
    from . import landing
    from . import upstream

    syncs: Mapping[
        str, set[downstream.DownstreamSync] | set[landing.LandingSync] | set[upstream.UpstreamSync]
    ]
    syncs = landing.LandingSync.for_bug(
        git_gecko, git_wpt, bug_number, statuses=statuses, flat=False
    )
    if not syncs:
        syncs = upstream.UpstreamSync.for_bug(
            git_gecko, git_wpt, bug_number, statuses=statuses, flat=False
        )
    if not syncs:
        syncs = downstream.DownstreamSync.for_bug(
            git_gecko, git_wpt, bug_number, statuses=statuses, flat=False
        )

    return syncs


def get_syncs(
    git_gecko: Repo,
    git_wpt: Repo,
    sync_type: str,
    obj_id: int,
    status: str | None = None,
    seq_id: int | None = None,
) -> set[SyncProcess]:
    from . import downstream
    from . import landing
    from . import upstream

    cls_types = {
        "downstream": downstream.DownstreamSync,
        "landing": landing.LandingSync,
        "upstream": upstream.UpstreamSync,
    }
    cls = cls_types[sync_type]
    syncs = cls.load_by_obj(git_gecko, git_wpt, obj_id, seq_id=seq_id)
    if status:
        syncs = {sync for sync in syncs if sync.status == status}
    return syncs
