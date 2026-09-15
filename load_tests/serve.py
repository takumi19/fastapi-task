import os

if os.environ.get('HW4_LOAD_RUN') != '1':
    raise RuntimeError('Start this module through load_tests/run_load.py.')

from src.config import Settings
from src.main import create_app

app = create_app(Settings.from_env())
cache = app.state.cache
namespace = os.environ['LOAD_RUN_ID']
cache.key = lambda code: f'hw4:{namespace}:link:{code}'

if os.environ['LOAD_CACHE_MODE'] == 'bypass':
    async def miss(code):
        return None

    async def no_write(*args):
        pass

    cache.get = miss
    cache.set = no_write
    cache.delete = no_write
