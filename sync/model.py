import enum
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy import Boolean, Column, DateTime, Enum, Integer, String, ForeignKey
from sqlalchemy.sql import func
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship, sessionmaker

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


class LandingStatus(enum.Enum):
    initial = 0
    have_commits = 1
    have_bug = 2
    applied_commits = 3
    complete = 4


class Sync(Base):
    __tablename__ = 'sync'

    id = Column(Integer, primary_key=True)
    bug = Column(Integer)
    pr_id = Column(Integer, ForeignKey('pull_request.id'))
    gecko_worktree = Column(String, unique=True)
    wpt_worktree = Column(String, unique=True)
    repository_id = Column(Integer, ForeignKey('repository.id'))
    source_id = Column(Integer, ForeignKey('branch.id'))
    # Only two allowed values 'upstream' and 'downstream'. Maybe should
    # use a different representation here
    direction = Column(Enum(SyncDirection), nullable=False)
    imported = Column(Boolean, default=False)

    modified = Column(DateTime(timezone=True), onupdate=func.now())

    closed = Column(Boolean, default=False)

    # Upstreaming only
    wpt_branch = Column(String, unique=True)

    pr = relationship("PullRequest", back_populates="sync", uselist=False)
    repository = relationship("Repository", uselist=False)
    source = relationship("Branch")
    gecko_commits = relationship("GeckoCommit")

    # Only for downstreaming
    try_pushes = relationship("TryPush")
    metadata_ready = Column(Boolean, default=False)


class Landing(Base):
    __tablename__ = "landing"

    id = Column(Integer, primary_key=True)
    head_commit_id = Column(Integer, ForeignKey('wpt_commit.id'), nullable=False)
    worktree = Column(String)
    bug = Column(String)

    status = Column(Enum(LandingStatus), default=LandingStatus.initial, nullable=False)

    head_commit = relationship("WptCommit", primaryjoin="Landing.head_commit_id==WptCommit.id",
                               foreign_keys=head_commit_id)
    wpt_commits = relationship("WptCommit", primaryjoin="Landing.id==WptCommit.landing_id")

    modified = Column(DateTime(timezone=True), onupdate=func.now())

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


class Repository(Base):
    __tablename__ = 'repository'

    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True)

    last_processed_commit_id = Column(Integer, ForeignKey('gecko_commit.id'))

    @classmethod
    def by_name(cls, session, name):
        return get(session, cls, name=name)


class Branch(Base):
    __tablename__ = 'branch'

    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True)


class GeckoCommit(Base):
    """Commits to gecko repositories"""
    __tablename__ = 'gecko_commit'

    id = Column(Integer, primary_key=True)
    rev = Column(String(40), unique=True)

    sync_id = Column(Integer, ForeignKey('sync.id'))
    sync = relationship("Sync")


class WptCommit(Base):
    """Commits to wpt repository"""
    __tablename__ = 'wpt_commit'

    id = Column(Integer, primary_key=True)
    rev = Column(String(40), unique=True)

    pr_id = Column(Integer, ForeignKey('pull_request.id'))
    pr = relationship("PullRequest")

    landing_id = Column(Integer, ForeignKey(Landing.id))
    landing = relationship("Landing",
                           back_populates="wpt_commits",
                           foreign_keys=landing_id)


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
    sync = relationship("Sync")


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
