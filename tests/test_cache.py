import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fakeredis.aioredis import FakeRedis
import pytest
from redis.exceptions import RedisError

from src.cache import LinkCache


@pytest.fixture
def cache():
    instance = LinkCache('redis://unused.invalid', ttl=300)
    instance.client = FakeRedis(decode_responses=True)
    return instance


@pytest.fixture
def value():
    return {'id':'link-uuid', 'revision':1, 'original_url':'https://example.org/', 'expires_at':None}


async def test_roundtrip_and_invalidation(cache, value):
    assert await cache.get('one') is None
    await cache.set('one', value)
    await cache.set('two', value)
    assert await cache.get('one') == value
    assert 298 <= await cache.client.ttl(cache.key('one')) <= 300
    await cache.delete('one', 'two')
    assert await cache.get('one') is None
    assert await cache.get('two') is None
    await cache.delete()
    await cache.close()


@pytest.mark.parametrize('raw', [
    '{broken-json', 'null', '[]', '{}',
    json.dumps({'id':123,'revision':1,'original_url':'https://example.org/','expires_at':None}),
    json.dumps({'id':'a','revision':'1','original_url':'https://example.org/','expires_at':None}),
    json.dumps({'id':'a','revision':1,'original_url':123,'expires_at':None}),
    json.dumps({'id':'a','revision':1,'original_url':'https://example.org/','expires_at':'not-a-date'}),
    json.dumps({'id':'a','revision':1,'original_url':'https://example.org/','expires_at':1}),
])
async def test_corrupt_or_expired_cache_is_a_miss(cache, raw):
    await cache.client.set(cache.key('bad'), raw)
    assert await cache.get('bad') is None
    await cache.close()


async def test_ttl_never_outlives_link(cache, value, monkeypatch):
    monkeypatch.setattr('src.cache.time', SimpleNamespace(time=lambda:1000.25))
    value['expires_at'] = 1010
    await cache.set('soon', value)
    assert 8 <= await cache.client.ttl(cache.key('soon')) <= 9
    assert await cache.get('soon') == value
    value['expires_at'] = 1000
    await cache.set('expired', value)
    assert not await cache.client.exists(cache.key('expired'))
    await cache.close()


async def test_cache_failures_do_not_prevent_db_fallback(cache, value, monkeypatch, caplog):
    for method in ['get','set','delete']:
        monkeypatch.setattr(cache.client, method, AsyncMock(side_effect=RedisError('offline')))
    assert await cache.get('one') is None
    await cache.set('one', value)
    await cache.delete('one')
    assert 'unavailable' in caplog.text
    await cache.close()
