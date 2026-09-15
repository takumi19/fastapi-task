from pathlib import Path
import time
from uuid import uuid4

from sqlalchemy import BigInteger, ForeignKey, Integer, String, Text, make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = 'users'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    username: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[int] = mapped_column(BigInteger, default=lambda: int(time.time()), nullable=False)


class Link(Base):
    __tablename__ = 'links'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    short_code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    original_url: Mapped[str] = mapped_column(String(2048), index=True, nullable=False)
    owner_id: Mapped[str | None] = mapped_column(ForeignKey('users.id'), nullable=True, index=True)
    created_at: Mapped[int] = mapped_column(BigInteger, default=lambda: int(time.time()), nullable=False)
    expires_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    clicks: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    last_accessed_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


def connect_database(url):
    parsed = make_url(url)
    if parsed.get_backend_name() == 'sqlite' and parsed.database and parsed.database != ':memory:':
        Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(url, pool_pre_ping=True)
    return engine, async_sessionmaker(engine, expire_on_commit=False)
