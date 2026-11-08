"""One US state case-law repo (united-states-<state>-caselaw, served at united-states/<state>/caselaw) from CAP bulk
records (cap_bulk_extract.py), straight into git fast-import: build.py's format for the federal courts, with the
publisher's head matter left out (syllabi, headnotes and counsel lists written by the reporter's publisher, not the
court) and each decision's reporter and precedential status in its front matter.

  python build_state.py <state> <parsed dir(s), comma-separated> <out repo dir> --today YYYY-MM-DD
  python build_state.py <plan key> <parsed dir> <out> --today ... --plan plan.json [--era 1934-1991] [--append]
A plan key (cap_plan.py) names any CAP court group; --era builds the decisions of those years only (a court split by
era); --append adds to an existing repo the decisions it does not hold (not its CAP ids, nor the same day and name),
as new commits on its main, then a documentation commit: append-only, nothing rewritten.

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


# The states built so far: (who, the court's name in CAP, the reporters read, what the head matter is and who wrote it).
STATES = {
    'california': ('the Supreme Court of California', r'^Supreme Court of California$',
                   'California Reports (Cal., Cal. 2d-5th; later series published by Bancroft-Whitney, West and LexisNexis under contract)'),
    'new-york': ('the New York Court of Appeals', r'^(?:New York )?Court of Appeals(?: of (?:the State of )?New York)?$|^New York Court of Appeals$',
                 'New York Reports (N.Y., N.Y.2d; published under contract) and, after N.Y.2d, the North Eastern Reporter (West)'),
    'delaware': ('the Supreme Court of Delaware', r'^(?:Supreme Court of Delaware|Delaware Supreme Court|Delaware Court of Errors and Appeals|Court of Errors and Appeals of Delaware)$',
                 'Delaware Reports (to 1966) and, after them, the Atlantic Reporter (West)'),
    'massachusetts': ('the Supreme Judicial Court of Massachusetts', r'^(?:Massachusetts )?Supreme Judicial Court(?: of Massachusetts)?$',
                      'Massachusetts Reports (Mass., 1804-2017, with its nominative reporters)'),
    'nevada': ('the Supreme Court of Nevada', r'^Supreme Court of Nevada$', 'Nevada Reports (Nev., 1865-2013)'),
}


def body_of(r: dict) -> str:
    """The opinions only: CAP's head matter (syllabus, headnotes, counsel, the reporter's summary) is the publisher's
    text in these reporters, not the court's, so it is left out (US case-law sizing, 2026-11-07)."""
    parts = []
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
    fm = {'court': court, 'docket': docket or None, 'decided': r.get('decided'), 'citations': r.get('citations') or None,
          'reporter': r.get('reporter'), 'precedential_status': 'published',
          'precedential_note': 'printed in a reporter; CAP records no other status',
          'cap_court': r.get('cap_court'),  # the court CAP files it under, where that court no longer sat that day
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


def norm_name(name: str) -> str:
    return re.sub(r'[^a-z0-9]+', ' ', name.lower()).strip()


REPORTERS = {}


def reporters_of(slugs) -> str:
    if not REPORTERS:
        f = Path.home() / 'Documents' / '.caselaw-cache' / 'us' / 'cap-bulk' / 'meta' / 'ReportersMetadata.json'
        try:
            for r in json.loads(f.read_text(encoding='utf-8')):
                REPORTERS[r['slug']] = r.get('short_name') or r['slug']
        except (OSError, ValueError):
            pass
    names = [REPORTERS.get(x, x) for x in slugs]
    return ', '.join(names[:12]) + (f' and {len(names) - 12} more' if len(names) > 12 else '')


def spec_from_plan(key: str, plan: dict) -> tuple[str, str, str]:
    """(who, court regex, reporters) for a plan key: the courts are already routed, so every record read is kept."""
    p = plan['repos'][key]
    courts = list(p['courts'])
    state = re.sub(r'-(appellate|other-courts)$', '', key)
    place = state.replace('-', ' ').title().replace(' Of ', ' of ')
    m = re.fullmatch(r'(ca\d+|cadc)-district', key)
    if key.endswith('-appellate'):
        who = f'the intermediate appellate courts of {place}'
    elif key.endswith('-other-courts'):
        who = f'the trial and other courts of {place}'
    elif m:
        ords = {'ca1': 'First', 'ca2': 'Second', 'ca3': 'Third', 'ca4': 'Fourth', 'ca5': 'Fifth', 'ca6': 'Sixth', 'ca7': 'Seventh',
                'ca8': 'Eighth', 'ca9': 'Ninth', 'ca10': 'Tenth', 'ca11': 'Eleventh', 'cadc': 'District of Columbia'}
        who = f'the federal district courts of the {ords[m.group(1)]} Circuit'
    elif key == 'bankruptcy':
        who = 'the federal bankruptcy courts and bankruptcy appellate panels'
    elif key == 'federal-special':
        who = 'the special federal courts (Claims, Customs, Customs and Patent Appeals, Tax, International Trade, military and others)'
    elif key == 'tribal':
        who = 'tribal courts (the Navajo Nation and other tribal courts CAP holds)'
    elif len(courts) == 1:
        who = f'the {courts[0]}' if not courts[0].startswith('the ') else courts[0]
    else:
        rest = len(courts) - 1
        who = f'the {courts[0]} and {rest} other court{"s" if rest > 1 else ""} of {place}'
    return who, r'.', reporters_of(p['reporters'])


def dedupe(recs: list[dict]) -> list[dict]:
    """One record per case: CAP sometimes carries the same case twice (the same day, docket and name, e.g. printed both in
    an orders list and with its opinion; 209 in the U.S. Reports, 2,110 in the Federal Reporter series). Keep the record
    with the most text and give it every citation and cited case of the others."""
    groups: dict[tuple, list[dict]] = {}
    out = []
    for r in recs:
        d = re.sub(r'^\s*(?:Nos?\.|Docket\s+No\.?)\s*', '', r.get('docket') or '').strip()
        if not d:
            d = '~' + (r.get('court') or '')  # no docket: the court stands in (the same case in two reporters)
        groups.setdefault(((r.get('decided') or '')[:10], d, norm_name(r.get('name_abbreviation') or '')), []).append(r)
    for g in groups.values():
        if len(g) > 1 and g[0].get('docket') in (None, ''):
            # without a docket, merge only texts of like length (two different cases can share a day and a name)
            lens = [text_length(x) for x in g]
            if max(lens) > 2.5 * max(1, min(lens)):
                out.extend(g)
                continue
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


def _valid(day: str) -> bool:
    try:
        date.fromisoformat(day)
        return True
    except ValueError:
        return False


def existing_state(out: Path) -> dict:
    """What an existing repo holds: its files, its decision index (CAP ids among the references), the day and name of
    every decision (to leave out the same case read from another reporter), the overlay files and the clamp count."""
    def git(*args):
        return subprocess.run(['git', '-C', str(out), *args], capture_output=True, encoding='utf-8', errors='replace').stdout
    paths = set(git('-c', 'core.quotePath=false', 'ls-tree', '-r', '--name-only', 'main').splitlines())
    index = git('show', 'main:decision-index.tsv').splitlines()
    refs = {l.split('\t', 1)[0] for l in index[1:]}
    keys, clamp = set(), 0
    for line in git('log', '--format=%s%x1f%(trailers:key=In-force,valueonly,separator=%x2C)%x1f%at', 'main').splitlines():
        subj, day, at = (line.split('\x1f') + ['', ''])[:3]
        day = day.strip()[:10]
        if day:
            keys.add((day, norm_name(subj.rsplit(', ', 1)[0])))
        if at.isdigit() and 0 < int(at) - EPOCH_NOON < 10_000_000:
            clamp = max(clamp, int(at) - EPOCH_NOON)
    return {'paths': paths, 'index': index, 'refs': refs, 'keys': keys, 'clamp': clamp,
            'readme': git('show', 'main:README.md'), 'corpus': git('show', 'main:corpus.json'),
            'decisions': sum(1 for p in paths if p.startswith('decisions/') and p.endswith('.md'))}


def append_docs(out, ex, docs, overlay, dindex, mark, first, n_refs, reporters, today, w, fi, checkpoints, slug_name):
    """The documentation commit after appended decisions: the decision index and the overlay files grown, corpus.json's
    coverage and start updated, and a section added to the README; nothing else is rewritten."""
    def show(p):
        return subprocess.run(['git', '-C', str(out), 'show', f'main:{p}'], capture_output=True, encoding='utf-8', errors='replace').stdout
    for p, rows in overlay.items():
        f = f'judgments/{LAW_CORPUS}/{p[:-3]}.jsonl'
        old = show(f) if f in ex['paths'] else ''
        new = ''.join(json.dumps(x, ensure_ascii=False, sort_keys=True) + '\n' for x in sorted(rows, key=lambda x: (x['decided'], x['citation'])))
        docs[f] = old + new
    head = ex['index'][0] if ex['index'] else 'reference\tpath'
    docs['decision-index.tsv'] = head + '\n' + ''.join(x + '\n' for x in sorted(set(ex['index'][1:]) | set(dindex)))
    try:
        cj = json.loads(ex['corpus'])
    except ValueError:
        cj = {}
    total = ex['decisions'] + mark
    if first and (not cj.get('history_from') or first < cj['history_from']):
        cj['history_from'] = first
    cj['coverage'] = re.sub(r'^\d+', str(total), cj.get('coverage') or f'{total} decisions')
    cj['appended'] = (cj.get('appended') or []) + [{'on': today, 'decisions': mark, 'source': 'Caselaw Access Project bulk data (static.case.law)',
                                                     'reporters': reporters, 'head_matter': 'left out (the publisher\'s text)'}]
    docs['corpus.json'] = json.dumps(cj, ensure_ascii=False, indent=2) + '\n'
    docs['README.md'] = ex['readme'].rstrip('\n') + f"""

## Added from the CAP bulk data ({today})
{mark:,} decisions this repo did not hold, from the Caselaw Access Project's bulk files ({reporters}): every decision of
its courts in CAP that was not here by its CAP id, nor as the same case (day and name) read from another reporter. Each
was appended as its own commit, dated the day decided, after the decisions already here (the history is append-only, so
they are not in date order; the tools read each decision's own date). Their head matter, the publisher's text, is left
out. Now {total:,} decisions.
"""
    ts = int(datetime.fromisoformat(today + 'T12:00:00-05:00').timestamp())
    msg = f'Documentation: {mark} decisions added from the CAP bulk data; decision index, overlay ({n_refs} citations), corpus.json, README\n'.encode()
    w(b'commit refs/heads/main\nauthor Lexifina <corpus@lexifina.com> %d -0500\ncommitter %s %d -0500\n' % (ts, COMMITTER, ts))
    w(b'data %d\n%s\n' % (len(msg), msg))
    for p in sorted(docs):
        b = docs[p].encode('utf-8')
        w(b'M 100644 inline %s\ndata %d\n%s\n' % (p.encode('utf-8'), len(b), b))
    w(b'done\n')
    fi.stdin.close()
    if fi.wait():
        raise SystemExit('fast-import failed')
    marks = dict(l.split() for l in (out / '.git' / 'marks-append').read_text().splitlines())
    (out / '.git' / 'push-checkpoints').write_text(''.join(marks[f':{m}'] + '\n' for m in checkpoints))
    print(json.dumps({'group': slug_name, 'decisions': mark, 'appended': True, 'total': total, 'overlay_rows': n_refs,
                      'checkpoints': len(checkpoints)}), flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('group'), ap.add_argument('parsed'), ap.add_argument('out')
    ap.add_argument('--today', required=True), ap.add_argument('--courts', default='')
    ap.add_argument('--law-repo', default=str(Path.home() / 'Documents' / 'caselaw-law-mirrors' / 'united-states-law.git'))
    ap.add_argument('--chunk-commits', type=int, default=40000)
    ap.add_argument('--plan', default=''), ap.add_argument('--era', default=''), ap.add_argument('--append', action='store_true')
    a = ap.parse_args()
    plan = json.loads(Path(a.plan).read_text(encoding='utf-8')) if a.plan else None
    if plan and a.group in plan['repos']:
        where, court_rx, reporters = spec_from_plan(a.group, plan)
        if a.append and a.group in STATES:
            where, reporters = STATES[a.group][0], reporters
    else:
        where, court_rx, reporters = STATES[a.group]
    court_rx = re.compile(a.courts or court_rx)
    era = tuple(a.era.split('-')) if a.era else None
    slug_name = f'{a.group}-{a.era}' if a.era else a.group
    if era:
        where = f'{where}, {era[0]}-{era[1]}'
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
                    if era and not (era[0] <= (r.get('decided') or '0000')[:4] <= era[1]):
                        continue
                    seen_ids.add(r['id'])
                    recs.append(r)
    recs = dedupe(recs)
    recs.sort(key=lambda r: ((r.get('decided') or '9999')[:10], r.get('official') or '', str(r['id'])))
    existing = None
    if a.append:
        existing = existing_state(out)
        recs = [r for r in recs if f"cap-{r['id']}" not in existing['refs'] and not any(f'cap-{x}' in existing['refs'] for x in r.get('merged_cap_ids') or [])
                and ((r.get('decided') or '')[:10], norm_name(r.get('name_abbreviation') or r.get('name') or '')) not in existing['keys']]
    print(f'{slug_name}: {len(recs)} decisions' + (' to append' if a.append else ''), file=sys.stderr, flush=True)
    if a.append and not recs:
        print(json.dumps({'group': slug_name, 'decisions': 0, 'appended': True}), flush=True)
        return
    if not a.append:
        if out.exists() and any(out.iterdir()):
            raise SystemExit(f'{out} is not empty')
        out.mkdir(parents=True, exist_ok=True)
        subprocess.run(['git', 'init', '-q', '-b', 'main', str(out)], check=True)
    fi = subprocess.Popen(['git', '-C', str(out), 'fast-import', '--quiet', '--done', f'--export-marks={out / ".git" / "marks-append" if a.append else out / ".git" / "marks"}',
                          '--big-file-threshold=64m'], stdin=subprocess.PIPE)
    w = fi.stdin.write
    overlay, dindex, used, undated = {}, [], set(existing['paths']) if existing else set(), []
    mark = clamp = 0
    if existing:
        clamp = existing['clamp']
    checkpoints, first, raw = [], None, 0
    for r in recs:
        day = (r.get('decided') or '')[:10]
        if len(day) == 7:
            day += '-01'
        if len(day) == 4:
            day += '-01-01'
        if ISO.match(day):
            try:
                date.fromisoformat(day)
            except ValueError:  # a day the month does not have (CAP: "1995-02-30"): the month's last day; the file keeps CAP's
                y_, m_ = int(day[:4]), int(day[5:7])
                if 1 <= m_ <= 12:
                    last = (date(y_ + (m_ == 12), m_ % 12 + 1, 1) - timedelta(days=1)).isoformat()
                    r = {**r, 'decided_as_given': r.get('decided'), 'decided': last}
                    day = last
        if not ISO.match(day) or day > a.today or not _valid(day):
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
        if existing and mark == 1:
            pass  # 'from' follows the committer line
        w(b'author %s <%s> %d -0500\ncommitter %s %d -0500\n' % (author.encode('utf-8'), email.encode(), ts, COMMITTER, ts))
        w(b'data %d\n%s\n' % (len(msg), msg))
        if existing and mark == 1:
            w(b'from refs/heads/main^0\n')
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
    if existing:
        append_docs(out, existing, docs, overlay, dindex, mark, first, n_refs, reporters, a.today, w, fi, checkpoints, slug_name)
        return
    gl = f'united-states/{slug_name}/caselaw'
    types_line = ''
    if False:
        kinds = {}
        for r in recs:
            if r not in undated:
                kinds[decision_type(r)] = kinds.get(decision_type(r), 0) + 1
        types_line = (f"\nThe U.S. Reports print every order as its own entry, and each is kept here: "
                      f"{kinds.get('memorandum order', 0):,} memorandum orders (certiorari denied, dismissed, rehearing denied; under 300 "
                      f"characters), {kinds.get('per curiam', 0):,} short per curiam decisions and orders (300 to 999 characters) and "
                      f"{kinds.get('opinion', 0):,} opinions (1,000 characters or more). Each file's `decision_type` says which.\n")
    docs['corpus.json'] = json.dumps({
        'corpus': f'us/{slug_name}/caselaw', 'path': gl, 'name': f'Case law: United States, {where}', 'label': f'United States case law ({where})',
        'language': 'en', 'kind': 'judgments', 'history': 'in-force',
        'coverage': f'{mark} published opinions with text, one file and one commit each, dated the day decided; {n_refs} citations of the US Code linked to united-states/federal/usc.',
        'history_from': first, 'record_to': a.today, 'checked_on': a.today,
        'record_note': f'Court decisions, not laws: every commit is Kind: court. In-force is the day decided. Commits before 1970 are dated 1970-01-01, a second apart; In-force holds the real day. Read from {reporters}. The head matter (syllabi, headnotes, counsel) is left out: it is the publisher\'s text, not the court\'s. Every decision is a published opinion (precedential_status: published); CAP records no other status. CAP is a closed archive (its last volumes are of 2018-2020): no nightly runner appends to this repo. The US Code record holds only some titles, so most statute citations are not linked.',
        'update': 'none: the Caselaw Access Project is a static archive, so no nightly runner is registered',
        'in_force_trailer': 'In-force', 'correction_trailer': 'Corrected',
        'dates': {'commit': 'decided', 'time': '12:00 UTC-5'},
        'source': {'publisher': 'Caselaw Access Project (Harvard Law School Library)', 'url': 'https://static.case.law/', 'licence': 'CC0 1.0',
                   'reporters': reporters},
        'overlay': {'path': f'judgments/{LAW_CORPUS}/<title>/<section>.jsonl', 'law_corpus': LAW_CORPUS},
    }, ensure_ascii=False, indent=2) + '\n'
    docs['README.md'] = f"""# US case law: {where[0].upper() + where[1:]}

Part of [lexifina/global-law](https://github.com/lexifina/global-law), one git history for every country's law: this is the case law of {where}, United States, which global-law reads at `{gl}/`.

Every published opinion of {where} in the Caselaw Access Project, as a git history: one file per decision, one commit
per decision, dated the day it was decided. {mark:,} decisions from {first} to the end of the source ({reporters}).
{types_line}
## Questions as git commands

| Question | Command |
|---|---|
| A decision by its citation | `grep -P '^<citation>\\t' decision-index.tsv` then `cat <path>` |
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
Text is CAP's OCR, unchanged. The head matter CAP holds (syllabus, headnotes, counsel lists, the
reporter's summary) is left out: in these reporters it is written by the publisher, not the court. Each file's front matter
gives the reporter and `precedential_status: published` (every decision here was printed in a reporter; CAP records no
other status).

## What it does not show
Decisions after the end of CAP's coverage (its last volumes are of 2018-2020): CAP is a closed archive, so no nightly
runner appends here. Unpublished decisions not in the reporters, the head matter, and statute citations to US Code titles
the record does not hold.
"""
    docs['REPRODUCE.md'] = f"""# Reproducing this repository

```
python tools/cap_bulk_fetch.py <cap-bulk>                  # every volume zip of static.case.law, verified
python tools/cap_bulk_extract.py <cap-bulk> <parsed> --reporters <reporters> --courts '<court regex>' [--after <day>]
python tools/cap_plan.py <cap-bulk>                       # which repo each case goes to: plan.json
python tools/cap_route_extract.py <cap-bulk> <parsed>      # every case, filed by its repo
python tools/build_state.py {a.group} <parsed>/{a.group} <empty folder> --today {a.today} --plan <cap-bulk>/plan.json{' --era ' + a.era if a.era else ''}
```

Input: the Caselaw Access Project's bulk files on static.case.law (CC0): VolumesMetadata.json and the volume zips it lists,
fetched 2026-11-08 at 16 to 32 parallel streams (about 12 MB/s), backing off on any error. static.case.law's robots.txt
disallows all paths (`User-agent: *` / `Disallow: /`); its documentation offers these files for bulk download, and they
were fetched as such, with no listing page crawled. Gives {mark:,} decision commits and one documentation commit.
"""
    for f in ('fetch_cap.py', 'build_state.py', 'build.py', 'cap_bulk_fetch.py', 'cap_bulk_extract.py', 'cap_plan.py', 'cap_route_extract.py'):
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
    print(json.dumps({'group': slug_name, 'decisions': mark, 'undated': len(undated), 'overlay_rows': n_refs,
                      'raw_gb': round(raw / 1e9, 2), 'checkpoints': len(checkpoints)}), flush=True)


if __name__ == '__main__':
    main()
