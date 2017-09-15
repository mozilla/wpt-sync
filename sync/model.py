import enum
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy import Boolean, Column, DateTime, Enum, ForeignKey, Integer, String, Table
from sqlalchemy.sql import func
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship, sessionmaker, with_polymorphic

import settings

Base = declarative_base()
Session = sessionmaker()
engine = None


class SyncDirection(enum.Enum):
    upstream = 1
    downstream = 2


class TryKind(enum.Enum):
    # First narrow try push has been sent
    initial = 1
    # Stability try push has been sent
    stability = 2


class TryResult(enum.Enum):
    greenish = 1
    infra = 2
    orange = 3


class UpstreamSyncStatus(enum.Enum):
    initial = 0
    commits_applied = 1
    have_pr = 2
    status_passed = 3
    complete = 4
    aborted = 5


class DownstreamSyncStatus(enum.Enum):
    initial = 0
    commits_applied = 1
    have_pr = 2
    status_passed = 3
    complete = 4
    aborted = 5


class LandingStatus(enum.Enum):
    initial = 0
    have_commits = 1
    have_bug = 2
    have_syncs = 3
    have_worktree = 4
    applied_commits = 5
    complete = 6


class PullRequest(Base):
    """Upstream Pull Requests"""
    __tablename__ = 'pull_request'

    id = Column(Integer, primary_key=True)

    # If the upstream PR has been merged
    merged = Column(Boolean, default=False)

    title = Column(String)  # TODO: fill this in
    commits = relationship("WptCommit")
    sync = relationship("Sync", back_populates="pr", uselist=False)

    @classmethod
    def update_from_github(cls, session, data):
        instance, _ = get_or_create(session, cls, id=data["number"])
        instance.title = data["title"]
        instance.merged = data["merged"]


class WptCommit(Base):
    """Commits to wpt repository"""
    __tablename__ = 'wpt_commit'

    id = Column(Integer, primary_key=True)
    rev = Column(String(40), unique=True)

    pr_id = Column(Integer, ForeignKey('pull_request.id'))
    pr = relationship("PullRequest")

    landing_id = Column(Integer, ForeignKey("landing.id"))
    landing = relationship("Landing",
                           back_populates="wpt_commits",
                           foreign_keys=landing_id)


class TryPush(Base):
    __tablename__ = 'try_push'

    id = Column(Integer, primary_key=True)
    # hg rev on try
    rev = Column(String(40), unique=True)
    taskgroup_id = Column(String(22), unique=True)
    complete = Column(Boolean, default=False)
    result = Column(Enum(TryResult))
    kind = Column(Enum(TryKind), nullable=False)
    sync_id = Column(Integer, ForeignKey('sync.id'))
    sync = relationship("DownstreamSync")
    # PR has been updated sync try push started
    stale = Column(Boolean, default=False)


class Repository(Base):
    __tablename__ = 'repository'

    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True)

    last_processed_commit_id = Column(Integer, ForeignKey('gecko_commit.id'))

    @classmethod
    def by_name(cls, session, name):
        return get(session, cls, name=name)

landing_syncs_applied = Table('landing_syncs_applied', Base.metadata,
    Column('landing_id', Integer, ForeignKey('landing.id')),
    Column('sync_upstream_id', Integer, ForeignKey('sync_upstream.id'))
)


class Landing(Base):
    __tablename__ = "landing"

    id = Column(Integer, primary_key=True)
    head_commit_id = Column(Integer, ForeignKey('wpt_commit.id'))
    worktree = Column(String)
    bug = Column(String)

    status = Column(Enum(LandingStatus), default=LandingStatus.initial, nullable=False)

    head_commit = relationship("WptCommit", primaryjoin="Landing.head_commit_id==WptCommit.id",
                               foreign_keys=head_commit_id)
    wpt_commits = relationship("WptCommit", primaryjoin="Landing.id==WptCommit.landing_id")

    modified = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now())

    syncs_applied = relationship("UpstreamSync", secondary=landing_syncs_applied)

    @classmethod
    def current(cls, session):
        landings = session.query(cls).filter(cls.status != LandingStatus.complete).all()
        assert len(landings) <= 1
        if len(landings) == 0:
            return None
        return landings[0]

    @classmethod
    def previous(cls, session):
        return (session.query(cls)
                .filter(cls.status == LandingStatus.complete)
                .order_by(cls.id.desc())
                .first())


