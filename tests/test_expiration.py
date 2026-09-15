import asyncio
from datetime import datetime, timedelta, timezone
import threading
import time
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError

from src.database import Base, Link, connect_database


def expiry():
    return (datetime.now(timezone.utc) + timedelta(minutes=3)).replace(second=0, microsecond=0).isoformat()


@pytest.mark.parametrize('operation', ['redirect','stats','update','delete','search'])
def test_expired_link_is_not_accessible(client, app, owner, create_link, db_call, operation):
    create_link(headers=owner, expires_at=expiry())
    assert client.get('/links/example').status_code == 307
    async def expire():
        async with app.state.sessions() as db:
            await db.execute(update(Link).where(Link.short_code == 'example').values(expires_at=int(time.time())-1))
            await db.commit()
    db_call(expire)
    if operation == 'redirect':
        response = client.get('/links/example')
    elif operation == 'stats':
        response = client.get('/links/example/stats')
    elif operation == 'update':
        response = client.put('/links/example', headers=owner, json={'original_url':'https://example.net/'})
    elif operation == 'delete':
        response = client.delete('/links/example', headers=owner)
    else:
        response = client.get('/links/search', params={'original_url':'https://example.org/article'})
        assert response.status_code == 200 and response.json() == []
        return
    assert response.status_code == 404
    assert not db_call(app.state.cache.client.exists, 'hw3:link:example')
    async def absent():
        async with app.state.sessions() as db:
            return await db.scalar(select(Link).where(Link.short_code == 'example'))
    assert db_call(absent) is None


def test_expired_alias_can_be_recreated(client, app, create_link, db_call):
    create_link(expires_at=expiry())
    async def expire():
        async with app.state.sessions() as db:
            await db.execute(update(Link).where(Link.short_code == 'example').values(expires_at=1))
            await db.commit()
    db_call(expire)
    created = create_link(original_url='https://example.net/reused')
    assert created['clicks'] == 0
    assert client.get('/links/example').headers['location'] == 'https://example.net/reused'


def test_startup_removes_expired_rows(app_factory, settings):
    async def seed():
        engine, sessions = connect_database(settings.database_url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with sessions() as db:
            db.add(Link(short_code='expired', original_url='https://example.org/', expires_at=1))
            db.add(Link(short_code='active', original_url='https://example.org/'))
            await db.commit()
        await engine.dispose()
    asyncio.run(seed())
    app = app_factory()
    with TestClient(app) as client:
        async def codes():
            async with app.state.sessions() as db:
                return list(await db.scalars(select(Link.short_code)))
        assert client.portal.call(codes) == ['active']


def test_background_cleanup_recovers_after_database_error(app_factory, monkeypatch, caplog):
    import src.main as main
    app = app_factory(cleanup_interval=0.02)
    recovered = threading.Event()
    original = main.purge_expired
    count = 0
    async def transient_failure(sessions, cache):
        nonlocal count
        count += 1
        if count == 1:
            raise SQLAlchemyError('simulated DB interruption')
        result = await original(sessions, cache)
        if result:
            recovered.set()
        return result
    with TestClient(app) as client:
        async def seed():
            async with app.state.sessions() as db:
                db.add(Link(short_code='background', original_url='https://example.org/', expires_at=1))
                await db.commit()
            await app.state.cache.client.set('hw3:link:background', 'old-value')
        monkeypatch.setattr(main, 'purge_expired', transient_failure)
        client.portal.call(seed)
        assert recovered.wait(timeout=3), 'background worker did not recover'
        assert not client.portal.call(app.state.cache.client.exists, 'hw3:link:background')
    assert 'Expiration cleanup temporarily failed' in caplog.text
    assert count >= 2


def test_health_reports_dependency_failures(client, app):
    assert client.get('/health').json() == {'database':True, 'redis':True}
    with patch.object(app.state.cache.client, 'ping', AsyncMock(side_effect=ConnectionError)):
        result = client.get('/health')
        assert result.status_code == 503
        assert result.json() == {'database':True, 'redis':False}
    with patch('sqlalchemy.ext.asyncio.AsyncSession.execute', new=AsyncMock(side_effect=SQLAlchemyError)):
        result = client.get('/health')
        assert result.status_code == 503
        assert result.json() == {'database':False, 'redis':True}
