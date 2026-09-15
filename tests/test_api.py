import asyncio
from datetime import datetime, timedelta, timezone
import json
import re
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
import httpx
import jwt
import pytest
from redis.exceptions import RedisError
from sqlalchemy import delete, select, update

from src.auth import passwords
from src.database import Link, User


def test_registration_login_and_password_storage(client, app, db_call):
    response = client.post('/auth/register', json={'username':'Alice', 'password':'long-enough-password'})
    assert response.status_code == 201
    assert set(response.json()) == {'id', 'username'}
    assert response.json()['username'] == 'alice'
    assert client.post('/auth/register', json={'username':'ALICE', 'password':'another-password'}).status_code == 409
    async def get_hash():
        async with app.state.sessions() as db:
            return (await db.scalar(select(User))).password_hash
    hashed = db_call(get_hash)
    assert hashed.startswith('$argon2')
    assert passwords.verify('long-enough-password', hashed)
    result = client.post('/auth/login', data={'username':'ALICE', 'password':'long-enough-password'})
    assert result.status_code == 200
    assert result.json()['token_type'] == 'bearer'
    claims = jwt.decode(result.json()['access_token'], app.state.settings.secret_key, algorithms=['HS256'], issuer='hw3-shortener')
    assert claims['sub'] == response.json()['id']
    assert claims['exp'] - claims['iat'] == 3600


@pytest.mark.parametrize('data', [
    {'username':'absent', 'password':'wrong'},
    {'username':'alice', 'password':'wrong'},
    {'username':'x' * 33, 'password':'wrong'},
    {'username':'alice', 'password':'x' * 129},
])
def test_invalid_login(client, owner, data):
    response = client.post('/auth/login', data=data)
    assert response.status_code == 401
    assert response.headers['www-authenticate'] == 'Bearer'


@pytest.mark.parametrize('data', [
    {'username':'ab', 'password':'long-password'},
    {'username':'has space', 'password':'long-password'},
    {'username':'valid', 'password':'short'},
    {'username':'valid', 'password':'long-password', 'admin':True},
])
def test_invalid_registration(client, data):
    assert client.post('/auth/register', json=data).status_code == 422


def test_crud_redirect_stats_and_search(client, owner, create_link, db_call, app):
    created = create_link(headers=owner)
    assert created['short_url'] == 'https://short.example/links/example'
    assert created['clicks'] == 0 and created['last_accessed_at'] is None
    assert created['expires_at'] is None
    assert datetime.fromisoformat(created['created_at'].replace('Z', '+00:00')).tzinfo is not None
    first = client.get('/links/example')
    second = client.get('/example')
    assert first.status_code == second.status_code == 307
    assert first.headers['location'] == 'https://example.org/article'
    assert first.headers['x-cache'] == 'MISS' and second.headers['x-cache'] == 'HIT'
    assert second.headers['cache-control'] == 'no-store'
    stats = client.get('/links/example/stats').json()
    assert stats['clicks'] == 2 and stats['last_accessed_at'] is not None
    assert client.get('/links/example/stats').json()['clicks'] == 2
    result = client.get('/links/search', params={'original_url':created['original_url']})
    assert [x['short_code'] for x in result.json()] == ['example']
    response = client.put('/links/example', headers=owner, json={'original_url':'https://example.org/new'})
    assert response.status_code == 200
    assert response.json()['original_url'] == 'https://example.org/new'
    assert response.json()['clicks'] == 2
    assert not db_call(app.state.cache.client.exists, 'hw3:link:example')
    assert client.get('/links/search', params={'original_url':created['original_url']}).json() == []
    assert client.get('/links/example').headers['location'] == 'https://example.org/new'
    assert client.delete('/links/example', headers=owner).status_code == 204
    assert not db_call(app.state.cache.client.exists, 'hw3:link:example')
    assert client.get('/links/example').status_code == 404
    assert client.get('/links/example/stats').status_code == 404
    assert client.delete('/links/example', headers=owner).status_code == 404


