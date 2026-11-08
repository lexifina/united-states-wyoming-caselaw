"""Download the whole Caselaw Access Project bulk set (static.case.law): every volume zip listed in
VolumesMetadata.json, into <dir>/zips/<reporter>/<volume>.zip, verified (size and zip CRCs). Resumable: a verified zip
is skipped. Priority reporters first (--first). Concurrency starts at --start streams and ramps by --step every
--ramp seconds while there are no 429/5xx/timeouts and throughput keeps rising; any such error halves it (floor 4) and
pauses that stream. An HTML or challenge answer (WAF, CAPTCHA) stops everything: it is never worked round.
static.case.law's robots.txt disallows all paths; Alan overruled it for these documented bulk files (2026-11-08).

Status: <dir>/status.json every 10 s. Stop: create <dir>/STOP. Pauses while C: has under --floor-gb free.

  python cap_bulk_fetch.py <dir> [--start 16] [--max 64] [--first cal,cal-2d,...] [--sample 50]"""
import argparse
import json
import os
import random
import shutil
import threading
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

UA = {'User-Agent': 'lexifina-gitlaw (case law archive; corpus@lexifina.com)'}
ap = argparse.ArgumentParser()
ap.add_argument('dir')
ap.add_argument('--start', type=int, default=16), ap.add_argument('--step', type=int, default=8)
ap.add_argument('--max', type=int, default=64), ap.add_argument('--ramp', type=int, default=60)
ap.add_argument('--floor-gb', type=float, default=12), ap.add_argument('--sample', type=int, default=50)
ap.add_argument('--first', default='')
a = ap.parse_args()
D = Path(a.dir)
Z = D / 'zips'
vols = json.loads((D / 'meta' / 'VolumesMetadata.json').read_text(encoding='utf-8'))
first = [s for s in a.first.split(',') if s]
rank = {s: i for i, s in enumerate(first)}
vols.sort(key=lambda v: (rank.get(v['reporter_slug'], len(rank)), v['reporter_slug'], len(v['volume_folder']), v['volume_folder']))
lock = threading.RLock()  # the monitor wakes waiting streams (cond) while holding it
st = {'volumes': len(vols), 'done': 0, 'skipped': 0, 'failed': 0, 'bytes': 0, 'errors': 0, 'concurrency': a.start,
      'started': time.strftime('%Y-%m-%dT%H:%M:%S'), 'state': 'sampling', 'est_total_gb': None, 'mbps': 0.0,
      'eta_h': None, 'last_error': '', 'first_done': {}, 'pid': os.getpid()}
sem_size = [a.start]
active = [0]
cond = threading.Condition(threading.Lock())
errors_window = [0]
stop_all = [None]


def log(*m):
    with open(D / 'fetch.log', 'a', encoding='utf-8') as f:
        f.write(time.strftime('%H:%M:%S ') + ' '.join(map(str, m)) + '\n')


def url_of(v):
    return f"https://static.case.law/{v['reporter_slug']}/{v['volume_folder']}.zip"


def dest_of(v):
    return Z / v['reporter_slug'] / f"{v['volume_folder']}.zip"


def verified(p: Path) -> bool:
    try:
        with zipfile.ZipFile(p) as z:
            return z.testzip() is None
    except Exception:  # noqa: BLE001
        return False


def free_gb():
    return shutil.disk_usage(str(D)).free / 1e9


def acquire():
    with cond:
        while active[0] >= sem_size[0]:
            cond.wait(1)
        active[0] += 1


def release():
    with cond:
        active[0] -= 1
        cond.notify_all()


