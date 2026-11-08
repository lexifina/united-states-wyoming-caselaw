"""Caselaw Access Project (static.case.law, CC0) -> gzip JSONL per reporter volume, streaming: each volume zip is
downloaded, its case JSON read in memory, the cases written to <out>/<reporter>/<volume>.jsonl.gz, the zip deleted.
Resumable (a finished volume is skipped). Refuses to start a download when C: has under --floor-gb free.

  python fetch_cap.py <out> <reporter> [<reporter> ...] [--workers 12] [--floor-gb 12] [--courts REGEX]

A record: id, name, name_abbreviation, decided, docket, citations [cite], official, court, court_abbr, jurisdiction,
judges, parties, attorneys, head_matter, opinions [{type, author, text}], cites_to [cite], source_url, reporter, volume."""
from __future__ import annotations

import argparse
import gzip
import io
import json
import re
import shutil
import sys
import time
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BASE = 'https://static.case.law'
UA = {'User-Agent': 'lexifina-gitlaw (case law archive)'}


def get(url: str, tries: int = 6) -> bytes:
    for i in range(tries):
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=300).read()
        except Exception as e:  # transient: back off
            err = e
            time.sleep(5 * (i + 1))
    raise err


def volumes(reporter: str) -> list[str]:
    html = get(f'{BASE}/{reporter}/').decode('utf-8', 'replace')
    return sorted(set(re.findall(rf"{re.escape(BASE)}/{re.escape(reporter)}/([^/']+)\.zip", html)),
                  key=lambda v: (len(v), v))


def free_gb() -> float:
    return shutil.disk_usage('C:/').free / 1e9


def record(d: dict, reporter: str, volume: str) -> dict:
    cb = d.get('casebody') or {}
    if isinstance(cb, dict) and 'data' in cb and isinstance(cb['data'], dict):
        cb = cb['data']
    cites = [c.get('cite') for c in (d.get('citations') or []) if isinstance(c, dict) and c.get('cite')]
    official = next((c.get('cite') for c in (d.get('citations') or []) if isinstance(c, dict) and c.get('type') == 'official'), None)
    court = d.get('court') or {}
    juris = d.get('jurisdiction') or {}
    ops = []
    for o in cb.get('opinions') or []:
        if isinstance(o, dict) and (o.get('text') or '').strip():
            ops.append({'type': o.get('type'), 'author': o.get('author'), 'text': o.get('text')})
    return {'id': d.get('id'), 'name': d.get('name'), 'name_abbreviation': d.get('name_abbreviation'),
            'decided': d.get('decision_date'), 'docket': d.get('docket_number'), 'citations': cites, 'official': official or (cites[0] if cites else None),
            'court': court.get('name'), 'court_abbr': court.get('name_abbreviation'), 'jurisdiction': juris.get('name_long'),
            'judges': cb.get('judges') or [], 'parties': cb.get('parties') or [], 'attorneys': cb.get('attorneys') or [],
            'head_matter': cb.get('head_matter') or '', 'opinions': ops,
            'cites_to': [c.get('cite') for c in (d.get('cites_to') or []) if isinstance(c, dict) and c.get('cite')],
            'source_url': f"{BASE}/{reporter}/{volume}/cases/{d.get('file_name')}.json", 'reporter': reporter, 'volume': volume}


def one(args) -> tuple[str, str, int]:
    out, reporter, volume, courts, floor = args
    dest = Path(out) / reporter / f'{volume}.jsonl.gz'
    if dest.exists():
        return reporter, volume, -1
    while free_gb() < floor:
        print(f'disk floor: {free_gb():.1f} GB free < {floor}; waiting', flush=True)
        time.sleep(60)
    z = zipfile.ZipFile(io.BytesIO(get(f'{BASE}/{reporter}/{volume}.zip')))
    n = 0
    tmp = dest.with_suffix('.part')
    dest.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(tmp, 'wt', encoding='utf-8', compresslevel=5) as fo:
        for name in z.namelist():
            if not (name.startswith('json/') and name.endswith('.json')):
                continue
            d = json.loads(z.read(name))
            r = record(d, reporter, volume)
            if courts and not re.search(courts, r['court'] or ''):
                continue
            if not r['opinions'] and not r['head_matter']:
                continue
            fo.write(json.dumps(r, ensure_ascii=False) + '\n')
            n += 1
    tmp.replace(dest)
    return reporter, volume, n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('out'), ap.add_argument('reporters', nargs='+')
    ap.add_argument('--workers', type=int, default=12), ap.add_argument('--floor-gb', type=float, default=12)
    ap.add_argument('--courts', default='')
    a = ap.parse_args()
    jobs = []
    for rep in a.reporters:
        vs = volumes(rep)
        print(rep, len(vs), 'volumes', flush=True)
        jobs += [(a.out, rep, v, a.courts, a.floor_gb) for v in vs]
    done = total = 0
    with ThreadPoolExecutor(a.workers) as ex:
        for rep, vol, n in ex.map(one, jobs):
            done += 1
            total += max(n, 0)
            if done % 50 == 0:
                print(f'{done}/{len(jobs)} volumes, {total} cases, {free_gb():.1f} GB free', flush=True)
    print('DONE', done, 'volumes', total, 'cases', flush=True)


if __name__ == '__main__':
    main()