def test_owner_required_for_mutations(client, owner, login, create_link):
    create_link(headers=owner)
    stranger = login('bob')
    for headers, expected in [({}, 401), (stranger, 403)]:
        assert client.put('/links/example', headers=headers, json={'original_url':'https://example.net/'}).status_code == expected
        assert client.delete('/links/example', headers=headers).status_code == expected
    create_link('anonymous')
    assert client.put('/links/anonymous', headers=owner, json={'original_url':'https://example.net/'}).status_code == 403
    assert client.delete('/links/anonymous', headers=owner).status_code == 403
    assert client.get('/links/anonymous').status_code == 307


@pytest.mark.parametrize('auth', ['Bearer invalid', 'Basic abc', 'Bearer'])
def test_supplied_invalid_auth_is_not_anonymous(client, auth):
    response = client.post('/links/shorten', json={'original_url':'https://example.org/'}, headers={'Authorization':auth})
    assert response.status_code == 401


@pytest.mark.parametrize('change', ['expired', 'missing_exp', 'bad_issuer', 'numeric_sub', 'unknown_user', 'wrong_signature'])
def test_invalid_jwt_claims(client, settings, change):
    now = int(datetime.now(timezone.utc).timestamp())
    payload = {'sub':'nonexistent-id', 'iat':now, 'exp':now+3600, 'iss':'hw3-shortener'}
    secret = settings.secret_key
    if change == 'expired':
        payload['exp'] = now-1
    elif change == 'missing_exp':
        del payload['exp']
    elif change == 'bad_issuer':
        payload['iss'] = 'another-service'
    elif change == 'numeric_sub':
        payload['sub'] = 123
    elif change == 'wrong_signature':
        secret = 'different-signing-key-' * 4
    token = jwt.encode(payload, secret, algorithm='HS256')
    response = client.post('/links/shorten', json={'original_url':'https://example.org/'}, headers={'Authorization':'Bearer '+token})
    assert response.status_code == 401


@pytest.mark.parametrize('payload', [
    {}, {'original_url':'javascript:alert(1)'}, {'original_url':'ftp://example.org/'},
    {'original_url':'https://name:password@example.org/'},
    {'original_url':'https://example.org/' + 'a' * 2100},
    {'original_url':'https://example.org/', 'custom_alias':'ab'},
    {'original_url':'https://example.org/', 'custom_alias':'with/slash'},
    {'original_url':'https://example.org/', 'custom_alias':'search'},
    {'original_url':'https://example.org/', 'custom_alias':'SHORTEN'},
    {'original_url':'https://example.org/', 'expires_at':'2000-01-01T00:00:00Z'},
    {'original_url':'https://example.org/', 'expires_at':'2035-01-01T00:00:01Z'},
    {'original_url':'https://example.org/', 'unexpected':True},
])
def test_invalid_creation_payload(client, payload):
    assert client.post('/links/shorten', json=payload).status_code == 422


def test_search_matches_normalized_url_and_multiple_links(client, create_link):
    create_link('first', original_url='https://EXAMPLE.org')
    create_link('second', original_url='https://example.org/')
    result = client.get('/links/search', params={'original_url':'https://EXAMPLE.org'})
    assert result.status_code == 200
    assert {x['short_code'] for x in result.json()} == {'first','second'}
    assert client.get('/links/search', params={'original_url':'https://absent.example/'}).json() == []
    assert client.get('/links/search', params={'original_url':'invalid'}).status_code == 422
    assert client.get('/links/search').status_code == 422


def test_generated_codes_and_collision_retry(client, owner, create_link):
    code = create_link(None)['short_code']
    assert re.fullmatch(r'[A-Za-z0-9_-]{8}', code)
    with patch('src.main.generate_code', side_effect=[code,'freecode']) as generate:
        created = create_link(None, headers=owner)
        assert created['short_code'] == 'freecode'
        assert generate.call_count == 2
    with patch('src.main.generate_code', return_value=code):
        assert client.post('/links/shorten', json={'original_url':'https://example.org/'}).status_code == 503
    assert client.get('/links/freecode/stats').json()['clicks'] == 0


