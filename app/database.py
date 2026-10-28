from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import WIB


class Base(DeclarativeBase):
    pass


def make_engine(database_url: str) -> Engine:
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    return create_engine(database_url, connect_args=connect_args, pool_pre_ping=True)


def make_session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, expire_on_commit=False)


# Waktu disimpan di DB sebagai UTC tanpa zona (aman untuk SQLite & MySQL),
# lalu dikonversi ke WIB saat keluar lewat API / MQTT.
def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_db_time(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def from_db_time(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc).astimezone(WIB)
