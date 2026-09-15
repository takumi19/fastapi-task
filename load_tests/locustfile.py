import csv
import json
import os
from pathlib import Path
import random
from uuid import uuid4

from locust import HttpUser, constant, events, task

observations = {'cache_hits':0, 'cache_misses':0}


class CreateUser(HttpUser):
    wait_time = constant(0)

    @task
    def create(self):
        with self.client.post('/links/shorten', json={'original_url':f'https://example.org/load/{uuid4().hex}'},
                              name='POST /links/shorten', catch_response=True, timeout=10) as response:
            if response.status_code != 201:
                response.failure(f'Expected 201, got {response.status_code}')
                return
            try:
                data = response.json()
                if not data.get('short_code') or data.get('clicks') != 0:
                    response.failure('Invalid link response')
            except ValueError:
                response.failure('Invalid JSON')


class RedirectUser(HttpUser):
    wait_time = constant(0)

    @task
    def redirect(self):
        code = f'hot-{random.randrange(64):03d}'
        with self.client.get(f'/links/{code}', allow_redirects=False, name='GET /links/[hot]',
                             catch_response=True, timeout=10) as response:
            if response.status_code != 307 or response.headers.get('Location') != f'https://example.org/{code}':
                response.failure(f'Invalid redirect ({response.status_code})')
                return
            header = response.headers.get('X-Cache')
            expected = 'HIT' if os.environ['LOAD_CACHE_MODE'] == 'enabled' else 'MISS'
            if header != expected:
                response.failure(f'Expected X-Cache {expected}, got {header}')
            if header == 'HIT':
                observations['cache_hits'] += 1
            elif header == 'MISS':
                observations['cache_misses'] += 1


@events.quitting.add_listener
def save_observations(environment, **kwargs):
    if path := os.environ.get('LOAD_OBSERVATIONS_PATH'):
        total = environment.stats.total
        final = {
            'requests': total.num_requests, 'failures': total.num_failures,
            'rps': total.total_rps, 'mean_ms': total.avg_response_time,
            'p50_ms': total.get_response_time_percentile(0.50),
            'p95_ms': total.get_response_time_percentile(0.95),
            'p99_ms': total.get_response_time_percentile(0.99),
        }
        Path(path).write_text(json.dumps({**observations, 'final_stats': final}, indent=2) + '\n')
        final_csv = Path(path).with_name(Path(path).name.replace('_cache.json', '_final_stats.csv'))
        with final_csv.open('w', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(final))
            writer.writeheader()
            writer.writerow(final)
