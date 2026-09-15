from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request
from fastapi.security import OAuth2PasswordBearer
import jwt
from pwdlib import PasswordHash
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from .database import User

passwords = PasswordHash.recommended()
oauth2 = OAuth2PasswordBearer(tokenUrl='auth/login', auto_error=False)
DUMMY_HASH = passwords.hash('not-a-user-password')


def unauthorized():
    return HTTPException(status_code=401, detail='Invalid or missing credentials', headers={'WWW-Authenticate':'Bearer'})


async def get_db(request: Request):
    async with request.app.state.sessions() as session:
        yield session


async def optional_user(request: Request, token: str | None = Depends(oauth2), db=Depends(get_db)):
    if token is None:
        if request.headers.get('Authorization'):
            raise unauthorized()
        return None
    try:
        claims = jwt.decode(token, request.app.state.settings.secret_key, algorithms=['HS256'],
                            options={'require':['sub','exp','iat']}, issuer='hw3-shortener')
        if not isinstance(claims['sub'], str):
            raise unauthorized()
    except jwt.InvalidTokenError:
        raise unauthorized() from None
    user = await db.scalar(select(User).where(User.id == claims['sub']))
    if user is None:
        raise unauthorized()
    return user


async def required_user(user=Depends(optional_user)):
    if user is None:
        raise unauthorized()
    return user


def access_token(user, settings):
    now = datetime.now(timezone.utc)
    return jwt.encode({'sub':user.id, 'iat':now, 'exp':now+timedelta(minutes=settings.token_minutes),
                       'iss':'hw3-shortener'}, settings.secret_key, algorithm='HS256')


async def verify_password(value, stored):
    return await run_in_threadpool(passwords.verify, value, stored)
