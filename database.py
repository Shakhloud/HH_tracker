from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base
from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text, BigInteger
from datetime import datetime

from config import settings

Base = declarative_base()

engine = create_async_engine(settings.DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class User(Base):
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False)
    username = Column(String(255), nullable=True)
    first_name = Column(String(255), nullable=True)
    last_name = Column(String(255), nullable=True)
    
    # Resume analysis results
    resume_text = Column(Text, nullable=True)
    skills = Column(Text, nullable=True)  # JSON string of extracted skills
    experience_years = Column(Integer, nullable=True)
    desired_position = Column(String(500), nullable=True)
    location = Column(String(255), nullable=True)
    salary_expectation = Column(String(100), nullable=True)
    
    # Subscription status
    is_subscribed = Column(Boolean, default=False)
    
    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_check_at = Column(DateTime, nullable=True)


class Vacancy(Base):
    __tablename__ = "vacancies"
    
    id = Column(Integer, primary_key=True)
    hh_id = Column(String(50), unique=True, nullable=False)
    name = Column(String(500), nullable=False)
    employer_name = Column(String(500), nullable=True)
    description = Column(Text, nullable=True)
    url = Column(String(1000), nullable=False)
    salary_from = Column(Integer, nullable=True)
    salary_to = Column(Integer, nullable=True)
    salary_currency = Column(String(10), nullable=True)
    location = Column(String(255), nullable=True)
    experience = Column(String(100), nullable=True)
    employment_type = Column(String(100), nullable=True)
    schedule = Column(String(100), nullable=True)
    published_at = Column(DateTime, nullable=False)
    
    created_at = Column(DateTime, default=datetime.utcnow)


class UserVacancy(Base):
    __tablename__ = "user_vacancies"
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False)
    vacancy_id = Column(Integer, nullable=False)
    is_sent = Column(Boolean, default=False)
    sent_at = Column(DateTime, nullable=True)
    
    created_at = Column(DateTime, default=datetime.utcnow)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncSession:
    async with async_session() as session:
        yield session
