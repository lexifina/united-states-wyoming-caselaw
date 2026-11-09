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
import difflib
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
_u = importlib.util.spec_from_file_location('usc_cite', HERE / 'usc_cite.py')
UC = importlib.util.module_from_spec(_u)
_u.loader.exec_module(UC)

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


def _record_from(ver: dict) -> str | None:
    """The day the US Code record starts (its earliest version of any section)."""
    key = id(ver)
    if _RECORD_FROM.get('key') != key:
        _RECORD_FROM.update(key=key, day=min((v[0][0] for v in ver.values() if v[0]), default=None))
    return _RECORD_FROM['day']


_RECORD_FROM: dict = {}


def usc_cites(text: str, have: set, day: str | None = None, ver: dict | None = None) -> list[tuple[str, str]]:
    """(law path, citation words) for every section of the US Code record a text cites (usc_cite.cites: a page stamp
    "USCA4" is not a citation, "(a)(4)" is a subsection, "§§ 1-16" a range read at its two ends), leaving out a citation
    under an older numbering than the record's (usc_cite.old_numbering: a decision before the title's recodification or
    before the section was added, an edition before it, "former")."""
    out = []
    start = _record_from(ver) if ver else None
    for c in UC.cites(text, exists=lambda t, s: f'title-{t}/{s}.md' in have, long_forms=True):
        p = f"title-{c['title']}/{c['section']}.md"
        if p not in have:
            continue
        first = ver[p][0][0] if ver and p in ver and ver[p][0] else None
        if UC.old_numbering(c, day, first, start):
            continue
        out.append((p, c['how'][:200]))
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


def docket_refs(docket: str) -> list[str]:
    """The docket a decision is indexed by: as given, less the leading punctuation CAP's OCR leaves (", 671", "- 475",
    ". 78-1567"), and only when it holds a digit (not "** This disposition is not appropriate for publication")."""
    d = re.sub(r'^[\s.,;:()\-–—*]+', '', docket or '').strip()
    return [d] if re.search(r'\d', d) and len(d) <= 80 else []


HEAD_MATTER_HEADING = '## CAP head matter'
HEAD_MATTER_NOTE = ('*CAP holds no separate opinion text for this decision: the text below is its head matter as CAP gives it, '
                    'which may hold the court\'s opinion or order together with the publisher\'s syllabus, headnotes and counsel.*')


def body_of(r: dict) -> str:
    """The opinions only: CAP's head matter (syllabus, headnotes, counsel, the reporter's summary) is the publisher's
    text in these reporters, not the court's, so it is left out (US case-law sizing, 2026-11-07). Where CAP has no
    opinion text at all, the head matter is all it has (often the opinion itself: a short order, or an opinion CAP did
    not split out), so it is given, labelled as CAP head matter (2026-11-09: 95,473 files had no text)."""
    parts = []
    for o in r.get('opinions') or []:
        head = (o.get('type') or 'opinion').replace('-', ' ').capitalize()
        if o.get('author'):
            head += f" ({o['author'].strip()})"
        parts += [f'## {head}', '', (o.get('text') or '').strip(), '']
    if not any((o.get('text') or '').strip() for o in r.get('opinions') or []) and (r.get('head_matter') or '').strip():
        parts = [HEAD_MATTER_HEADING, '', HEAD_MATTER_NOTE, '', r['head_matter'].strip(), '']
    return '\n'.join(parts)


def has_opinion(r: dict) -> bool:
    return any((o.get('text') or '').strip() for o in r.get('opinions') or [])


def compare_text(r: dict) -> str:
    """The text two records of one case are compared by: the opinions, or the head matter where there are none."""
    t = '\n'.join((o.get('text') or '') for o in r.get('opinions') or [])
    return t if t.strip() else (r.get('head_matter') or '')


