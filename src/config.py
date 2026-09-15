from dataclasses import dataclass
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    database_url: str
    redis_url: str
    secret_key: str
    public_base_url: str = 'http://127.0.0.1:8000'
    cache_ttl: int = 300
    cleanup_interval: float = 5
    token_minutes: int = 60

    @classmethod
    def from_env(cls):
        load_dotenv(ROOT / '.env')
        secret = os.environ.get('SECRET_KEY', '')
        if len(secret) < 32 or secret.startswith('replace-'):
            raise RuntimeError('Set SECRET_KEY to a random string of at least 32 characters in hw3/.env.')
        return cls(
            database_url=os.environ.get('DATABASE_URL', f'sqlite+aiosqlite:///{ROOT / "data" / "links.db"}'),
            redis_url=os.environ.get('REDIS_URL', 'redis://127.0.0.1:6379/0'),
            secret_key=secret,
            public_base_url=os.environ.get('PUBLIC_BASE_URL', 'http://127.0.0.1:8000').rstrip('/'),
        )
