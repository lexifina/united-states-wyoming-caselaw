"""Every case of the CAP bulk zips -> the record build_state.py reads (fetch_cap.record), filed by the repo it goes to
(cap_plan.route and plan.json's folds): <out>/<repo>/<reporter>/<volume>.jsonl.gz, one pass, one process per core,
nothing held beyond one volume. Decisions CAP files under a court that no longer (or not yet) sat that day, or under no
name, are given the court that did (cap_plan.court_name; cap_court keeps CAP's).

A volume's marker <out>/_done/<reporter>/<volume>.json is JSON: {"repos": {repo: records written}, "empty": {repo: cases
left out for having no opinion text and no head matter}}. A volume is skipped only when its marker can be trusted:
every file it names exists and holds that many records, and no other repo holds a file of that volume. Otherwise the
volume is extracted again (its stale files removed first). 2026-11-09: 80 volumes had markers but no files (12,440
decisions with text never reached their repos: the old marker names <reporter>~<volume> are Windows 8.3 short names,
so "alaska~1" opened the marker "alaska~12", and volumes 1-4 of every reporter with ten or more volumes were skipped
as done). Markers are now one folder per reporter, named <volume>.json, which no short name can stand for.

  python cap_route_extract.py <cap-bulk dir> <out dir> [--reporters a,b] [--volumes rep~vol,...] [--force]
  python cap_route_extract.py <cap-bulk dir> <out dir> --verify      (check every marker, extract nothing)"""
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
RENAMES = P.RENAMES
FOLD = {}


def init(fold):
    FOLD.update(fold)


def count_lines(f: Path) -> int:
    with gzip.open(f, 'rb') as fh:
        return sum(1 for _ in fh)


def marker_path(out: Path, rep: str, vol: str) -> Path:
    return Path(out) / '_done' / rep / f'{vol}.json'


def marker_ok(mark: Path, out: Path, have: list[str]) -> bool:
    """Whether a volume's marker matches its files: have = the repos that hold a file of the volume."""
    if not mark.exists():
        return False
    rep, vol = mark.parent.name, mark.name[:-5]
    try:
        m = json.loads(mark.read_text())
    except ValueError:
        return False
    if isinstance(m, int):  # an old marker: the total only; when the files hold it, rewritten per repo
        counts = {r: count_lines(out / r / rep / f'{vol}.jsonl.gz') for r in have}
        if sum(counts.values()) != m:
            return False
        mark.write_text(json.dumps({'repos': dict(sorted(counts.items())), 'empty': None}))
        return True
    want = {r: n for r, n in (m.get('repos') or {}).items() if n}
    if set(want) != set(have):
        return False
    return all(count_lines(out / r / rep / f'{vol}.jsonl.gz') == n for r, n in want.items())


def one(job):
    zp, out, have, force = job
    zp, out = Path(zp), Path(out)
    rep, vol = zp.parent.name, zp.stem
    mark = marker_path(out, rep, vol)
    if not force and marker_ok(mark, out, have):
        return 0, False
    for r in have:  # stale or partial files of this volume: written again below
        (out / r / rep / f'{vol}.jsonl.gz').unlink(missing_ok=True)
    groups, empty = defaultdict(list), defaultdict(int)
    with zipfile.ZipFile(zp) as z:
        for name in z.namelist():
            if not (name.startswith('json/') and name.endswith('.json')):
                continue
            d = json.loads(z.read(name))
            c = d.get('court') or {}
            day = d.get('decision_date') or ''
            court, cap = P.court_name(c.get('name') or '', c.get('id'), day)
            jur = (d.get('jurisdiction') or {}).get('name_long') or ''
            r = P.route(court, jur, day, rep)
            if r is None:
                continue
            r = FOLD.get(r, r)
            rec = F.record(d, rep, vol)
            if cap is not None:
                rec['cap_court'], rec['court'] = cap, court
            rec['first_page'] = d.get('first_page')
            if not rec['opinions'] and not rec['head_matter']:
                empty[r] += 1
                continue
            groups[r].append(rec)
    n = 0
    for r, recs in groups.items():
        dest = out / r / rep / f'{vol}.jsonl.gz'
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix('.part')
        with gzip.open(tmp, 'wt', encoding='utf-8', compresslevel=4) as fo:
            for rec in recs:
                fo.write(json.dumps(rec, ensure_ascii=False) + '\n')
        os.replace(tmp, dest)
        n += len(recs)
    mark.parent.mkdir(parents=True, exist_ok=True)
    mark.write_text(json.dumps({'repos': {r: len(v) for r, v in sorted(groups.items())}, 'empty': dict(sorted(empty.items()))}))
    return n, True


def verify_one(job):
    zp, out, have, _ = job
    zp, out = Path(zp), Path(out)
    return f'{zp.parent.name}~{zp.stem}', marker_ok(marker_path(out, zp.parent.name, zp.stem), out, have)


def holdings(out: Path) -> dict:
    """(reporter, volume) -> the repos holding a file of it: one walk of the output."""
    have = defaultdict(list)
    for r in os.scandir(out):
        if not r.is_dir() or r.name.startswith('_'):
            continue
        for rep in os.scandir(r.path):
            if rep.is_dir():
                for f in os.scandir(rep.path):
                    if f.name.endswith('.jsonl.gz'):
                        have[(rep.name, f.name[:-9])].append(r.name)
    return have


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('bulk'), ap.add_argument('out'), ap.add_argument('--reporters', default='')
    ap.add_argument('--volumes', default=''), ap.add_argument('--force', action='store_true'), ap.add_argument('--verify', action='store_true')
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    try:  # heavy: below normal (the pool's processes inherit it)
        import psutil
        psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
    except Exception:  # noqa: BLE001
        pass
    fold = json.loads((Path(a.bulk) / 'plan.json').read_text(encoding='utf-8'))['folded']
    reps = set(filter(None, a.reporters.split(',')))
    vols = set(filter(None, a.volumes.split(',')))
    have = holdings(out)
    jobs = [(str(z), a.out, have.get((z.parent.name, z.stem), []), a.force) for z in sorted((Path(a.bulk) / 'zips').glob('*/*.zip'))
            if z.with_suffix('.ok').exists() and (not reps or z.parent.name in reps) and (not vols or f'{z.parent.name}~{z.stem}' in vols)]
    if a.verify:
        bad = []
        with ProcessPoolExecutor(os.cpu_count()) as ex:
            for v, ok in ex.map(verify_one, jobs, chunksize=16):
                if not ok:
                    bad.append(v)
        print(json.dumps({'volumes': len(jobs), 'untrusted_markers': len(bad), 'examples': bad[:50]}), flush=True)
        raise SystemExit(1 if bad else 0)
    total = redone = 0
    with ProcessPoolExecutor(os.cpu_count(), initializer=init, initargs=(fold,)) as ex:
        for i, (n, did) in enumerate(ex.map(one, jobs, chunksize=8)):
            total += n
            redone += did
            if i % 2000 == 0:
                print(i, '/', len(jobs), 'volumes', total, 'cases', flush=True)
    print(json.dumps({'volumes': len(jobs), 'extracted': redone, 'cases': total}), flush=True)


if __name__ == '__main__':
    main()
