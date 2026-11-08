"""Every case of the CAP bulk zips -> the record build_state.py reads (fetch_cap.record), filed by the repo it goes to
(cap_plan.route and plan.json's folds): <out>/<repo>/<reporter>/<volume>.jsonl.gz, one pass, one process per core,
nothing held beyond one volume. A volume already extracted is skipped (its marker <out>/_done/<reporter>~<volume>).
Decisions CAP files under a court that no longer sat that day are given the court that did (cap_court keeps CAP's).

  python cap_route_extract.py <cap-bulk dir> <out dir> [--reporters a,b]"""
import argparse
import gzip
import importlib.util
import json
import os
import zipfile
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, HERE / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


F = _load('fetch_cap', 'fetch_cap.py')
P = _load('cap_plan', 'cap_plan.py')
RENAMES = [('New York Court for the Correction of Errors', 'New York Court of Appeals', '1848-01-01'),
           ('Delaware Court of Errors and Appeals', 'Delaware Supreme Court', '1951-07-01')]
FOLD = {}


def init(fold):
    FOLD.update(fold)


def one(job):
    zp, out = job
    zp = Path(zp)
    rep, vol = zp.parent.name, zp.stem
    mark = Path(out) / '_done' / f'{rep}~{vol}'
    if mark.exists():
        return 0
    groups = defaultdict(list)
    with zipfile.ZipFile(zp) as z:
        for name in z.namelist():
            if not (name.startswith('json/') and name.endswith('.json')):
                continue
            d = json.loads(z.read(name))
            court, jur = (d.get('court') or {}).get('name') or '', (d.get('jurisdiction') or {}).get('name_long') or ''
            r = P.route(court, jur, d.get('decision_date') or '')
            if r is None:
                continue
            r = FOLD.get(r, r)
            rec = F.record(d, rep, vol)
            for old, new, since in RENAMES:
                if rec['court'] == old and (rec['decided'] or '') >= since:
                    rec['cap_court'], rec['court'] = old, new
            rec['first_page'] = d.get('first_page')
            if not rec['opinions'] and not rec['head_matter']:
                continue
            groups[r].append(rec)
    n = 0
    for r, recs in groups.items():
        dest = Path(out) / r / rep / f'{vol}.jsonl.gz'
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix('.part')
        with gzip.open(tmp, 'wt', encoding='utf-8', compresslevel=4) as fo:
            for rec in recs:
                fo.write(json.dumps(rec, ensure_ascii=False) + '\n')
        os.replace(tmp, dest)
        n += len(recs)
    mark.parent.mkdir(parents=True, exist_ok=True)
    mark.write_text(str(n))
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('bulk'), ap.add_argument('out'), ap.add_argument('--reporters', default='')
    a = ap.parse_args()
    fold = json.loads((Path(a.bulk) / 'plan.json').read_text(encoding='utf-8'))['folded']
    reps = set(filter(None, a.reporters.split(',')))
    jobs = [(str(z), a.out) for z in sorted((Path(a.bulk) / 'zips').glob('*/*.zip'))
            if z.with_suffix('.ok').exists() and (not reps or z.parent.name in reps)]
    total = 0
    with ProcessPoolExecutor(os.cpu_count(), initializer=init, initargs=(fold,)) as ex:
        for i, n in enumerate(ex.map(one, jobs, chunksize=8)):
            total += n
            if i % 2000 == 0:
                print(i, '/', len(jobs), 'volumes', total, 'cases', flush=True)
    print(json.dumps({'volumes': len(jobs), 'cases': total}), flush=True)


if __name__ == '__main__':
    main()
