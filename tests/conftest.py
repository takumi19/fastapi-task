from dataclasses import replace
import importlib

from fakeredis.aioredis import FakeRedis
from fastapi.testclient import TestClient
import pytest

from src.cache import LinkCache
from src.config import Settings


@pytest.fixture
def settings(tmp_path):
    return Settings(database_url=f'sqlite+aiosqlite:///{tmp_path}/test.sqlite3',
                    redis_url='redis://unused.invalid/0', secret_key='test-only-secret-' * 4,
                    public_base_url='https://short.example', cleanup_interval=60)


@pytest.fixture
def app_factory(settings, monkeypatch):
    monkeypatch.setenv('PYTHON_DOTENV_DISABLED', '1')
    monkeypatch.setenv('SECRET_KEY', settings.secret_key)
    monkeypatch.setenv('DATABASE_URL', 'sqlite+aiosqlite:///:memory:')
    module = importlib.import_module('src.main')

    def fake_cache(url, ttl):
        cache = LinkCache(url, ttl)
        cache.client = FakeRedis(decode_responses=True)
        return cache

    monkeypatch.setattr(module, 'LinkCache', fake_cache)
    return lambda **overrides: module.create_app(replace(settings, **overrides))


@pytest.fixture
def app(app_factory):
    return app_factory()


@pytest.fixture
def client(app):
    with TestClient(app, follow_redirects=False) as value:
        yield value


@pytest.fixture
def login(client):
    def register_and_login(name='alice'):
        response = client.post('/auth/register', json={'username':name, 'password':'a-test-password-123'})
        assert response.status_code == 201, response.text
        token = client.post('/auth/login', data={'username':name, 'password':'a-test-password-123'})
        assert token.status_code == 200, token.text
        return {'Authorization':'Bearer ' + token.json()['access_token']}
    return register_and_login


@pytest.fixture
def owner(login):
    return login()


@pytest.fixture
def create_link(client):
    def create(code='example', headers=None, **kwargs):
        payload = {'original_url':'https://example.org/article', **kwargs}
        if code is not None:
            payload['custom_alias'] = code
        response = client.post('/links/shorten', json=payload, headers=headers or {})
        assert response.status_code == 201, response.text
        return response.json()
    return create


@pytest.fixture
def db_call(client):
    return client.portal.call
