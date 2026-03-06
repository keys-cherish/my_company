"""All ORM models for the Business Empire game."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# ---------- Users ----------

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, index=True)
    tg_name: Mapped[str] = mapped_column(String(128), nullable=False)
    self_points: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    reputation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)  # optimistic lock
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())

    companies: Mapped[list[Company]] = relationship("Company", back_populates="owner")
    shares: Mapped[list[Shareholder]] = relationship("Shareholder", back_populates="user")


# ---------- Companies ----------

class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    company_type: Mapped[str] = mapped_column(String(32), nullable=False, default="tech")
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    cp_points: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    daily_revenue: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    level: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    employee_count: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())

    owner: Mapped[User] = relationship("User", back_populates="companies")
    shareholders: Mapped[list[Shareholder]] = relationship("Shareholder", back_populates="company")
    research: Mapped[list[ResearchProgress]] = relationship("ResearchProgress", back_populates="company")
    products: Mapped[list[Product]] = relationship("Product", back_populates="company")
    roadshows: Mapped[list[Roadshow]] = relationship("Roadshow", back_populates="company")
    real_estates: Mapped[list[RealEstate]] = relationship("RealEstate", back_populates="company")
    daily_reports: Mapped[list[DailyReport]] = relationship("DailyReport", back_populates="company")


# ---------- Company Operations ----------

class CompanyOperationProfile(Base):
    __tablename__ = "company_operation_profiles"

    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id"),
        primary_key=True,
        nullable=False,
        index=True,
    )
    work_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=8)
    office_level: Mapped[str] = mapped_column(String(16), nullable=False, default="standard")
    training_level: Mapped[str] = mapped_column(String(16), nullable=False, default="none")
    training_expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    insurance_level: Mapped[str] = mapped_column(String(16), nullable=False, default="basic")
    culture: Mapped[int] = mapped_column(Integer, nullable=False, default=50)  # 0-100
    ethics: Mapped[int] = mapped_column(Integer, nullable=False, default=60)  # 0-100
    regulation_pressure: Mapped[int] = mapped_column(Integer, nullable=False, default=40)  # 0-100
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
    )


# ---------- Shareholders ----------

class Shareholder(Base):
    __tablename__ = "shareholders"
    __table_args__ = (UniqueConstraint("company_id", "user_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    shares: Mapped[float] = mapped_column(Float, nullable=False, default=0)  # percentage 0-100
    invested_amount: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    joined_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())

    company: Mapped[Company] = relationship("Company", back_populates="shareholders")
    user: Mapped[User] = relationship("User", back_populates="shares")


# ---------- Research ----------

class ResearchProgress(Base):
    __tablename__ = "research_progress"
    __table_args__ = (UniqueConstraint("company_id", "tech_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), nullable=False, index=True)
    tech_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="researching")  # researching / completed
    started_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    company: Mapped[Company] = relationship("Company", back_populates="research")


# ---------- Products ----------

class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    tech_id: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    daily_income: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    quality: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    assigned_employees: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())

    company: Mapped[Company] = relationship("Company", back_populates="products")


# ---------- Roadshows ----------

class Roadshow(Base):
    __tablename__ = "roadshows"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), nullable=False, index=True)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    result: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    bonus: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    reputation_gained: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())

    company: Mapped[Company] = relationship("Company", back_populates="roadshows")


# ---------- Cooperations ----------

class Cooperation(Base):
    __tablename__ = "cooperations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_a_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), nullable=False, index=True)
    company_b_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), nullable=False, index=True)
    type: Mapped[str] = mapped_column(String(32), nullable=False, default="standard")
    bonus_multiplier: Mapped[float] = mapped_column(Float, nullable=False, default=0.10)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())


# ---------- Real Estates ----------

class RealEstate(Base):
    __tablename__ = "real_estates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), nullable=False, index=True)
    building_type: Mapped[str] = mapped_column(String(64), nullable=False)
    level: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    daily_dividend: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    purchase_price: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    purchased_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())

    company: Mapped[Company] = relationship("Company", back_populates="real_estates")


# ---------- Daily Reports ----------

class DailyReport(Base):
    __tablename__ = "daily_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), nullable=False, index=True)
    date: Mapped[str] = mapped_column(String(10), nullable=False)  # YYYY-MM-DD
    product_income: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    employee_income: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    cooperation_bonus: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    realestate_income: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    reputation_buff_income: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    total_income: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    operating_cost: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    dividend_paid: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    company: Mapped[Company] = relationship("Company", back_populates="daily_reports")


# ---------- Weekly Quests ----------

class WeeklyTask(Base):
    __tablename__ = "weekly_tasks"
    __table_args__ = (UniqueConstraint("user_id", "quest_id", "week_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    quest_id: Mapped[str] = mapped_column(String(64), nullable=False)
    week_key: Mapped[str] = mapped_column(String(10), nullable=False)  # "2026-W09"
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    target: Mapped[int] = mapped_column(Integer, nullable=False)
    completed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # 0/1
    rewarded: Mapped[int] = mapped_column(Integer, nullable=False, default=0)   # 0/1
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())
