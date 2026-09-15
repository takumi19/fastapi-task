from datetime import datetime, timedelta, timezone

import pytest

from src.config import Settings
from src.schemas import LinkCreate


@pytest.mark.parametrize('secret', ['', 'too-short', 'replace-with-at-least-32-random-characters'])
def test_missing_or_placeholder_secret_is_rejected(monkeypatch, secret):
    monkeypatch.setattr('src.config.load_dotenv', lambda path: None)
    monkeypatch.setenv('SECRET_KEY', secret)
    with pytest.raises(RuntimeError, match='SECRET_KEY'):
        Settings.from_env()


def test_environment_configuration_and_defaults(monkeypatch, tmp_path):
    monkeypatch.setattr('src.config.ROOT', tmp_path)
    monkeypatch.setattr('src.config.load_dotenv', lambda path: None)
    monkeypatch.setenv('SECRET_KEY', 'only-a-test-secret-' * 3)
    for name in ['DATABASE_URL','REDIS_URL','PUBLIC_BASE_URL']:
        monkeypatch.delenv(name, raising=False)
    config = Settings.from_env()
    assert str(tmp_path / 'data' / 'links.db') in config.database_url
    assert config.redis_url == 'redis://127.0.0.1:6379/0'
    monkeypatch.setenv('DATABASE_URL','postgresql+psycopg://test:pass@db/test')
    monkeypatch.setenv('REDIS_URL','redis://redis:6379/2')
    monkeypatch.setenv('PUBLIC_BASE_URL','https://short.example/')
    config = Settings.from_env()
    assert config.database_url.startswith('postgresql+psycopg://')
    assert config.redis_url.endswith('/2')
    assert config.public_base_url == 'https://short.example'


def test_expiration_normalizes_timezone_and_accepts_none():
    future = (datetime.now(timezone.utc)+timedelta(days=1)).replace(second=0,microsecond=0)
    offset = future.astimezone(timezone(timedelta(hours=3)))
    first = LinkCreate(original_url='https://example.org/', expires_at=offset)
    second = LinkCreate(original_url='https://example.org/', expires_at=future.replace(tzinfo=None))
    assert first.expires_at == second.expires_at == future
    assert LinkCreate(original_url='https://example.org/', expires_at=None).expires_at is None
