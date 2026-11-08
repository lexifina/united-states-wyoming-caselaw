"""CAP bulk volume zips (cap_bulk_fetch.py) -> the records build.py reads (fetch_cap.record), one gzip JSONL per volume,
for the courts asked, in parallel (one process per core). Skips volumes already extracted and zips not yet verified.

  python cap_bulk_extract.py <cap-bulk dir> <out dir> --reporters cal,cal-2d --courts REGEX [--after YYYY-MM-DD]"""
import argparse
import gzip
import importlib.util
import json
import os
import re
import zipfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
_f = importlib.util.spec_from_file_location('fetch_cap', HERE / 'fetch_cap.py')
F = importlib.util.module_from_spec(_f)
_f.loader.exec_module(F)


def one(job):
    zp, out, reporter, volume, courts, after, rename = job
    dest = Path(out) / reporter / f'{volume}.jsonl.gz'
    if dest.exists():
        return 0
    rx = re.compile(courts)
    n = 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix('.part')
    with zipfile.ZipFile(zp) as z, gzip.open(tmp, 'wt', encoding='utf-8', compresslevel=5) as fo:
        for name in z.namelist():
            if not (name.startswith('json/') and name.endswith('.json')):
                continue
            d = json.loads(z.read(name))
            if not rx.search((d.get('court') or {}).get('name') or ''):
                continue
            if after and (d.get('decision_date') or '') <= after:
                continue
            r = F.record(d, reporter, volume)
            old, new, since = rename or (None, None, None)
            if old and r['court'] == old and (r['decided'] or '') >= since:
                # a CAP mislabel: the court a decision is filed under had been abolished on that day
                r['cap_court'], r['court'] = r['court'], new
            r['first_page'] = d.get('first_page')
            r['ocr_confidence'] = (d.get('analysis') or {}).get('ocr_confidence')
            if not r['opinions'] and not r['head_matter']:
                continue
            fo.write(json.dumps(r, ensure_ascii=False) + '\n')
            n += 1
    os.replace(tmp, dest)
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('bulk'), ap.add_argument('out')
    ap.add_argument('--reporters', required=True), ap.add_argument('--courts', required=True), ap.add_argument('--after', default='')
    ap.add_argument('--rename', default='', help='OLD|NEW|SINCE: CAP files decisions of NEW under OLD from SINCE on')
    a = ap.parse_args()
    jobs = []
    for rep in a.reporters.split(','):
        for zp in sorted((Path(a.bulk) / 'zips' / rep).glob('*.zip')):
            if zp.with_suffix('.ok').exists():
                jobs.append((str(zp), a.out, rep, zp.stem, a.courts, a.after, tuple(a.rename.split('|')) if a.rename else None))
    with ProcessPoolExecutor(os.cpu_count()) as ex:
        total = sum(ex.map(one, jobs, chunksize=4))
    print(json.dumps({'volumes': len(jobs), 'cases': total}))


if __name__ == '__main__':
    main()