def one(v):
    if stop_all[0] or (D / 'STOP').exists():
        return
    dest = dest_of(v)
    ok_mark = dest.with_suffix('.ok')
    if ok_mark.exists() and dest.exists():
        with lock:
            st['skipped'] += 1
        return
    while free_gb() < a.floor_gb and not (D / 'STOP').exists():
        st['state'] = f'paused: {free_gb():.1f} GB free'
        time.sleep(30)
    for attempt in range(6):
        if stop_all[0] or (D / 'STOP').exists():
            return
        acquire()
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix('.part')
            r = urllib.request.urlopen(urllib.request.Request(url_of(v), headers=UA), timeout=120)
            ctype = r.headers.get('Content-Type', '')
            if 'html' in ctype or r.headers.get('cf-mitigated'):
                stop_all[0] = f'challenge or HTML answer for {url_of(v)} ({ctype})'
                log('STOP', stop_all[0])
                return
            n = 0
            with open(tmp, 'wb') as f:
                while chunk := r.read(1 << 20):
                    f.write(chunk)
                    n += len(chunk)
            want = int(r.headers.get('Content-Length') or n)
            release_needed = False
        except urllib.error.HTTPError as e:
            release()
            if e.code == 404:
                with lock:
                    st['failed'] += 1
                log('404', url_of(v))
                return
            if e.code in (403,):
                stop_all[0] = f'403 for {url_of(v)}: stopped, not worked round'
                log('STOP', stop_all[0])
                return
            with lock:
                st['errors'] += 1
                errors_window[0] += 1
                st['last_error'] = f'{e.code} {url_of(v)}'
            time.sleep(10 * (attempt + 1))
            continue
        except Exception as e:  # noqa: BLE001 (timeouts, resets)
            release()
            with lock:
                st['errors'] += 1
                errors_window[0] += 1
                st['last_error'] = f'{type(e).__name__} {url_of(v)}'
            time.sleep(10 * (attempt + 1))
            continue
        release()
        if n != want or not verified(tmp):
            with lock:
                st['errors'] += 1
                st['last_error'] = f'bad zip {url_of(v)} {n}/{want}'
            tmp.unlink(missing_ok=True)
            continue
        os.replace(tmp, dest)
        ok_mark.write_text(str(n))
        with lock:
            st['done'] += 1
            st['bytes'] += n
            if v['reporter_slug'] in rank:
                st['first_done'][v['reporter_slug']] = st['first_done'].get(v['reporter_slug'], 0) + 1
        return
    with lock:
        st['failed'] += 1
    log('FAILED', url_of(v))


def sample_size():
    random.seed(20261108)
    picks = random.sample(vols, min(a.sample, len(vols)))

    def head(v):
        try:
            r = urllib.request.urlopen(urllib.request.Request(url_of(v), headers=UA, method='HEAD'), timeout=60)
            return int(r.headers.get('Content-Length') or 0)
        except Exception:  # noqa: BLE001
            return None
    with ThreadPoolExecutor(16) as ex:
        got = [s for s in ex.map(head, picks) if s]
    return sum(got) / len(got) * len(vols) if got else None


def monitor():
    t0, b0 = time.time(), 0
    last_ramp, last_rate = time.time(), 0.0
    window_start, window_bytes = time.time(), st['bytes']
    while True:
        time.sleep(10)
        now = time.time()
        with lock:
            rate = (st['bytes'] - window_bytes) / max(now - window_start, 1) / 1e6
            window_start, window_bytes = now, st['bytes']
            st['mbps'] = round(rate, 1)
            st['concurrency'] = sem_size[0]
            st['free_gb'] = round(free_gb(), 1)
            left = (st['volumes'] - st['done'] - st['skipped'] - st['failed'])
            mean = st['bytes'] / st['done'] if st['done'] else None
            if mean and rate > 0:
                st['eta_h'] = round(left * mean / 1e6 / rate / 3600, 2)
            if st['state'].startswith('paused') and free_gb() >= a.floor_gb:
                st['state'] = 'downloading'
            if now - last_ramp >= a.ramp:
                if errors_window[0]:
                    sem_size[0] = max(4, sem_size[0] // 2)
                    log('errors', errors_window[0], '-> concurrency', sem_size[0])
                elif rate >= last_rate * 1.05 and sem_size[0] < a.max:
                    sem_size[0] = min(a.max, sem_size[0] + a.step)
                    log('ramp ->', sem_size[0], f'{rate:.1f} MB/s')
                errors_window[0] = 0
                last_ramp, last_rate = now, rate
                with cond:
                    cond.notify_all()
            if stop_all[0]:
                st['state'] = 'STOPPED: ' + stop_all[0]
            if (D / 'STOP').exists():
                st['state'] = 'stopping (STOP file)'
            (D / 'status.json').write_text(json.dumps(st, indent=1), encoding='utf-8')


est = sample_size()
st['est_total_gb'] = round(est / 1e9, 1) if est else None
log('estimate', st['est_total_gb'], 'GB for', len(vols), 'volumes; free', round(free_gb(), 1))
if est and free_gb() - est / 1e9 < 40:
    st['state'] = f'not started: estimate {est / 1e9:.0f} GB would leave under 40 GB free'
    (D / 'status.json').write_text(json.dumps(st, indent=1), encoding='utf-8')
    raise SystemExit(st['state'])
st['state'] = 'downloading'
threading.Thread(target=monitor, daemon=True).start()
with ThreadPoolExecutor(a.max) as ex:
    list(ex.map(one, vols))
st['state'] = 'DONE' if not stop_all[0] else 'STOPPED: ' + stop_all[0]
st['finished'] = time.strftime('%Y-%m-%dT%H:%M:%S')
(D / 'status.json').write_text(json.dumps(st, indent=1), encoding='utf-8')
log('end', st['state'], st['done'], 'done', st['failed'], 'failed')
