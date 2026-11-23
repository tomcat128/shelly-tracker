"""Database models (SQLAlchemy) - works with SQLite, MySQL/MariaDB and PostgreSQL."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


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
    __table_args__ = (
        UniqueConstraint("device_id", "ts", name="uq_reading_device_ts"),
        Index("ix_reading_device_ts", "device_id", "ts"),
    )

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
    __table_args__ = (Index("ix_measurement_metric", "metric"),)

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


def init_db(engine: Engine) -> sessionmaker:
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)
