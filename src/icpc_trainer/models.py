from __future__ import annotations

from datetime import datetime
from enum import Enum

from sqlalchemy import DateTime, Enum as SQLEnum, Float, ForeignKey, Integer, String, Text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

DEFAULT_DATABASE_URL = "sqlite+aiosqlite:///./trainer.db"


class Base(DeclarativeBase):
    pass


class AttemptStatus(str, Enum):
    PASS = "Pass"
    FAIL = "Fail"


class Problem(Base):
    __tablename__ = "problems"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    contest_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    html_content: Mapped[str] = mapped_column(Text, nullable=False)

    attempts: Mapped[list[Attempt]] = relationship(
        back_populates="problem",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    fsrs_state: Mapped[FSRSState | None] = relationship(
        back_populates="problem",
        cascade="all, delete-orphan",
        uselist=False,
        lazy="selectin",
    )


class Attempt(Base):
    __tablename__ = "attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    problem_id: Mapped[int] = mapped_column(ForeignKey("problems.id"), nullable=False, index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    status: Mapped[AttemptStatus] = mapped_column(SQLEnum(AttemptStatus), nullable=False)
    duration: Mapped[int] = mapped_column(Integer, nullable=False)

    problem: Mapped[Problem] = relationship(back_populates="attempts")


class FSRSState(Base):
    __tablename__ = "fsrs_states"

    problem_id: Mapped[int] = mapped_column(
        ForeignKey("problems.id"),
        primary_key=True,
    )
    stability: Mapped[float] = mapped_column(Float, nullable=False)
    difficulty: Mapped[float] = mapped_column(Float, nullable=False)
    last_reviewed: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    next_review_date: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)

    problem: Mapped[Problem] = relationship(back_populates="fsrs_state")


def create_async_sqlite_engine(database_url: str = DEFAULT_DATABASE_URL) -> AsyncEngine:
    return create_async_engine(database_url, echo=False)


def create_session_maker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_db(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