def test_alias_conflict(client, create_link):
    create_link('unique')
    assert client.post('/links/shorten', json={'original_url':'https://example.net/', 'custom_alias':'unique'}).status_code == 409


@pytest.mark.parametrize('path', ['/links/no-such-link','/links/a','/links/'+'a'*33,'/no-such-link'])
def test_missing_redirects(client, path):
    assert client.get(path).status_code == 404


def test_invalid_update_keeps_destination(client, owner, create_link):
    create_link(headers=owner)
    assert client.put('/links/example', headers=owner, json={'original_url':'not a URL'}).status_code == 422
    assert client.get('/links/example').headers['location'] == 'https://example.org/article'


def test_concurrent_redirects_have_no_lost_clicks(client, app, create_link, db_call):
    create_link()
    async def many_redirects():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as http:
            return await asyncio.gather(*(http.get('/links/example', follow_redirects=False) for _ in range(24)))
    responses = db_call(many_redirects)
    assert all(r.status_code == 307 for r in responses)
    assert client.get('/links/example/stats').json()['clicks'] == 24


def test_stale_cache_after_update_and_alias_reuse(client, owner, create_link, app, db_call):
    create_link(headers=owner)
    client.get('/links/example')
    redis = app.state.cache.client
    old = db_call(redis.get, 'hw3:link:example')
    assert client.put('/links/example', headers=owner, json={'original_url':'https://example.org/new'}).status_code == 200
    db_call(redis.set, 'hw3:link:example', old)
    response = client.get('/links/example')
    assert response.headers['location'] == 'https://example.org/new'
    assert response.headers['x-cache'] == 'MISS'
    assert client.get('/links/example/stats').json()['clicks'] == 2
    assert client.delete('/links/example', headers=owner).status_code == 204
    create_link(original_url='https://example.org/reused')
    db_call(redis.set, 'hw3:link:example', old)
    response = client.get('/links/example')
    assert response.headers['location'] == 'https://example.org/reused'
    assert client.get('/links/example/stats').json()['clicks'] == 1


def test_redis_outage_and_recovery(client, owner, create_link, app):
    create_link(headers=owner)
    client.get('/links/example')
    redis = app.state.cache.client
    with patch.object(redis, 'get', AsyncMock(side_effect=RedisError)), \
         patch.object(redis, 'set', AsyncMock(side_effect=RedisError)), \
         patch.object(redis, 'delete', AsyncMock(side_effect=RedisError)):
        assert client.put('/links/example', headers=owner, json={'original_url':'https://example.org/recovered'}).status_code == 200
        response = client.get('/links/example')
        assert response.status_code == 307
        assert response.headers['location'] == 'https://example.org/recovered'
    assert client.get('/links/example').headers['location'] == 'https://example.org/recovered'
    assert client.get('/links/example/stats').json()['clicks'] == 3


@pytest.mark.parametrize('method', ['PUT','DELETE'])
def test_deleted_between_ownership_check_and_write(client, owner, create_link, monkeypatch, method):
    import src.main as main
    create_link(headers=owner)
    original = main.get_live_link
    async def concurrent_deletion(db, cache, code):
        link = await original(db, cache, code)
        await db.execute(delete(Link).where(Link.id == link.id))
        await db.commit()
        return link
    monkeypatch.setattr(main, 'get_live_link', concurrent_deletion)
    kwargs = {'json':{'original_url':'https://example.net/'}} if method == 'PUT' else {}
    assert client.request(method, '/links/example', headers=owner, **kwargs).status_code == 404


def test_restart_preserves_links(app_factory):
    with TestClient(app_factory(), follow_redirects=False) as first:
        assert first.post('/links/shorten', json={'original_url':'https://example.org/', 'custom_alias':'persist'}).status_code == 201
        assert first.get('/links/persist').status_code == 307
    with TestClient(app_factory(), follow_redirects=False) as second:
        data = second.get('/links/persist/stats').json()
        assert data['original_url'] == 'https://example.org/'
        assert data['clicks'] == 1
