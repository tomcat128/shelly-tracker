"""Database models (SQLAlchemy) - works with SQLite, MySQL/MariaDB and PostgreSQL."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    inspect,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

log = logging.getLogger(__name__)


def utcnow() -> datetime:
    """Naive UTC datetime (all timestamps in the DB are UTC)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    gen: Mapped[str | None] = mapped_column(String(16), nullable=True)
    online: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Reading(Base):
    __tablename__ = "readings"
    # The unique constraint doubles as the index for all time range queries.
    __table_args__ = (UniqueConstraint("device_id", "ts", name="uq_reading_device_ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(64), ForeignKey("devices.id"))
    ts: Mapped[datetime] = mapped_column(DateTime)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    online: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    values_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    raw: Mapped[str | None] = mapped_column(Text, nullable=True)

    measurements: Mapped[list["Measurement"]] = relationship(
        back_populates="reading", cascade="all, delete-orphan"
    )


class Measurement(Base):
    __tablename__ = "measurements"
    # Lookups are always by reading_id; WITHOUT ROWID stores rows directly in the primary
    # key b-tree (SQLite) instead of a table plus a separate PK index (~60% smaller).
    __table_args__ = ({"sqlite_with_rowid": False},)

    reading_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("readings.id", ondelete="CASCADE"), primary_key=True
    )
    metric: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[float] = mapped_column(Float)

    reading: Mapped[Reading] = relationship(back_populates="measurements")


def make_engine(url: str) -> Engine:
    kwargs: dict = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        db_path = url.split("///", 1)[-1]
        if db_path and db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(url, **kwargs)

    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    return engine


class HourlyStat(Base):
    """Hourly statistics that replace minute data older than RAW_RETENTION_MONTHS."""

    __tablename__ = "hourly_stats"
    __table_args__ = ({"sqlite_with_rowid": False},)

    device_id: Mapped[str] = mapped_column(String(64), ForeignKey("devices.id"), primary_key=True)
    hour: Mapped[datetime] = mapped_column(DateTime, primary_key=True)  # start of hour, UTC
    metric: Mapped[str] = mapped_column(String(128), primary_key=True)
    count: Mapped[int] = mapped_column(Integer)
    sum: Mapped[float] = mapped_column(Float)
    min: Mapped[float] = mapped_column(Float)
    max: Mapped[float] = mapped_column(Float)


# Indexes of earlier versions that are no longer needed.
_OBSOLETE_INDEXES = [("measurements", "ix_measurement_metric"), ("readings", "ix_reading_device_ts")]


def migrate(engine: Engine) -> None:
    """Upgrade databases created by earlier versions (idempotent)."""
    dialect = engine.dialect.name
    insp = inspect(engine)
    changed = False
    with engine.begin() as conn:
        for table, index in _OBSOLETE_INDEXES:
            if index in {i["name"] for i in insp.get_indexes(table)}:
                log.info("Migration: dropping unused index %s", index)
                on = f" ON {table}" if dialect == "mysql" else ""
                conn.execute(text(f"DROP INDEX {index}{on}"))
                changed = True

        if dialect == "sqlite":
            ddl = conn.execute(
                text("SELECT sql FROM sqlite_master WHERE type='table' AND name='measurements'")
            ).scalar()
            if ddl and "WITHOUT ROWID" not in ddl.upper():
                count = conn.execute(text("SELECT count(*) FROM measurements")).scalar()
                log.info("Migration: rebuilding measurements as WITHOUT ROWID (%d rows) ...", count)
                conn.execute(text("ALTER TABLE measurements RENAME TO measurements_old"))
                Measurement.__table__.create(conn)
                conn.execute(text(
                    "INSERT INTO measurements (reading_id, metric, value) "
                    "SELECT reading_id, metric, value FROM measurements_old ORDER BY reading_id, metric"
                ))
                conn.execute(text("DROP TABLE measurements_old"))
                changed = True

    if changed and dialect == "sqlite":
        log.info("Migration: VACUUM to release disk space ...")
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.exec_driver_sql("VACUUM")
        log.info("Migration done")


def init_db(engine: Engine) -> sessionmaker:
    Base.metadata.create_all(engine)
    migrate(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)