def same_text(a: str, b: str) -> bool:
    """Two texts of one decision: two reporters' OCR of one opinion (difflib ratio >= 0.6 over the first 4,000
    characters) or one inside the other (the first 300 characters of either found in the other). Two different
    decisions of one day, docket and name (an order list's entry and the opinion, a remand and its sequel) fail it."""
    a, b = re.sub(r'\s+', ' ', a or '').strip(), re.sub(r'\s+', ' ', b or '').strip()
    if not a or not b:
        return not a and not b
    if a[:300] in b or b[:300] in a:
        return True
    sm = difflib.SequenceMatcher(None, a[:4000], b[:4000], autojunk=False)
    return sm.real_quick_ratio() >= 0.6 and sm.quick_ratio() >= 0.6 and sm.ratio() >= 0.6


def date_precision(decided: str | None) -> str | None:
    """'month' or 'year' for a CAP date given only to the month ("1890-05") or the year ("1890"), else None."""
    d = (decided or '').strip()
    return 'month' if re.fullmatch(r'\d{4}-\d{2}', d) else 'year' if re.fullmatch(r'\d{4}', d) else None


def commit_day(decided: str | None) -> str:
    """The day a decision is committed on: a month-only date is the month's first day, a year-only one 1 January, a
    day the month does not have its last day (the builder's rule; the append filter compares on this, not CAP's text)."""
    day = (decided or '')[:10]
    if len(day) == 7:
        day += '-01'
    if len(day) == 4:
        day += '-01-01'
    if ISO.match(day):
        try:
            date.fromisoformat(day)
        except ValueError:
            y_, m_ = int(day[:4]), int(day[5:7])
            if 1 <= m_ <= 12:
                day = (date(y_ + (m_ == 12), m_ % 12 + 1, 1) - timedelta(days=1)).isoformat()
    return day


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
    # statute citations of the opinions only: a head-matter fallback (publisher's headnotes) adds no overlay rows
    found = usc_cites(text if has_opinion(r) else '', have, day, ver)
    given = r.get('decided_as_given') or r.get('decided')
    fm = {'court': court, 'docket': docket or None, 'decided': r.get('decided'), 'decided_as_given': r.get('decided_as_given'),
          'decided_precision': date_precision(given),  # CAP gives only the month or the year: committed on its first day
          'citations': r.get('citations') or None,
          'reporter': r.get('reporter'), 'precedential_status': 'published',
          'precedential_note': 'printed in a reporter; CAP records no other status',
          'cap_court': r.get('cap_court'),  # the court CAP files it under, where that court no longer sat that day
          'court_note': ('CAP gives no court name; the court is the one its head matter names' if r.get('cap_court') == '' else None),
          'text_source': None if has_opinion(r) else 'CAP head matter (CAP holds no opinion text)',
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
    refs = sorted({*(r.get('citations') or []), *docket_refs(docket), f"cap-{r['id']}"})
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
    # every record read is kept, a court CAP leaves unnamed too ('.' left out 307 South Carolina decisions, 2026-11-09)
    return who, r'', reporters_of(p['reporters'])


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
        # one key can hold several decisions (2026-11-09: 7-9% of 132,388 merges dropped a different decision, e.g.
        # 327 F. Supp. 3d 300 and 358, one day, docket and name): a record joins the first kept record whose text it
        # shares (same_text), else it is kept as its own decision
        clusters: list[list[dict]] = []
        for r in g:
            for c in clusters:
                if same_text(compare_text(c[0]), compare_text(r)):
                    c.append(r)
                    break
            else:
                clusters.append([r])
        for c in clusters:
            out.append(merge_group(c))
    return out


def merge_group(c: list[dict]) -> dict:
    """The first record of a group of one case's records, with every citation and cited case of the others."""
    if len(c) == 1:
        return c[0]
    keep = dict(c[0])
    cites = list(keep.get('citations') or [])
    for o in c[1:]:
        cites += [x for x in o.get('citations') or [] if x not in cites]
        keep['cites_to'] = sorted(set((keep.get('cites_to') or []) + (o.get('cites_to') or [])))
    keep['citations'] = cites
    keep['merged_cap_ids'] = [o['id'] for o in c[1:]]
    return keep


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
    by_key: dict = {}  # (day, normalised name) and (day, '#' + docket) -> [paths of decisions held]
    log = git('-c', 'core.quotePath=false', 'log', '--format=%x00%s%x1f%(trailers:key=In-force,valueonly,separator=%x2C)%x1f%at%x1f'
              '%(trailers:key=Docket,valueonly,separator=%x2C)%x1f%(trailers:key=Kind,valueonly)', '--name-only', '--diff-filter=A', 'main')
    for block in log.split('\x00')[1:]:
        head, _, names = block.partition('\n')
        subj, day, at, dock, kind = (head.split('\x1f') + ['', '', '', ''])[:5]
        day = day.strip()[:10]
        added = [n for n in names.split('\n') if n.startswith('decisions/') and n.endswith('.md') and n in paths]
        if day:
            keys.add((day, norm_name(subj.rsplit(', ', 1)[0])))
            for p in added:
                by_key.setdefault((day, norm_name(subj.rsplit(', ', 1)[0])), []).append(p)
                if docket_key(dock):
                    by_key.setdefault((day, '#' + docket_key(dock)), []).append(p)
        # only decisions before 1970 count: a 1970 decision's noon is also within ten million seconds of the epoch,
        # and counting it dated later appends' pre-1970 decisions in April 1970
        if day and day < '1970-01-01' and at.isdigit() and 0 < int(at) - EPOCH_NOON < 10_000_000:
            clamp = max(clamp, int(at) - EPOCH_NOON)
    return {'paths': paths, 'index': index, 'refs': refs, 'keys': keys, 'by_key': by_key, 'clamp': clamp,
            'readme': git('show', 'main:README.md'), 'corpus': git('show', 'main:corpus.json'),
            'decisions': sum(1 for p in paths if p.startswith('decisions/') and p.endswith('.md'))}


def docket_key(docket: str | None) -> str:
    """A docket as compared: letters and digits only, upper case, with "No." and "Docket No." left out."""
    d = re.sub(r'^\s*(?:Nos?\.|Docket\s+No\.?)\s*', '', docket or '')
    d = re.sub(r'[^A-Za-z0-9]', '', d).upper()
    return d if re.search(r'\d', d) else ''


def read_blobs(repo: Path, ref: str, paths: list[str]) -> dict:
    """{path: text} for many files of one commit, one cat-file --batch."""
    import threading
    proc = subprocess.Popen(['git', '-C', str(repo), 'cat-file', '--batch'], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    threading.Thread(target=lambda: (proc.stdin.write(''.join(f'{ref}:{p}\n' for p in paths).encode('utf-8')), proc.stdin.close()),
                     daemon=True).start()
    out = {}
    for p in paths:
        head = proc.stdout.readline().split()
        if len(head) < 3 or head[1] == b'missing':
            continue
        out[p] = proc.stdout.read(int(head[2])).decode('utf-8', errors='replace')
        proc.stdout.read(1)
    proc.wait()
    return out


def file_text(md: str) -> str:
    """A decision file's text as compared with a record's (compare_text): the body under its headings, less the front
    matter, the title, the opinion headings and the head-matter note."""
    body = md.split('\n---\n', 1)[1] if md.startswith('---\n') and '\n---\n' in md else md
    lines = [l for l in body.split('\n') if not l.startswith('# ') and not l.startswith('## ') and l.strip() != HEAD_MATTER_NOTE]
    return '\n'.join(lines)


def held_as(r: dict, ex: dict, texts: dict) -> str | None:
    """The path of the decision an existing repo already holds that is this record's case: the same commit day and
    (normalised name or docket), and the same text (same_text). texts: {path: file text} of the candidates."""
    day = commit_day(r.get('decided'))
    cands = []
    for k in ((day, norm_name(r.get('name_abbreviation') or r.get('name') or '')), (day, '#' + docket_key(r.get('docket')))):
        if k[1] and k[1] != '#':
            cands += [p for p in ex['by_key'].get(k, []) if p not in cands]
    for p in cands:
        if p in texts and same_text(file_text(texts[p]), compare_text(r)):
            return p
    return None


def held_candidates(r: dict, ex: dict) -> list[str]:
    day = commit_day(r.get('decided'))
    out = []
    for k in ((day, norm_name(r.get('name_abbreviation') or r.get('name') or '')), (day, '#' + docket_key(r.get('docket')))):
        if k[1] and k[1] != '#':
            out += ex['by_key'].get(k, [])
    return out


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
    whole = head + '\n' + ''.join(x + '\n' for x in sorted(set(ex['index'][1:]) | set(dindex)))
    if len(whole.encode('utf-8')) < 90_000_000:
        docs['decision-index.tsv'] = whole
    else:
        # GitHub takes no file over 100 MB: decision-index.tsv stays as it is and the new references go in the year's
        # own file (decision-index-<year>.tsv), which the tools read with it (the Supreme Court's grew to 117 MB).
        docs.pop('decision-index.tsv', None)  # left as it is (main() put the new references alone there)
        f = f'decision-index-{today[:4]}.tsv'
        old = show(f).splitlines()[1:] if f in ex['paths'] else []
        docs[f] = head + '\n' + ''.join(x + '\n' for x in sorted(set(old) | set(dindex)))
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


def check_counts(key: str, parsed: list[str], read_n: int, plan: dict) -> None:
    """The records read must be every record the routed extraction wrote for this repo (its volume markers, see
    cap_route_extract.py), and with the cases it left out for having no text, the plan's count. 2026-11-09: 80
    volumes' files were missing behind their markers, and 12,440 decisions never reached their repos unnoticed."""
    done = Path(parsed[0]).parent / '_done'
    want = empty = 0
    empty_known = True
    for rep in plan['repos'][key]['reporters']:
        for m in (done / rep).glob('*.json'):
            try:
                x = json.loads(m.read_text())
            except ValueError:
                raise SystemExit(f'{key}: unreadable marker {m.name}: run cap_route_extract.py --verify')
            if isinstance(x, int):
                raise SystemExit(f'{key}: marker {m.name} gives no count per repo: run cap_route_extract.py --verify')
            want += (x.get('repos') or {}).get(key, 0)
            if x.get('empty') is None:
                empty_known = False
            else:
                empty += x['empty'].get(key, 0)
    if read_n != want:
        raise SystemExit(f'{key}: read {read_n} records, the extraction markers say {want}: run cap_route_extract.py --verify')
    n = plan['repos'][key]['n']
    if (empty_known and read_n + empty != n) or read_n > n:
        raise SystemExit(f'{key}: read {read_n} records (+{empty} with no text), the plan routes {n} cases here')


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
    recs, seen_ids, read_n = [], set(), 0
    for d in a.parsed.split(','):
        for f in sorted(Path(d).rglob('*.jsonl.gz')):
            with gzip.open(f, 'rt', encoding='utf-8') as fh:
                for line in fh:
                    r = json.loads(line)
                    read_n += 1
                    if not court_rx.search(r.get('court') or '') or r['id'] in seen_ids:
                        continue
                    if era and not (era[0] <= (r.get('decided') or '0000')[:4] <= era[1]):
                        continue
                    seen_ids.add(r['id'])
                    recs.append(r)
    if plan and a.group in plan['repos'] and not a.courts:
        check_counts(a.group, a.parsed.split(','), read_n, plan)
    recs = dedupe(recs)
    recs.sort(key=lambda r: ((r.get('decided') or '9999')[:10], r.get('official') or '', str(r['id'])))
    existing = None
    held_refs: list[str] = []  # citations of records left out as a decision already held: index rows to that file
    if a.append:
        existing = existing_state(out)
        keep = []
        cand = [r for r in recs if f"cap-{r['id']}" not in existing['refs'] and not any(f'cap-{x}' in existing['refs'] for x in r.get('merged_cap_ids') or [])]
        texts = read_blobs(out, 'main', sorted({p for r in cand for p in held_candidates(r, existing)}))
        for r in cand:
            # the same case held already (the same commit day, name or docket, and text): left out, but its citations
            # and CAP id name the file held (2026-11-09: 43,892 left out by day and name alone lost their citations;
            # month-only dates compared as CAP's text re-appended 370 Supreme Court decisions)
            p = held_as(r, existing, texts)
            if p:
                held_refs += [f'{x}\t{p}' for x in sorted({*(r.get('citations') or []), f"cap-{r['id']}", *(f'cap-{x}' for x in r.get('merged_cap_ids') or [])})]
            else:
                keep.append(r)
        recs = keep
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
    overlay, dindex, used, undated = {}, list(held_refs), set(existing['paths']) if existing else set(), []
    mark = clamp = n_hm = 0
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
        n_hm += not has_opinion(r)
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
        'coverage': f'{mark} published decisions, one file and one commit each, dated the day decided: {mark - n_hm} with their opinion text and {n_hm} with CAP head matter only (CAP holds no opinion text for them); {n_refs} citations of the US Code linked to united-states/federal/usc.',
        'history_from': first, 'record_to': a.today, 'checked_on': a.today,
        'record_note': f'Court decisions, not laws: every commit is Kind: court. In-force is the day decided. Commits before 1970 are dated from 1970-01-01 noon, a second apart (past 43,200 of them, on into 1970-01-02 and later); In-force holds the real day. Read from {reporters}. The head matter (syllabi, headnotes, counsel) is left out: it is the publisher\'s text, not the court\'s. Every decision is a published opinion (precedential_status: published); CAP records no other status. CAP is a closed archive (its last volumes are of 2018-2020): no nightly runner appends to this repo. The US Code record holds only some titles, so most statute citations are not linked.',
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
| Decisions given in a window | `git log --format='%(trailers:key=In-force,valueonly,separator=) %an' \\| awk '$1 >= "1973-01-01" && $1 <= "1973-01-31"'`: read the `In-force:` trailer, not the commit date (see Dates), or use the global-law tools |
| Decisions citing 11 U.S.C. § 1182 | `cat judgments/united-states/federal/usc/title-11/1182.jsonl` |
| The section as the court applied it | each overlay row's `law_commit` is the section's own version in the US Code record on the day decided (or `law_record_from` when the decision is older than the record) |
| Who wrote a line? | `git blame decisions/...`: each line is credited to the decision (court and citation) |

## Dates and authors
The commit date is the day the decision was given, at noon US Eastern (`In-force:` and `Decided:`). Git cannot hold dates
before 1970, so earlier decisions are committed from 1970-01-01 noon, a second apart (past 43,200 of them, on into
1970-01-02 and later), and `In-force:` holds the real day: read dates from `In-force:`, not from the commit date. A date
CAP gives only to the month or the year is committed on its first day, and the file's `decided_precision` says so. The
author is the court and the official citation, at an address that cannot exist. These are court rulings, not laws: every
commit is `Kind: court`.

## Source
The Caselaw Access Project's bulk data (Harvard Law School Library), dedicated to the public domain under CC0; court
opinions are not copyrightable (17 U.S.C. § 105; Georgia v. Public.Resource.Org, 2020). Each file keeps its source URL.
Text is CAP's OCR, unchanged. The head matter CAP holds (syllabus, headnotes, counsel lists, the
reporter's summary) is left out: in these reporters it is written by the publisher, not the court. Where CAP holds no
opinion text for a decision, its head matter is given instead, under `## CAP head matter` (`text_source` in the front
matter): it is all CAP has, and often the court's order or opinion itself. Each file's front matter
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
    for f in ('fetch_cap.py', 'build_state.py', 'build.py', 'cap_bulk_fetch.py', 'cap_bulk_extract.py', 'cap_plan.py', 'cap_route_extract.py', 'usc_cite.py'):
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
