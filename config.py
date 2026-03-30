from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    # Telegram
    BOT_TOKEN: str
    
    # OpenRouter AI
    OPENROUTER_API_KEY: str
    OPENROUTER_MODEL: str = "nvidia/nemotron-3-nano-30b-a3b:free"
    
    # Database
    DATABASE_URL: str = "postgresql+asyncpg://user:password@localhost:5432/hh_tracker"
    
    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"
    
    # Proxy (MTProto)
    PROXY_URL: Optional[str] = None
    
    # HH API
    HH_API_URL: str = "https://api.hh.ru"
    HH_USER_AGENT: str = "HH-Tracker-Bot/1.0"
    
    # Scheduler
    CHECK_INTERVAL_MINUTES: int = 20
    MAX_VACANCIES: int = 150
    
    class Config:
        env_file = ".env"


settings = Settings()
