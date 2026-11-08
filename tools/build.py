"""One US case-law repo from parsed records (CAP and CourtListener, same record shape), straight into git fast-import.

  python build.py <group> <parsed dir(s), comma-separated> <out repo dir> --today YYYY-MM-DD [--courts REGEX]

One commit per decision (Kind: court) dated the day decided (noon US Eastern, UTC-5), pre-1970 clamped to 1970-01-01
a second apart (In-force holds the real day); then a Lexifina documentation commit with README, REPRODUCE.md,
corpus.json, decision-index.tsv (reporter citations and docket numbers -> path), unresolved.csv and the judgment overlay
judgments/united-states/federal/usc/<title>/<section>.jsonl, each row pinned to the section's own version in the US Code
record on the day decided (reconcile.fix_row). Deterministic for the same input."""
from __future__ import annotations

import argparse
import gzip
import importlib.util
import json
import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import regex

HERE = Path(__file__).resolve().parent
_r = importlib.util.spec_from_file_location('us_reconcile', HERE.parent / 'nl-caselaw' / 'reconcile.py')
RC = importlib.util.module_from_spec(_r)
_r.loader.exec_module(RC)

LAW_CORPUS = 'united-states/federal/usc'
EASTERN = timezone(timedelta(hours=-5))
COMMITTER = b'Lexifina <corpus@lexifina.com>'
EPOCH_NOON = 17 * 3600  # 1970-01-01 12:00 UTC-5
ISO = re.compile(r'^\d{4}-\d{2}-\d{2}$')
CIRCUITS = {'ca1': 'First', 'ca2': 'Second', 'ca3': 'Third', 'ca4': 'Fourth', 'ca5': 'Fifth', 'ca6': 'Sixth', 'ca7': 'Seventh',
            'ca8': 'Eighth', 'ca9': 'Ninth', 'ca10': 'Tenth', 'ca11': 'Eleventh'}
GROUPS = {'supreme-court': ('the Supreme Court of the United States', r'^Supreme Court of the United States$')}
# CAP's court names carry a few typos ("for the Ninth", "Six Circuit", "Second Circuitt"): matched too (2026-11-06).
_TYPO = {'Sixth': '(?:Sixth|Six)'}
for k, n in CIRCUITS.items():
    GROUPS[k] = (f'the United States Court of Appeals for the {n} Circuit',
                 rf'^United States (?:Circuit )?Court of Appeals for the {_TYPO.get(n, n)}(?: Circuitt?)?$')
GROUPS['cadc'] = ('the United States Court of Appeals for the District of Columbia Circuit',
                  r'^United States Court of Appeals(?: for the|,) District of Columbia(?: Circuit)?$'
                  r'|^Court of Appeals of (?:the )?District of Columbia$')
GROUPS['cafc'] = ('the United States Court of Appeals for the Federal Circuit', r'^United States Court of Appeals for the Federal Circuit$')
# "11 U.S.C. § 1182", "28 U.S.C. §§ 1331, 1332", "42 U.S.C.A. § 1983", "15 USC 78j(b)"
USC = regex.compile(r'\b(\d{1,2})\s*U\.?\s?S\.?\s?C\.?(?:A\.?|S\.?)?\s*(?:§§?|[Ss]ec(?:tions?|s?\.)|[Ss]ections?)?\s*'
                    r'((?:\d+[a-z]?(?:-\d+)?(?:\([a-zA-Z0-9]+\))*)(?:\s*(?:,|and|or|&)\s*\d+[a-z]?(?:-\d+)?(?:\([a-zA-Z0-9]+\))*){0,12})')
SEC = re.compile(r'\d+[a-z]?(?:-\d+)?')


def slug(s: str) -> str:
    return re.sub(r'-{2,}', '-', re.sub(r'[^A-Za-z0-9]+', '-', s)).strip('-')[:100] or 'x'


