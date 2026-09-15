from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

RESERVED = {'search', 'shorten', 'auth', 'links', 'docs', 'redoc', 'openapi.json', 'health'}


class UserCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    username: str = Field(min_length=3, max_length=32, pattern=r'^[A-Za-z0-9_.-]+$')
    password: str = Field(min_length=8, max_length=128)

    @field_validator('username')
    @classmethod
    def normalize_username(cls, value):
        return value.lower()


class UserOut(BaseModel):
    id: str
    username: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = 'bearer'


class URLInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    original_url: HttpUrl = Field(max_length=2048)

    @field_validator('original_url')
    @classmethod
    def without_credentials(cls, value):
        if value.username is not None or value.password is not None:
            raise ValueError('URL must not contain a username or password.')
        return value


class LinkCreate(URLInput):
    custom_alias: str | None = Field(default=None, min_length=3, max_length=32, pattern=r'^[A-Za-z0-9_-]+$')
    expires_at: datetime | None = None

    @field_validator('custom_alias')
    @classmethod
    def not_reserved(cls, value):
        if value and value.lower() in RESERVED:
            raise ValueError('This alias is reserved.')
        return value

    @field_validator('expires_at')
    @classmethod
    def future_minute(cls, value):
        if value is None:
            return None
        value = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
        if value.second or value.microsecond:
            raise ValueError('expires_at must have minute precision (seconds must be zero).')
        if value <= datetime.now(timezone.utc):
            raise ValueError('expires_at must be in the future.')
        return value


class LinkOut(BaseModel):
    short_code: str
    short_url: str
    original_url: str
    created_at: datetime
    expires_at: datetime | None
    clicks: int
    last_accessed_at: datetime | None


def describe(link, base_url):
    def date(value):
        return datetime.fromtimestamp(value, timezone.utc) if value is not None else None
    return LinkOut(short_code=link.short_code, short_url=f'{base_url}/links/{link.short_code}',
                   original_url=link.original_url, created_at=date(link.created_at),
                   expires_at=date(link.expires_at), clicks=link.clicks,
                   last_accessed_at=date(link.last_accessed_at))
