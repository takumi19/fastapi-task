import json
import logging
import math
import time

from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)


class LinkCache:
    def __init__(self, url, ttl):
        self.client = Redis.from_url(url, decode_responses=True, socket_connect_timeout=1, socket_timeout=1)
        self.ttl = ttl

    @staticmethod
    def key(code):
        return f'hw3:link:{code}'

    async def get(self, code):
        try:
            raw = await self.client.get(self.key(code))
            if raw is None:
                return None
            value = json.loads(raw)
            if not isinstance(value, dict) or not {'id','revision','original_url','expires_at'} <= value.keys():
                return None
            if not isinstance(value['id'], str) or not isinstance(value['revision'], int) or not isinstance(value['original_url'], str):
                return None
            if value['expires_at'] is not None and (not isinstance(value['expires_at'], int) or value['expires_at'] <= time.time()):
                return None
            return value
        except (RedisError, ValueError, TypeError):
            logger.warning('Redis cache read unavailable; using database')
            return None

    async def set(self, code, value):
        ttl = self.ttl
        if value['expires_at'] is not None:
            ttl = min(ttl, math.floor(value['expires_at'] - time.time()))
        if ttl <= 0:
            return
        try:
            await self.client.set(self.key(code), json.dumps(value), ex=ttl)
        except RedisError:
            logger.warning('Redis cache write unavailable')

    async def delete(self, *codes):
        if not codes:
            return
        try:
            await self.client.delete(*(self.key(code) for code in codes))
        except RedisError:
            logger.warning('Redis cache invalidation unavailable; cached redirects will be checked against database revisions')

    async def close(self):
        await self.client.aclose()
