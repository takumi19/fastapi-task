import argparse
import csv
from datetime import datetime, timezone
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time

import httpx
from redis import Redis

ROOT = Path(__file__).resolve().parent.parent


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def run_case(name, workload, mode, users, seconds, redis_url, output):
    run_id = secrets.token_hex(8)
    host = f'http://127.0.0.1:{(port := free_port())}'
    namespace = f'hw4:{run_id}:*'
    redis = Redis.from_url(redis_url)
    with tempfile.TemporaryDirectory(prefix='hw4-load-') as temp:
        db_path = Path(temp) / 'load.sqlite3'
        env = {**os.environ, 'HW4_LOAD_RUN':'1', 'LOAD_RUN_ID':run_id,
               'LOAD_CACHE_MODE':mode, 'DATABASE_URL':f'sqlite+aiosqlite:///{db_path}',
               'SECRET_KEY':secrets.token_urlsafe(48), 'REDIS_URL':redis_url,
               'PUBLIC_BASE_URL':host, 'PYTHONUNBUFFERED':'1',
               'LOAD_OBSERVATIONS_PATH':str(output / f'{name}_cache.json')}
        with (output / f'{name}_server.log').open('w') as server_log:
            server = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'load_tests.serve:app',
                                       '--host','127.0.0.1','--port',str(port),'--no-access-log'],
                                      cwd=ROOT, env=env, stdout=server_log, stderr=subprocess.STDOUT)
            try:
                with httpx.Client(base_url=host, timeout=5, follow_redirects=False, trust_env=False) as client:
                    for _ in range(100):
                        if server.poll() is not None:
                            raise RuntimeError(f'Server exited; see {name}_server.log')
                        try:
                            if client.get('/health').status_code == 200:
                                break
                        except httpx.HTTPError:
                            pass
                        time.sleep(.1)
                    else:
                        raise RuntimeError('Load server did not become healthy')
                    warmup_clicks = 0
                    if workload == 'redirect':
                        for i in range(64):
                            code = f'hot-{i:03d}'
                            created = client.post('/links/shorten', json={'custom_alias':code,'original_url':f'https://example.org/{code}'})
                            if created.status_code != 201:
                                raise RuntimeError('Failed to seed hot links')
                            if client.get(f'/links/{code}').status_code != 307:
                                raise RuntimeError('Warmup redirect failed')
                            warmup_clicks += 1
                command = [sys.executable,'-m','locust','-f','load_tests/locustfile.py',
                           'CreateUser' if workload == 'create' else 'RedirectUser',
                           '--headless','--host',host,'--users',str(users),'--spawn-rate','10',
                           '--run-time',f'{seconds}s','--stop-timeout','5','--only-summary',
                           '--csv',str(output/name),'--html',str(output/f'{name}.html'),
                           '--exit-code-on-error','1']
                print(f'Running {name}: {workload}, {mode}, {users} users, {seconds}s', flush=True)
                with (output / f'{name}_locust.log').open('w') as log:
                    proc = subprocess.run(command,cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,
                                          timeout=seconds+60)
                stats = list(csv.DictReader((output/f'{name}_stats.csv').open()))
                total = next(row for row in stats if row['Name'] == 'Aggregated')
                observations = json.loads((output/f'{name}_cache.json').read_text())
                final_stats = observations.pop('final_stats')
                with sqlite3.connect(db_path) as db:
                    count, clicks = db.execute('SELECT COUNT(*),COALESCE(SUM(clicks),0) FROM links').fetchone()
                result = {'name':name,'workload':workload,'cache_mode':mode,'users':users,'seconds':seconds,
                          'requests':int(total['Request Count']),'failures':int(total['Failure Count']),
                          'rps':float(total['Requests/s']),'mean_ms':float(total['Average Response Time']),
                          'p50_ms':float(total['50%']),'p95_ms':float(total['95%']),'p99_ms':float(total['99%']),
                          'database_rows':count,'database_clicks':clicks,'warmup_clicks':warmup_clicks,
                          'locust_exit_code':proc.returncode,**observations,**final_stats}
                if workload == 'create':
                    result['database_matches_requests'] = count == result['requests']-result['failures']
                else:
                    result['database_matches_requests'] = clicks == result['requests']+warmup_clicks
                print(json.dumps(result),flush=True)
                return result
            finally:
                server.terminate()
                try:
                    server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)
                keys = list(redis.scan_iter(match=namespace))
                if keys:
                    redis.delete(*keys)
                redis.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seconds', type=int, default=20)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--users', type=int, default=20)
    parser.add_argument('--redis-url', default='redis://127.0.0.1:6379/14')
    args = parser.parse_args()
    if min(args.seconds,args.repeats,args.users) < 1:
        parser.error('seconds, repeats and users must be positive')
    output = ROOT / 'reports' / 'load'
    output.mkdir(parents=True,exist_ok=True)
    redis = Redis.from_url(args.redis_url, socket_connect_timeout=2)
    redis_version = redis.info('server')['redis_version']
    redis.close()
    metadata = {'date_utc':datetime.now(timezone.utc).isoformat(), 'python':platform.python_version(),
                'platform':platform.platform(), 'machine':platform.machine(),
                'cpu_count':os.cpu_count(), 'sqlite':sqlite3.sqlite_version, 'redis':redis_version,
                'locust':importlib.metadata.version('locust'), 'fastapi':importlib.metadata.version('fastapi'),
                'seconds_per_case':args.seconds, 'redirect_repeats':args.repeats,
                'users':args.users, 'spawn_rate':10, 'hot_links':64,
                'note':'Loopback; one Uvicorn worker; load client, API and Redis share this machine; ramp-up included.'}
    results = []
    def run(name,workload,mode):
        results.append(run_case(name,workload,mode,args.users,args.seconds,args.redis_url,output))
        (output/'results.json').write_text(json.dumps({'environment':metadata,'cases':results},indent=2)+'\n')
    run('create','create','enabled')
    for repeat in range(1,args.repeats+1):
        # Чередуем порядок запусков, чтобы он меньше влиял на результат.
        modes = ['enabled','bypass'] if repeat % 2 else ['bypass','enabled']
        for mode in modes:
            run(f'redirect_{mode}_{repeat}','redirect',mode)
    if any(row['failures'] or row['locust_exit_code'] or not row['database_matches_requests'] for row in results):
        raise SystemExit('Load run found errors or mismatched DB counts. Review reports/load/results.json.')
    print('All load scenarios completed without HTTP errors or lost database writes.',flush=True)


if __name__ == '__main__':
    main()