def usc_versions(law: Path, ref: str = 'main') -> dict:
    """path -> ([days], [shas]) for every file of the US Code record. Its first commit is the 2017 edition with no
    In-force trailer (a starting text), so a commit with no In-force day is dated by its author date; reconcile.versions
    would skip it and leave 880 of the 1,010 sections unheld."""
    out = subprocess.run(['git', '-C', str(law), 'log', ref, '--format=%x00%H %ad %(trailers:key=In-force,valueonly,separator=%x2C)',
                          '--date=short', '--name-only', '--no-renames'], capture_output=True, text=True, encoding='utf-8').stdout
    ver: dict = {}
    for block in out.split('\x00')[1:]:
        head, _, names = block.partition('\n')
        sha, ad, *rest = head.split(' ')
        d = next((x.strip()[:10] for x in ' '.join(rest).split(',') if ISO.match(x.strip()[:10])), ad[:10])
        for n in names.split('\n'):
            if n.endswith('.md') and n.startswith('title-'):
                ver.setdefault(n, []).append((d, sha))
    res = {}
    for p, v in ver.items():
        v.sort()
        res[p] = ([x[0] for x in v], [x[1] for x in v])
    return res


def usc_cites(text: str, have: set) -> list[tuple[str, str]]:
    out = []
    for m in USC.finditer(text, timeout=2.0) if text else []:
        title = m.group(1)
        for s in SEC.findall(m.group(2)):
            p = f'title-{int(title)}/{s}.md'
            if p in have:
                out.append((p, re.sub(r'\s+', ' ', m.group(0)).strip()[:200]))
    return out


def body_of(r: dict) -> str:
    parts = []
    if (r.get('head_matter') or '').strip():
        parts += ['## Head matter', '', r['head_matter'].strip(), '']
    for o in r.get('opinions') or []:
        head = (o.get('type') or 'opinion').replace('-', ' ').capitalize()
        if o.get('author'):
            head += f" ({o['author'].strip()})"
        parts += [f'## {head}', '', (o.get('text') or '').strip(), '']
    return '\n'.join(parts)


def text_length(r: dict) -> int:
    return sum(len(o.get('text') or '') for o in r.get('opinions') or []) + len(r.get('head_matter') or '')


def decision_type(r: dict) -> str:
    """How a reader tells a U.S. Reports entry apart, by the length of its text: the reporter prints every memorandum
    order (certiorari denied, dismissed, rehearing denied) as its own one-line entry, and CAP keeps each as a case."""
    n = text_length(r)
    return 'memorandum order' if n < 300 else 'per curiam' if n < 1000 else 'opinion'


def render(r: dict, day: str, have: set, ver: dict, extra: tuple = ()):
    """(path, file bytes, author, email, message bytes, [(law path, overlay row)], [index refs], cites) for one decision."""
    court = r.get('court') or 'Court'
    cite = r.get('official') or (r['citations'][0] if r.get('citations') else None)
    name = (r.get('name_abbreviation') or r.get('name') or cite or str(r['id'])).strip()
    key = slug(cite) if cite else f"cap-{r['id']}"
    path = f"decisions/{slug(court).lower()[:50]}/{day[:4]}/{key}.md"
    docket = re.sub(r'^\s*(?:Nos?\.|Docket\s+No\.?)\s*', '', r.get('docket') or '').strip()
    text = body_of(r)
    found = usc_cites(text, have)
    fm = {'court': court, 'decision_type': decision_type(r) if court == 'Supreme Court of the United States' else None,
          'docket': docket or None, 'decided': r.get('decided'), 'citations': r.get('citations') or None,
          'judges': r.get('judges') or None, 'parties': r.get('parties') or None, 'source_url': r.get('source_url'),
          'source': r.get('source') or 'Caselaw Access Project (Harvard Law School Library), CC0', 'cap_id': r.get('id')}
    lines = ['---'] + [f'{k}: {json.dumps(v, ensure_ascii=False)}' for k, v in fm.items() if v not in (None, '', [])]
    if r.get('cites_to'):
        lines.append('cites_cases: ' + json.dumps(sorted(set(r['cites_to']))[:500], ensure_ascii=False))
    uniq = sorted({p for p, _ in found})
    if uniq:
        lines.append('cites_law: ' + json.dumps([f'{LAW_CORPUS}/{p}' for p in uniq], ensure_ascii=False))
    lines += ['---', '', f'# {name}', '', text.strip(), '']
    body = '\n'.join(lines).replace('\r\n', '\n').encode('utf-8')
    author = f"{court} {cite or docket or r['id']}".replace('<', '').replace('>', '')[:200]
    email = f"{slug(cite or docket or str(r['id'])).lower()[:60]}@caselaw.invalid"
    kind = 'correction' if any(t.startswith('Corrected:') for t in extra) else 'court'
    tr = [f'Kind: {kind}', f'In-force: {day}', f'Decided: {day}', f'Court: {court}']
    if docket:
        tr.append(f'Docket: {docket}')
    for c in (r.get('citations') or [])[:6]:
        tr.append(f'Citation: {c}')
    tr.append(f"Source: {r.get('source_url')}")
    seen = set()
    for p, how in found:
        if p not in seen and len(seen) < 60:
            seen.add(p)
            tr.append(f'Cites: {LAW_CORPUS}/{p} ({how[:120]})')
    tr += [t for t in extra if t and not t.rstrip().endswith(':')]
    msg = (f'{name}, {cite or docket}'[:200] + '\n\n' + '\n'.join(tr) + '\n').encode('utf-8')
    counts = {}
    for p, _ in found:
        counts[p] = counts.get(p, 0) + 1
    rows, done = [], set()
    for p, how in found:
        if p in done:
            continue
        done.add(p)
        row = {'decided': day, 'name': name, 'court': court, 'citation': cite or docket or str(r['id']),
               'url': r.get('source_url') or '', 'how': how, 'mentions': counts[p], 'decision': path}
        rows.append((p, RC.fix_row(row, p, ver)))
    refs = sorted({*(r.get('citations') or []), *( [docket] if docket else []), f"cap-{r['id']}"})
    return path, body, author, email, msg, rows, refs