class GeckoCommit(Base):
    """Commits to gecko repositories"""
    __tablename__ = 'gecko_commit'

    id = Column(Integer, primary_key=True)
    rev = Column(String(40), unique=True)

    sync_id = Column(Integer, ForeignKey('sync.id'))
    sync = relationship("UpstreamSync")


class Sync(Base):
    __tablename__ = 'sync'

    id = Column(Integer, primary_key=True)
    bug = Column(Integer)
    pr_id = Column(Integer, ForeignKey('pull_request.id'))
    modified = Column(DateTime(timezone=True), onupdate=func.now())
    gecko_worktree = Column(String, unique=True)
    wpt_worktree = Column(String, unique=True)
    repository_id = Column(Integer, ForeignKey('repository.id'))
    human_needed = Column(Boolean, default=False)

    # Only two allowed values 'upstream' and 'downstream'. Maybe should
    # use a different representation here
    direction = Column(Enum(SyncDirection), nullable=False)

    pr = relationship("PullRequest", back_populates="sync", uselist=False)
    repository = relationship("Repository", uselist=False)

    __mapper_args__ = {
        'polymorphic_identity': 'sync',
        'polymorphic_on': direction
    }


class DownstreamSync(Sync):
    __tablename__ = 'sync_downstream'

    id = Column(Integer, ForeignKey('sync.id'), primary_key=True)

    status = Column(Enum(DownstreamSyncStatus))
    try_pushes = relationship("TryPush")
    metadata_ready = Column(Boolean, default=False)

    __mapper_args__ = {
        'polymorphic_identity': SyncDirection.downstream
    }


class UpstreamSync(Sync):
    __tablename__ = 'sync_upstream'

    id = Column(Integer, ForeignKey('sync.id'), primary_key=True)

    status = Column(Enum(UpstreamSyncStatus))
    wpt_branch = Column(String, unique=True)
    gecko_commits = relationship("GeckoCommit")

    __mapper_args__ = {
        'polymorphic_identity': SyncDirection.upstream
    }

    # Upstreaming only

    @classmethod
    def unlanded(cls, session, exclude_repos=["autoland"]):
        return (session.query(cls)
                .join(Repository)
                .filter(~Repository.name.in_(exclude_repos),
                        ~UpstreamSync.status.in_((UpstreamSyncStatus.complete,
                                                  UpstreamSyncStatus.aborted)))
                .order_by(UpstreamSync.id.asc()))


SyncSubclass = with_polymorphic(Sync, [DownstreamSync, UpstreamSync])


def configure(config):
    global engine
    if engine is not None:
        return
    engine = create_engine(config["database"]["url"],
                           echo=config["database"]["echo"])
    Session.configure(bind=engine)


def create():
    assert engine is not None
    Base.metadata.create_all(engine)


def drop():
    assert engine is not None
    Base.metadata.drop_all(engine)


def session():
    return Session()


def get(session, model, **kwargs):
    return session.query(model).filter_by(**kwargs).first()


def get_or_create(session, model, defaults=None, **kwargs):
    instance = session.query(model).filter_by(**kwargs).first()

    if instance:
        created = False
    else:
        kwargs.update(defaults or {})
        instance = model(**kwargs)
        session.add(instance)
        created = True

    return instance, created


@contextmanager
def session_scope(session):
    """Provide a transactional scope around a series of operations."""
    try:
        yield session
        session.commit()
    except:
        session.rollback()
        raise


if __name__ == "__main__":
    config = settings.load()
    configure(config)
    create()
