import asyncio
from contextlib import asynccontextmanager, suppress
import logging
import secrets
import time

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import HttpUrl, TypeAdapter, ValidationError
from sqlalchemy import case, delete, or_, select, text, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from .auth import (DUMMY_HASH, access_token, get_db, optional_user, passwords,
                   required_user, unauthorized, verify_password)
from .cache import LinkCache
from .config import Settings
from .database import Base, Link, User, connect_database
from .schemas import LinkCreate, LinkOut, TokenOut, URLInput, UserCreate, UserOut, describe

logger = logging.getLogger(__name__)


def alive(now):
    return or_(Link.expires_at.is_(None), Link.expires_at > now)


def generate_code():
    return secrets.token_urlsafe(6)


def cache_value(link):
    return {'id':link.id, 'revision':link.revision, 'original_url':link.original_url,
            'expires_at':link.expires_at}


async def purge_expired(sessions, cache):
    async with sessions() as db:
        codes = list((await db.scalars(delete(Link).where(Link.expires_at <= int(time.time())).returning(Link.short_code))).all())
        await db.commit()
    await cache.delete(*codes)
    return len(codes)


async def remove_expired_code(db, cache, code):
    removed = await db.scalar(delete(Link).where(Link.short_code == code, Link.expires_at <= int(time.time())).returning(Link.short_code))
    await db.commit()
    if removed:
        await cache.delete(code)


async def get_live_link(db, cache, code):
    link = await db.scalar(select(Link).where(Link.short_code == code))
    if link is None:
        raise HTTPException(404, 'Link not found')
    if link.expires_at is not None and link.expires_at <= time.time():
        await db.delete(link)
        await db.commit()
        await cache.delete(code)
        raise HTTPException(404, 'Link not found or expired')
    return link