def dedupe(recs: list[dict]) -> list[dict]:
    """One record per case: CAP sometimes carries the same case twice (the same day, docket and name, e.g. printed both in
    an orders list and with its opinion; 209 in the U.S. Reports, 2,110 in the Federal Reporter series). Keep the record
    with the most text and give it every citation and cited case of the others."""
    groups: dict[tuple, list[dict]] = {}
    out = []
    for r in recs:
        d = re.sub(r'^\s*(?:Nos?\.|Docket\s+No\.?)\s*', '', r.get('docket') or '').strip()
        if not d:
            out.append(r)
            continue
        groups.setdefault(((r.get('decided') or '')[:10], d, (r.get('name_abbreviation') or '').strip().lower()), []).append(r)
    for g in groups.values():
        if len(g) == 1:
            out.append(g[0])
            continue
        g.sort(key=lambda r: (-sum(len(o.get('text') or '') for o in r.get('opinions') or []) - len(r.get('head_matter') or ''), str(r['id'])))
        keep = dict(g[0])
        cites = list(keep.get('citations') or [])
        for o in g[1:]:
            cites += [c for c in o.get('citations') or [] if c not in cites]
            keep['cites_to'] = sorted(set((keep.get('cites_to') or []) + (o.get('cites_to') or [])))
        keep['citations'] = cites
        keep['merged_cap_ids'] = [o['id'] for o in g[1:]]
        out.append(keep)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('group'), ap.add_argument('parsed'), ap.add_argument('out')
    ap.add_argument('--today', required=True), ap.add_argument('--courts', default='')
    ap.add_argument('--law-repo', default=str(Path.home() / 'Documents' / 'caselaw-law-mirrors' / 'united-states-law.git'))
    ap.add_argument('--chunk-commits', type=int, default=40000)
    a = ap.parse_args()
    where, court_rx = GROUPS[a.group]
    court_rx = re.compile(a.courts or court_rx)
    out = Path(a.out)
    ver = usc_versions(Path(a.law_repo), ref='main')
    have = set(ver)
    recs, seen_ids = [], set()
    for d in a.parsed.split(','):
        for f in sorted(Path(d).rglob('*.jsonl.gz')):
            with gzip.open(f, 'rt', encoding='utf-8') as fh:
                for line in fh:
                    r = json.loads(line)
                    if not court_rx.search(r.get('court') or '') or r['id'] in seen_ids:
                        continue
                    seen_ids.add(r['id'])
                    recs.append(r)
    recs = dedupe(recs)
    recs.sort(key=lambda r: ((r.get('decided') or '9999')[:10], r.get('official') or '', str(r['id'])))
    print(f'{a.group}: {len(recs)} decisions', file=sys.stderr, flush=True)
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f'{out} is not empty')
    out.mkdir(parents=True, exist_ok=True)
    subprocess.run(['git', 'init', '-q', '-b', 'main', str(out)], check=True)
    fi = subprocess.Popen(['git', '-C', str(out), 'fast-import', '--quiet', '--done', f'--export-marks={out / ".git" / "marks"}',
                          '--big-file-threshold=64m'], stdin=subprocess.PIPE)
    w = fi.stdin.write
    overlay, dindex, used, undated = {}, [], set(), []
    mark = clamp = 0
    checkpoints, first, raw = [], None, 0
    for r in recs:
        day = (r.get('decided') or '')[:10]
        if len(day) == 7:
            day += '-01'
        if len(day) == 4:
            day += '-01-01'
        if not ISO.match(day) or day > a.today:
            undated.append(r)
            continue
        first = first or day
        path, body, author, email, msg, rows, refs = render(r, day, have, ver)
        if path in used:
            path = path[:-3] + f"--cap-{r['id']}.md"
            body = body  # same text; only the file name differs
        used.add(path)
        dindex += [x + '\t' + path for x in refs]
        raw += len(body)
        d0 = date.fromisoformat(day)
        if d0.year < 1970:
            clamp += 1
            ts = EPOCH_NOON + clamp
        else:
            ts = int(datetime(d0.year, d0.month, d0.day, 12, tzinfo=EASTERN).timestamp())
        mark += 1
        w(b'commit refs/heads/main\nmark :%d\n' % mark)
        w(b'author %s <%s> %d -0500\ncommitter %s %d -0500\n' % (author.encode('utf-8'), email.encode(), ts, COMMITTER, ts))
        w(b'data %d\n%s\n' % (len(msg), msg))
        w(b'M 100644 inline %s\ndata %d\n%s\n' % (path.encode('utf-8'), len(body), body))
        if mark % a.chunk_commits == 0:
            checkpoints.append(mark)
        for p, row in rows:
            row['decision'] = path
            overlay.setdefault(p, []).append(row)
    docs = {}
    for p, rows in overlay.items():
        rows.sort(key=lambda x: (x['decided'], x['citation']))
        docs[f'judgments/{LAW_CORPUS}/{p[:-3]}.jsonl'] = ''.join(json.dumps(x, ensure_ascii=False, sort_keys=True) + '\n' for x in rows)
    docs['decision-index.tsv'] = 'reference\tpath\n' + ''.join(x + '\n' for x in sorted(set(dindex)))
    for r in undated:
        docs[f"decisions/undated/cap-{r['id']}.md"] = f"---\ncap_id: {r['id']}\nsource_url: {json.dumps(r.get('source_url'))}\n---\n\n# {r.get('name_abbreviation') or r['id']}\n\n{body_of(r)}\n"
    n_refs = sum(len(v) for v in overlay.values())
    gl = f'united-states/{a.group}/caselaw'
    types_line = ''
    if a.group == 'supreme-court':
        kinds = {}
        for r in recs:
            if r not in undated:
                kinds[decision_type(r)] = kinds.get(decision_type(r), 0) + 1
        types_line = (f"\nThe U.S. Reports print every order as its own entry, and each is kept here: "
                      f"{kinds.get('memorandum order', 0):,} memorandum orders (certiorari denied, dismissed, rehearing denied; under 300 "
                      f"characters), {kinds.get('per curiam', 0):,} short per curiam decisions and orders (300 to 999 characters) and "
                      f"{kinds.get('opinion', 0):,} opinions (1,000 characters or more). Each file's `decision_type` says which.\n")
    docs['corpus.json'] = json.dumps({
        'corpus': f'us/{a.group}/caselaw', 'path': gl, 'name': f'Case law: United States, {where}', 'label': f'United States case law ({where})',
        'language': 'en', 'kind': 'judgments', 'history': 'in-force',
        'coverage': f'{mark} published opinions with text, one file and one commit each, dated the day decided; {n_refs} citations of the US Code linked to united-states/federal/usc.',
        'history_from': first, 'record_to': a.today, 'checked_on': a.today,
        'record_note': 'Court decisions, not laws: every commit is Kind: court. In-force is the day decided. Commits before 1970 are dated 1970-01-01, a second apart; In-force holds the real day. The US Code record holds only some titles, so most statute citations are not linked.',
        'in_force_trailer': 'In-force', 'correction_trailer': 'Corrected',
        'dates': {'commit': 'decided', 'time': '12:00 UTC-5'},
        'source': {'publisher': 'Caselaw Access Project (Harvard Law School Library)', 'url': 'https://static.case.law/', 'licence': 'CC0 1.0'},
        'overlay': {'path': f'judgments/{LAW_CORPUS}/<title>/<section>.jsonl', 'law_corpus': LAW_CORPUS},
    }, ensure_ascii=False, indent=2) + '\n'
    docs['README.md'] = f"""# US case law: {where}

Part of [lexifina/global-law](https://github.com/lexifina/global-law), one git history for every country's law: this is the case law of {where}, United States, which global-law reads at `{gl}/`.

Every published opinion of {where} in the Caselaw Access Project, as a git history: one file per decision, one commit
per decision, dated the day it was decided. {mark:,} decisions from {first} to the end of the source.
{types_line}
## Questions as git commands

| Question | Command |
|---|---|
| A decision by its citation | `grep -P '^410 U.S. 113\\t' decision-index.tsv` then `cat <path>` |
| Decisions given in a window | `git log --since=1973-01-01 --until=1973-01-31 --format='%ad %an' --date=short` |
| Decisions citing 11 U.S.C. § 1182 | `cat judgments/united-states/federal/usc/title-11/1182.jsonl` |
| The section as the court applied it | each overlay row's `law_commit` is the section's own version in the US Code record on the day decided (or `law_record_from` when the decision is older than the record) |
| Who wrote a line? | `git blame decisions/...`: each line is credited to the decision (court and citation) |

## Dates and authors
The commit date is the day the decision was given, at noon US Eastern (`In-force:` and `Decided:`). Git cannot hold dates
before 1970, so earlier decisions are committed on 1970-01-01, a second apart, and `In-force:` holds the real day. The
author is the court and the official citation, at an address that cannot exist. These are court rulings, not laws: every
commit is `Kind: court`.

## Source
The Caselaw Access Project's bulk data (Harvard Law School Library), dedicated to the public domain under CC0; court
opinions are not copyrightable (17 U.S.C. § 105; Georgia v. Public.Resource.Org, 2020). Each file keeps its source URL.
Text is CAP's OCR, unchanged.

## What it does not show
Opinions after the end of CAP's coverage (added from CourtListener where noted), unpublished decisions not in the
reporters, and statute citations to US Code titles the record does not hold.
"""
    docs['REPRODUCE.md'] = f"""# Reproducing this repository

```
python tools/fetch_cap.py <cache> us f f2d f3d f-appx
python tools/build.py {a.group} <cache>/<reporter>,... <empty folder> --today {a.today}
```

Input: static.case.law reporter volume zips (CC0). Gives {mark:,} decision commits and one documentation commit.
"""
    for f in ('fetch_cap.py', 'build.py'):
        docs[f'tools/{f}'] = (HERE / f).read_text(encoding='utf-8')
    ts = int(datetime.fromisoformat(a.today + 'T12:00:00-05:00').timestamp())
    msg = f'Documentation: README, REPRODUCE, corpus.json, decision index and the judgment overlay ({n_refs} citations)\n'.encode()
    w(b'commit refs/heads/main\nauthor Lexifina <corpus@lexifina.com> %d -0500\ncommitter %s %d -0500\n' % (ts, COMMITTER, ts))
    w(b'data %d\n%s\n' % (len(msg), msg))
    for p in sorted(docs):
        b = docs[p].encode('utf-8')
        w(b'M 100644 inline %s\ndata %d\n%s\n' % (p.encode('utf-8'), len(b), b))
    w(b'done\n')
    fi.stdin.close()
    if fi.wait():
        raise SystemExit('fast-import failed')
    marks = dict(l.split() for l in (out / '.git' / 'marks').read_text().splitlines())
    (out / '.git' / 'push-checkpoints').write_text(''.join(marks[f':{m}'] + '\n' for m in checkpoints))
    print(json.dumps({'group': a.group, 'decisions': mark, 'undated': len(undated), 'overlay_rows': n_refs,
                      'raw_gb': round(raw / 1e9, 2), 'checkpoints': len(checkpoints)}), flush=True)


if __name__ == '__main__':
    main()
