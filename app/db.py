"""Short transactions; candidate data belongs to one explicit local owner."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import JSON, Boolean, ForeignKey, String, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def now() -> str:
    return datetime.now(UTC).isoformat()


def new_id() -> str:
    return uuid4().hex


class Base(DeclarativeBase):
    pass


class Profile(Base):
    __tablename__ = "profiles"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    owner: Mapped[str] = mapped_column(String, index=True)
    data: Mapped[dict] = mapped_column(JSON)
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    created: Mapped[str] = mapped_column(String, default=now)


class Scan(Base):
    __tablename__ = "scans"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    owner: Mapped[str] = mapped_column(String, index=True)
    profile_id: Mapped[str] = mapped_column(ForeignKey("profiles.id"))
    mode: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String, default="queued")
    progress: Mapped[str] = mapped_column(String, default="Queued")
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    created: Mapped[str] = mapped_column(String, default=now)


class Action(Base):
    __tablename__ = "actions"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    owner: Mapped[str] = mapped_column(String, index=True)
    profile_id: Mapped[str] = mapped_column(ForeignKey("profiles.id"))
    scan_id: Mapped[str] = mapped_column(ForeignKey("scans.id"))
    version: Mapped[int] = mapped_column(default=1)
    payload: Mapped[dict] = mapped_column(JSON)
    payload_hash: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String, default="proposed")
    expires: Mapped[str | None] = mapped_column(String, nullable=True)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    created: Mapped[str] = mapped_column(String, default=now)


class Connection(Base):
    __tablename__ = "connections"
    owner: Mapped[str] = mapped_column(String, primary_key=True)
    account: Mapped[str] = mapped_column(String)
    encrypted_token: Mapped[str] = mapped_column(String)
    scopes: Mapped[list] = mapped_column(JSON)
    consent: Mapped[bool] = mapped_column(Boolean, default=False)


class Activity(Base):
    __tablename__ = "activity"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    owner: Mapped[str] = mapped_column(String, index=True)
    event: Mapped[str] = mapped_column(String)
    reference: Mapped[str] = mapped_column(String, default="")
    created: Mapped[str] = mapped_column(String, default=now)


class OpportunityStatus(Base):
    __tablename__ = "opportunity_status"
    previous_state: Mapped[str | None] = mapped_column(String, nullable=True)
    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner: Mapped[str] = mapped_column(String, index=True)
    state: Mapped[str] = mapped_column(String)
    created: Mapped[str] = mapped_column(String, default=now)


class JobSourceCache(Base):
    __tablename__ = "job_source_cache"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    updated: Mapped[str] = mapped_column(String, default=now)
    retry_after: Mapped[str | None] = mapped_column(String, nullable=True)


class JobSourceUsage(Base):
    __tablename__ = "job_source_usage"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    requests: Mapped[int] = mapped_column(default=0)


class Store:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(
            "sqlite:///" + str(directory / "careerpilot.db"),
            connect_args={"check_same_thread": False, "timeout": 10},
        )

        @event.listens_for(self.engine, "connect")
        def sqlite_settings(connection, record):
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")

        self.session = sessionmaker(self.engine, expire_on_commit=False)

    def migrate(self):
        from alembic import command
        from alembic.config import Config

        root = Path(__file__).resolve().parents[1]
        config = Config(str(root / "alembic.ini"))
        config.set_main_option("script_location", str(root / "migrations"))
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")

    def activity(self, owner: str, event: str, reference: str = ""):
        with self.session.begin() as session:
            session.add(Activity(owner=owner, event=event, reference=reference))

    def owned(self, model, identifier: str, owner: str):
        from app.errors import AppError

        with self.session() as session:
            row = session.get(model, identifier)
            if not row or row.owner != owner:
                raise AppError("not_found", "Resource not found.", 404)
            return row

    def latest_profile(self, owner: str):
        from sqlalchemy import select

        with self.session() as session:
            return session.scalar(
                select(Profile)
                .where(Profile.owner == owner, Profile.confirmed.is_(True))
                .order_by(Profile.created.desc())
                .limit(1)
            )