def create_app(settings=None):
    settings = settings or Settings.from_env()
    engine, sessions = connect_database(settings.database_url)
    cache = LinkCache(settings.redis_url, settings.cache_ttl)

    @asynccontextmanager
    async def lifespan(app):
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await purge_expired(sessions, cache)

        async def expiration_worker():
            while True:
                await asyncio.sleep(settings.cleanup_interval)
                try:
                    await purge_expired(sessions, cache)
                except SQLAlchemyError:
                    logger.warning('Expiration cleanup temporarily failed; requests still enforce expiry')

        task = asyncio.create_task(expiration_worker())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            await cache.close()
            await engine.dispose()

    app = FastAPI(title='HW3 — URL shortener', version='1.0.0', lifespan=lifespan,
                  description='Required assignment features: links, aliases, expiry, statistics, registration and Redis caching.')
    app.state.settings = settings
    app.state.sessions = sessions
    app.state.cache = cache

    @app.get('/health', tags=['Service'])
    async def health():
        database_ok = redis_ok = False
        try:
            async with sessions() as db:
                await db.execute(text('SELECT 1'))
                database_ok = True
        except SQLAlchemyError:
            pass
        try:
            redis_ok = bool(await cache.client.ping())
        except Exception:
            pass
        return JSONResponse({'database':database_ok, 'redis':redis_ok}, status_code=200 if database_ok and redis_ok else 503)

    @app.post('/auth/register', response_model=UserOut, status_code=201, tags=['Authentication'])
    async def register(payload: UserCreate, db=Depends(get_db)):
        hashed = await run_in_threadpool(passwords.hash, payload.password)
        user = User(username=payload.username, password_hash=hashed)
        db.add(user)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raise HTTPException(409, 'Username already registered') from None
        return UserOut(id=user.id, username=user.username)

    @app.post('/auth/login', response_model=TokenOut, tags=['Authentication'])
    async def login(form: OAuth2PasswordRequestForm = Depends(), db=Depends(get_db)):
        if len(form.username) > 32 or len(form.password) > 128:
            raise unauthorized()
        user = await db.scalar(select(User).where(User.username == form.username.lower()))
        valid = await verify_password(form.password, user.password_hash if user else DUMMY_HASH)
        if user is None or not valid:
            raise unauthorized()
        return TokenOut(access_token=access_token(user, settings))

    @app.post('/links/shorten', response_model=LinkOut, status_code=201, tags=['Links'])
    async def shorten(payload: LinkCreate, db=Depends(get_db), user=Depends(optional_user)):
        owner_id = user.id if user else None
        if payload.custom_alias:
            await remove_expired_code(db, cache, payload.custom_alias)
        for _ in range(8):
            code = payload.custom_alias or generate_code()
            link = Link(short_code=code, original_url=str(payload.original_url), owner_id=owner_id,
                        expires_at=int(payload.expires_at.timestamp()) if payload.expires_at else None)
            db.add(link)
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()
                if payload.custom_alias:
                    raise HTTPException(409, 'Alias already exists') from None
                continue
            await cache.delete(code)
            return describe(link, settings.public_base_url)
        raise HTTPException(503, 'Could not allocate a unique code; retry the request')

    @app.get('/links/search', response_model=list[LinkOut], tags=['Links'])
    async def search(original_url: str = Query(max_length=2048), db=Depends(get_db)):
        try:
            url = str(TypeAdapter(HttpUrl).validate_python(original_url))
        except ValidationError:
            raise HTTPException(422, 'original_url must be an HTTP(S) URL') from None
        rows = await db.scalars(select(Link).where(Link.original_url == url, alive(int(time.time()))).order_by(Link.created_at, Link.short_code))
        return [describe(link, settings.public_base_url) for link in rows]

    @app.get('/links/{short_code}/stats', response_model=LinkOut, tags=['Links'])
    async def stats(short_code: str, db=Depends(get_db)):
        link = await get_live_link(db, cache, short_code)
        return describe(link, settings.public_base_url)

    @app.put('/links/{short_code}', response_model=LinkOut, tags=['Links'])
    async def change(short_code: str, payload: URLInput, db=Depends(get_db), user=Depends(required_user)):
        user_id = user.id
        link = await get_live_link(db, cache, short_code)
        if link.owner_id != user_id:
            raise HTTPException(403, 'Only the owner can update this link')
        changed = await db.scalar(update(Link).where(Link.id == link.id, Link.owner_id == user_id,
            alive(int(time.time()))).values(original_url=str(payload.original_url), revision=Link.revision+1)
            .returning(Link).execution_options(populate_existing=True))
        if changed is None:
            await db.rollback()
            raise HTTPException(404, 'Link not found or expired')
        await db.commit()
        await cache.delete(short_code)
        return describe(changed, settings.public_base_url)

    @app.delete('/links/{short_code}', status_code=204, tags=['Links'])
    async def remove(short_code: str, db=Depends(get_db), user=Depends(required_user)):
        user_id = user.id
        link = await get_live_link(db, cache, short_code)
        if link.owner_id != user_id:
            raise HTTPException(403, 'Only the owner can delete this link')
        removed = await db.scalar(delete(Link).where(Link.id == link.id, Link.owner_id == user_id,
                                                    alive(int(time.time()))).returning(Link.short_code))
        if removed is None:
            await db.rollback()
            raise HTTPException(404, 'Link not found or expired')
        await db.commit()
        await cache.delete(short_code)
        return Response(status_code=204)

    @app.get('/links/{short_code}', response_class=RedirectResponse, status_code=307, tags=['Links'])
    async def redirect(short_code: str, db=Depends(get_db)):
        if not 3 <= len(short_code) <= 32:
            raise HTTPException(404, 'Link not found')
        cached = await cache.get(short_code)
        now = int(time.time())
        values = {'clicks':Link.clicks+1,
                  'last_accessed_at':case((Link.last_accessed_at > now, Link.last_accessed_at), else_=now)}
        condition = (Link.short_code == short_code, alive(now))
        if cached:
            touched = await db.scalar(update(Link).where(*condition, Link.id == cached['id'],
                                    Link.revision == cached['revision']).values(**values).returning(Link.id))
            if touched is not None:
                await db.commit()
                return RedirectResponse(cached['original_url'], status_code=307,
                                        headers={'X-Cache':'HIT', 'Cache-Control':'no-store'})
            await cache.delete(short_code)
        link = await db.scalar(update(Link).where(*condition).values(**values).returning(Link))
        if link is None:
            await db.rollback()
            await remove_expired_code(db, cache, short_code)
            raise HTTPException(404, 'Link not found or expired')
        await db.commit()
        await cache.set(short_code, cache_value(link))
        return RedirectResponse(link.original_url, status_code=307,
                                headers={'X-Cache':'MISS', 'Cache-Control':'no-store'})

    app.add_api_route('/{short_code}', redirect, methods=['GET'], response_class=RedirectResponse,
                      status_code=307, include_in_schema=False)
    return app


app = create_app()
