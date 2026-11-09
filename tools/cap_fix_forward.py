"""Forward fix of a pushed CAP case-law repo against the fixed builder (2026-11-09 deep check): new commits on top of
main, nothing rewritten. Per repo, the records cap_route_extract.py routes to it (fixed router), merged by the fixed
build_state.dedupe (one case only where the texts agree), compared with what main holds:
  - a case main does not hold (its CAP ids not in the decision index, and no decision of the same commit day, name or
    docket with the same text): appended as its own Kind: court commit, in date order (extracts that were missing,
    wrongly merged decisions, South Carolina's unnamed court, decisions routed here by the fixed router);
  - a case held under another file (same day, name or docket, same text): its citations and CAP id are indexed to it;
  - a citation or CAP id the index gives a file whose case it is not (a wrong merge): pointed at its own decision;
  - a held file with no text (CAP has no opinion text: the head matter, labelled), a court CAP names for a day it did
    not sit (cap_court keeps CAP's), or an impossible date without decided_as_given: a Kind: correction commit on the
    same file (Corrected:, Corrects: <the commit that added it>), as nl-caselaw/nightly.py writes corrections;
  - in the repos with CAP appends (the 14 federal and 5 test states): a decision appended after the same decision
    (same day and docket or name, same text) is listed in decision-relations.tsv as `same-decision-as` the earlier
    one, as is a path the 2026-11-08 dedupe removed that the append added again; no file is deleted;
then one Lexifina documentation commit: decision index, decision-relations.tsv, decision-dates.tsv (CAP dates given
only to the month or year), overlay rows of the new decisions, corpus.json, README and REPRODUCE.md sections, tools/.

  python cap_fix_forward.py <repo slug> [--dry-run] [--push] [--today YYYY-MM-DD]
  python cap_fix_forward.py <repo slug> --moves [--dry-run] [--push]
    --moves: after the destination repos are pushed, delete from this repo the decisions the fixed router sends
    elsewhere (federal dedupe pattern: one documentation commit, index rows and overlay rows removed), each listed in
    decision-relations.tsv as `moved-to <repo>/<path>`."""
from __future__ import annotations

import argparse
import gzip
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
_s = importlib.util.spec_from_file_location('build_state', HERE / 'build_state.py')
B = importlib.util.module_from_spec(_s)
_s.loader.exec_module(B)
D = Path.home() / 'Documents'
ROOT = D / 'us-caselaw-repos'
BULK = D / '.caselaw-cache' / 'us' / 'cap-bulk'
ROUTED = D / '.caselaw-cache' / 'us' / 'cap-routed'
OUT = D / 'cap-fix' / 'runs'
APPENDED = {'supreme-court', 'cadc', 'cafc', *[f'ca{i}' for i in range(1, 12)], 'california', 'new-york', 'delaware', 'massachusetts', 'nevada'}
FEDERAL = {'supreme-court', 'cadc', 'cafc', *[f'ca{i}' for i in range(1, 12)]}
CHUNK = 40000
REL_HEAD = 'path\trelation\ttarget\tnote'
DATES_HEAD = 'path\tdecided_as_given\tprecision'
TOOLS = ('fetch_cap.py', 'build_state.py', 'build.py', 'cap_bulk_fetch.py', 'cap_bulk_extract.py', 'cap_plan.py', 'cap_route_extract.py',
         'usc_cite.py', 'cap_fix_forward.py')

try:
    import psutil
    psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
except Exception:  # noqa: BLE001
    pass


def git(repo, *a, inp=None, check=True, text=True):
    r = subprocess.run(['git', '-C', str(repo), '-c', 'core.quotePath=false', *a], input=inp, capture_output=True)
    if check and r.returncode:
        raise SystemExit(f'git {a[0]} in {Path(repo).name}: {r.stderr.decode(errors="replace")[-600:]}')
    return r.stdout.decode('utf-8', errors='replace') if text else r.stdout


def key_era(slug: str, plan: dict) -> tuple[str, tuple | None]:
    if slug in plan['repos']:
        return slug, None
    for k, p in plan['repos'].items():
        for s, e, _ in p.get('split') or []:
            if slug == f'{k}-{s}-{e}':
                return k, (s, e)
    raise SystemExit(f'{slug}: not in plan.json')


def load_records(key: str, era) -> list[dict]:
    recs, seen = [], set()
    for f in sorted((ROUTED / key).rglob('*.jsonl.gz')):
        with gzip.open(f, 'rt', encoding='utf-8') as fh:
            for line in fh:
                r = json.loads(line)
                if r['id'] in seen:
                    continue
                if era and not (era[0] <= (r.get('decided') or '0000')[:4] <= era[1]):
                    continue
                seen.add(r['id'])
                r.pop('attorneys', None)
                recs.append(r)
    return recs


def scan(repo: Path) -> dict:
    """One log of main: every decision file's adding commit, the (day, name) and (day, docket) keys, the doc segments,
    the pre-1970 clamp, the paths a Lexifina commit removed (with the kept path its message names)."""
    log = git(repo, 'log', '--first-parent', '--reverse', '--format=%x00%H%x1f%an%x1f%ae%x1f%at%x1f%s%x1f'
              '%(trailers:key=In-force,valueonly,separator=%x2C)%x1f%(trailers:key=Docket,valueonly,separator=%x2C)%x1f'
              '%(trailers:key=Source,valueonly,separator=%x2C)', '--name-status', '--no-renames', 'main')
    add, by_key, removed = {}, defaultdict(list), {}
    seg, clamp, order = 0, 0, 0
    spill = april = pre = 0
    for block in log.split('\x00')[1:]:
        head, _, names = block.partition('\n')
        sha, an, ae, at, subj, day, dock, src = (head.split('\x1f') + [''] * 8)[:8]
        day = day.strip()[:10]
        if an == 'Lexifina':
            seg += 1
        for ln in names.split('\n'):
            st, _, p = ln.partition('\t')
            if not p.startswith('decisions/') or not p.endswith('.md'):
                continue
            if st == 'A' and an != 'Lexifina':
                order += 1
                add[p] = {'sha': sha, 'an': an, 'ae': ae, 'at': int(at), 'day': day, 'seg': seg, 'order': order,
                          'cap': 'static.case.law' in src, 'docket': dock, 'name': B.norm_name(subj.rsplit(', ', 1)[0])}
                if day:
                    by_key[(day, B.norm_name(subj.rsplit(', ', 1)[0]))].append(p)
                    if B.docket_key(dock):
                        by_key[(day, '#' + B.docket_key(dock))].append(p)
            elif st == 'D':
                removed[p] = sha
        if an != 'Lexifina' and day and day < '1970-01-01' and at.isdigit():
            pre += 1
            dt = int(at) - B.EPOCH_NOON
            if 0 < dt < 10_000_000:
                clamp = max(clamp, dt)
            if dt >= 86400 - 12 * 3600:  # on or after 1970-01-02 00:00 UTC-5
                spill += 1
            if dt >= 90 * 86400:
                april += 1
    return {'add': add, 'by_key': by_key, 'removed': removed, 'clamp': clamp, 'spill': spill, 'april': april, 'pre1970': pre}


def dedupe_notes(repo: Path) -> dict:
    """removed path -> kept path, from the dedupe_pushed.py commit messages."""
    out = {}
    for line in git(repo, 'log', '--author=Lexifina', '--grep=duplicate decisions merged', '--format=%B', 'main').splitlines():
        m = re.match(r'^(decisions/\S.*?\.md) -> (decisions/\S.*?\.md) \(', line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def index_files(repo: Path, names: list[str]) -> dict[str, list[tuple[str, str]]]:
    out = {}
    for f in names:
        if re.fullmatch(r'decision-index(?:-\d{4})?\.tsv', f):
            lines = git(repo, 'show', f'main:{f}').splitlines()
            out[f] = [tuple(l.split('\t', 1)) for l in lines[1:] if '\t' in l]
    return out


def refs_of(r: dict) -> set[str]:
    docket = re.sub(r'^\s*(?:Nos?\.|Docket\s+No\.?)\s*', '', r.get('docket') or '').strip()
    return {*(r.get('citations') or []), *B.docket_refs(docket), f"cap-{r['id']}", *(f'cap-{x}' for x in r.get('merged_cap_ids') or [])}


def cap_refs_of(r: dict) -> set[str]:
    return {*(r.get('citations') or []), f"cap-{r['id']}", *(f'cap-{x}' for x in r.get('merged_cap_ids') or [])}


def dated(r: dict, today: str) -> tuple[dict, str | None]:
    """(record, commit day) as build_state.main dates it, or (record, None) for a record it leaves undated."""
    raw = (r.get('decided') or '')[:10]
    day = B.commit_day(r.get('decided'))
    if len(raw) == 10 and B.ISO.match(raw) and day != raw:
        r = {**r, 'decided_as_given': r.get('decided'), 'decided': day}
    if not B.ISO.match(day) or day > today or not B._valid(day):
        return r, None
    return r, day


def front(md: str) -> dict:
    fm = {}
    if md.startswith('---\n'):
        for line in md[4:].split('\n---\n', 1)[0].split('\n'):
            k, _, v = line.partition(': ')
            try:
                fm[k] = json.loads(v)
            except ValueError:
                fm[k] = v
    return fm


def body_empty(md: str) -> bool:
    return not B.file_text(md).strip()


def fi_commit(w, author, email, ats, cts, msg: bytes, frm: str | None, files: dict, deletes=()):
    w.append(b'commit refs/heads/main\n')
    w.append(b'author %s <%s> %d -0500\ncommitter %s %d -0500\n' % (author.encode('utf-8'), email.encode(), ats, B.COMMITTER, cts))
    w.append(b'data %d\n%s\n' % (len(msg), msg))
    if frm:
        w.append(b'from %s\n' % frm.encode())
    for p in deletes:
        w.append(b'D %s\n' % p.encode('utf-8'))
    for p, b in files.items():
        w.append(b'M 100644 inline %s\ndata %d\n%s\n' % (p.encode('utf-8'), len(b), b))


def sync(repo: Path) -> str:
    """main == origin/main, fetched: the base the fix commits go on (never ahead, never diverged)."""
    git(repo, 'fetch', '-q', 'origin', 'main')
    local, remote = git(repo, 'rev-parse', 'main').strip(), git(repo, 'rev-parse', 'origin/main').strip()
    if local != remote:
        if subprocess.run(['git', '-C', str(repo), 'merge-base', '--is-ancestor', local, remote]).returncode == 0:
            git(repo, 'update-ref', 'refs/heads/main', remote, local)
        else:
            raise SystemExit(f'{repo.name}: local main {local[:10]} is not behind origin/main {remote[:10]}; not touched')
    return remote


def index_text(rows) -> bytes:
    return ('reference\tpath\n' + ''.join(f'{a}\t{b}\n' for a, b in sorted(set(rows)))).encode('utf-8')


def similar_name(a: str, b: str) -> bool:
    import difflib
    return bool(a and b) and (a == b or difflib.SequenceMatcher(None, a, b).ratio() >= 0.6)


def clean_ref(ref: str) -> str | None:
    """A docket reference as the builders now write it: CAP's OCR punctuation in front stripped (", 671", ". 78-1567"),
    and none at all without a digit ("** This disposition is not appropriate for publication")."""
    r = re.sub(r'^[\s.,;:()\-–—*¡]+', '', ref).strip()
    return r if re.search(r'\d', r) and len(r) <= 80 or r.startswith('cap-') else None


def write_index(idx: dict, drop: set, add: set) -> dict[str, bytes]:
    """The index files changed: rows in `drop` removed wherever they are, rows in `add` put in decision-index.tsv,
    or in decision-index-<year>.tsv where the main file would pass 90 MB (GitHub refuses files over 100 MB). Junk
    docket references are cleaned in every file (clean_ref)."""
    out = {}
    for f, rows in list(idx.items()):
        new = []
        for ref, p in rows:
            c = clean_ref(ref)
            if c is not None:
                new.append((c, p))
        if new != rows:
            idx[f] = new
            out[f] = None
    have = {r for rows in idx.values() for r in rows}
    add = add - have
    for f, rows in idx.items():
        if drop & set(rows):
            idx[f] = [r for r in rows if r not in drop]
            out[f] = None
    if add:
        main = 'decision-index.tsv'
        size = sum(len(a) + len(b) + 2 for a, b in idx.get(main, [])) + sum(len(a) + len(b) + 2 for a, b in add)
        target = main if size < 90_000_000 else f'decision-index-{time.strftime("%Y")}.tsv'
        idx[target] = list(idx.get(target, [])) + sorted(add)
        out[target] = None
    return {f: index_text(idx[f]) for f in out}


def fix(slug: str, a) -> dict:
    t0 = time.time()
    plan = json.loads((BULK / 'plan.json').read_text(encoding='utf-8'))
    key, era = key_era(slug, plan)
    repo = ROOT / slug
    base = sync(repo)
    stats = Counter()
    names = git(repo, 'ls-tree', '-r', '--name-only', base).splitlines()
    files = {n for n in names if n.startswith('decisions/') and n.endswith('.md')}
    idx = index_files(repo, names)
    cap_path, ref_paths = {}, defaultdict(set)
    for rows in idx.values():
        for ref, p in rows:
            ref_paths[ref].add(p)
            if ref.startswith('cap-') and ref[4:].isdigit() and p in files:
                cap_path[int(ref[4:])] = p
    for p in files:
        m = re.fullmatch(r'decisions/undated/cap-(\d+)\.md', p)
        if m:
            cap_path.setdefault(int(m.group(1)), p)
    sc = scan(repo)
    recs = load_records(key, era)
    stats['records'] = len(recs)
    clusters = B.dedupe(recs)
    del recs
    stats['clusters'] = len(clusters)

    # 1. each case: held (by CAP id, or the same text under the same day and name or docket) or new
    assigned = {}  # path -> cluster record
    owner = defaultdict(set)  # ref -> paths of the cases it belongs to
    new, pending = [], []
    for k in clusters:
        ids = [k['id'], *(k.get('merged_cap_ids') or [])]
        held = [cap_path[i] for i in ids if i in cap_path]
        if held:
            p = cap_path.get(k['id']) or held[0]
            assigned.setdefault(p, k)
            for r in cap_refs_of(k):
                owner[r].add(p)
            if len(set(held)) > 1:
                stats['case_in_two_files'] += 1
        else:
            pending.append(k)
    cands = sorted({p for k in pending for p in B.held_candidates(k, {'by_key': sc['by_key']}) if p in files})
    texts = B.read_blobs(repo, base, cands)
    for k in pending:
        p = B.held_as(k, {'by_key': sc['by_key']}, texts) if B.held_candidates(k, {'by_key': sc['by_key']}) else None
        if p and p in files:
            stats['held_same_text'] += 1
        else:
            # one decision in two reporters whose OCR differs (official and regional): the same commit day, a citation
            # in common, and a like name (not a table page's citation shared by several cases)
            day = B.commit_day(k.get('decided'))
            nm = B.norm_name(k.get('name_abbreviation') or k.get('name') or '')
            p = next((q for c in k.get('citations') or [] for q in sorted(ref_paths.get(c, ())) if q in files
                      and sc['add'].get(q, {}).get('day') == day and similar_name(nm, sc['add'][q].get('name', ''))
                      # another reporter's print of it: two decisions on one page of one reporter are two decisions
                      and (assigned.get(q) or {}).get('reporter') != k.get('reporter')), None)
            if p:
                stats['held_shared_citation'] += 1
        if p and p in files:
            for r in cap_refs_of(k):
                owner[r].add(p)
            assigned.setdefault(p, k)
        else:
            new.append(k)
    del texts

    # 2. the new decisions, dated and rendered as build_state.main renders them
    ver = B.usc_versions(D / 'caselaw-law-mirrors' / 'united-states-law.git', ref='main')
    have = set(ver)
    used = set(files)
    new_items = []
    for k in new:
        k2, day = dated(k, a.today)
        if day is None:
            stats['new_undated_left_out'] += 1
            continue
        new_items.append((day, k2))
    new_items.sort(key=lambda x: (x[0], x[1].get('official') or '', str(x[1]['id'])))
    rendered = []
    for day, k in new_items:
        path, body, author, email, msg, rows, refs = B.render(k, day, have, ver)
        if path in used:
            path = path[:-3] + f"--cap-{k['id']}.md"
            rows = [(lp, {**row, 'decision': path}) for lp, row in rows]
        used.add(path)
        for r in cap_refs_of(k):
            owner[r].add(path)
        rendered.append((day, k, path, body, author, email, msg, rows, refs))
        assigned[path] = k
    stats['new_decisions'] = len(rendered)
    stats['new_head_matter_only'] = sum(not B.has_opinion(x[1]) for x in rendered)

    # 4. the index: every case's citations and CAP ids name its own file; a row giving one to another case's file
    #    (a wrong merge) is dropped
    rows_all = {r for rows in idx.values() for r in rows}
    drop = {(ref, p) for ref, p in rows_all if ref in owner and p not in owner[ref] and p in assigned}
    addrows = {(ref, p) for ref, ps in owner.items() for p in ps}
    for day, k, path, body, author, email, msg, rows, refs in rendered:
        addrows |= {(x, path) for x in refs}
    stats['index_rows_dropped'] = len(drop)
    stats['index_rows_added'] = len(addrows - rows_all)

    # 3. corrections of held files
    wrong_cites = defaultdict(set)  # path -> citations of another decision its file lists (a wrong merge)
    for ref, p in drop:
        if not ref.startswith('cap-'):
            wrong_cites[p].add(ref)
    need = [p for p, k in assigned.items() if p in files and (not B.has_opinion(k) or k.get('cap_court') is not None or p in wrong_cites
            or (len((k.get('decided') or '')) >= 10 and B.commit_day(k.get('decided')) != k['decided'][:10]))]
    held_texts = B.read_blobs(repo, base, sorted(need))
    corrections = []
    for p in sorted(need, key=lambda p: (sc['add'].get(p, {}).get('order', 0), p)):
        md = held_texts.get(p)
        if md is None or 'precedential_note:' not in md:
            continue  # not a build_state file (the federal repos' own builds): left as it is
        k, day = dated(assigned[p], a.today)
        if day is None:
            continue
        fm = front(md)
        why = []
        if body_empty(md) and (k.get('head_matter') or '').strip():
            why.append('Correction: text from CAP head matter (CAP holds no opinion text for this decision)')
            stats['corr_head_matter'] += 1
        if k.get('cap_court') is not None and fm.get('court') != k.get('court'):
            why.append(f"Correction: court {fm.get('court')!r} is CAP's name for a court that did not sit that day; "
                       f"now {k['court']!r} (cap_court keeps CAP's)")
            stats['corr_court'] += 1
        if k.get('decided_as_given') and not fm.get('decided_as_given'):
            why.append(f"Correction: decided_as_given {k['decided_as_given']} (a day the month does not have) added")
            stats['corr_decided_as_given'] += 1
        bad = sorted(wrong_cites.get(p, set()) & set(fm.get('citations') or []))
        if bad:
            why.append('Correction: citations of a different decision merged into this file by mistake removed: ' + '; '.join(bad)[:300])
            stats['corr_wrong_merge'] += 1
        if not why:
            continue
        info = sc['add'].get(p, {})
        extra = (f'Corrected: {a.today}', *(f"Corrects: {info['sha']}" for _ in [0] if info.get('sha')), *why)
        _, body, author, email, msg, _, _ = B.render(k, day, have, ver, extra=extra)
        if not info.get('at'):
            continue
        corrections.append((p, body, author, email, msg, info['at']))
    del held_texts

    # 5. relations: appended duplicates and re-added dedupe removals
    rel_old = git(repo, 'show', f'{base}:decision-relations.tsv', check=False) if 'decision-relations.tsv' in names else ''
    rels = {tuple(l.split('\t')[:4]) for l in rel_old.splitlines()[1:] if l.strip()}
    if slug in APPENDED:
        rels |= append_duplicates(repo, base, sc, files, stats)
        notes = dedupe_notes(repo)
        for p, kept in notes.items():
            if p in files and kept in files and p != kept:
                rels.add((p, 'same-decision-as', kept, 'removed as a duplicate on 2026-11-08 and added again by the CAP append'))
                stats['readded_marked'] += 1
    # 6. date precision
    dates_rows = set()
    for p, k in assigned.items():
        prec = B.date_precision(k.get('decided'))
        if prec:
            dates_rows.add((p, k['decided'], prec))
    stats['partial_dates'] = len(dates_rows)

    res = {'repo': slug, 'base': base, **stats}
    if a.dry_run:
        res['secs'] = round(time.time() - t0)
        res['examples_new'] = [x[2] for x in rendered[:5]]
        return res

    # 7. write: decisions (date order), corrections, documentation
    w = []
    clamp = sc['clamp']
    frm = base
    now = int(time.time())
    overlay = defaultdict(list)
    for day, k, path, body, author, email, msg, rows, refs in rendered:
        d0 = date.fromisoformat(day)
        if d0.year < 1970:
            clamp += 1
            ts = B.EPOCH_NOON + clamp
        else:
            ts = int(datetime(d0.year, d0.month, d0.day, 12, tzinfo=B.EASTERN).timestamp())
        fi_commit(w, author, email, ts, ts, msg, frm, {path: body})
        frm = None
        for lp, row in rows:
            overlay[lp].append({**row, 'decision': path})
    for p, body, author, email, msg, at in corrections:
        fi_commit(w, author, email, at, now, msg, frm, {p: body})
        frm = None
    docs = doc_files(repo, base, names, slug, key, idx, drop, addrows, overlay, rels, dates_rows, files, rendered, corrections,
                     assigned, sc, stats, a.today)
    n_dec, n_cor = len(rendered), len(corrections)
    msg = (f'Documentation: CAP deep-check fixes of {a.today}: {n_dec} decisions added, {n_cor} corrected; decision index, '
           f'decision-relations.tsv, decision-dates.tsv, overlay, corpus.json, README, REPRODUCE.md, tools\n\n'
           + '\n'.join(f'{v} {k.replace("_", " ")}' for k, v in sorted(stats.items()) if isinstance(v, int))
           + redates_block(slug, cap_path, sc) + '\n').encode('utf-8')
    fi_commit(w, 'Lexifina', 'corpus@lexifina.com', now, now, msg, frm, docs)
    w.append(b'done\n')
    run_fast_import(repo, w)
    res.update(finish(repo, slug, base, a))
    res['secs'] = round(time.time() - t0)
    return res


# CAP dates that are misread years (the head matter gives the true one: "(Argued October 14, 1889; decided November 1,
# 1839.)"): Redates: on the commit that added the file, as tools/law.py reads it (2026-11-09 deep check).
REDATES = {'new-york': {564462: '1889-11-01', 568203: '1890-12-02', 47756: '1906-05-08', 1963527: '1919-10-24', 2188477: '1883-02-06'}}


def redates_block(slug, cap_path, sc) -> str:
    lines = []
    for cid, day in sorted(REDATES.get(slug, {}).items()):
        p = cap_path.get(cid)
        sha = sc['add'].get(p, {}).get('sha') if p else None
        if sha:
            lines.append(f'Redates: {sha} {day}')
    return ('\n\n' + '\n'.join(lines)) if lines else ''


def run_fast_import(repo: Path, w: list):
    p = subprocess.Popen(['git', '-C', str(repo), 'fast-import', '--quiet', '--done', '--big-file-threshold=64m'], stdin=subprocess.PIPE)
    for chunk in w:
        p.stdin.write(chunk)
    p.stdin.close()
    if p.wait():
        raise SystemExit(f'{repo.name}: fast-import failed')


def append_duplicates(repo, base, sc, files, stats) -> set:
    """A decision appended from the CAP bulk data after an earlier decision of the same commit day and docket (or name,
    where neither gives a docket that differs), with the same text: (later, same-decision-as, earlier, note)."""
    groups = defaultdict(set)
    for kk, ps in sc['by_key'].items():
        live = [p for p in ps if p in files]
        if len(live) > 1:
            groups[kk] |= set(live)
    pairs = []
    for kk, ps in groups.items():
        ps = sorted(ps, key=lambda p: sc['add'][p]['order'])
        for i, later in enumerate(ps[1:], 1):
            li = sc['add'][later]
            if li['seg'] < 1 or not li['cap']:
                continue
            for earlier in ps[:i]:
                ei = sc['add'][earlier]
                if not kk[1].startswith('#'):
                    a_, b_ = B.docket_key(li['docket']), B.docket_key(ei['docket'])
                    if a_ and b_ and a_ != b_:
                        continue
                pairs.append((later, earlier, kk[1].startswith('#')))
    need = sorted({p for x in pairs for p in x[:2]})
    texts = B.read_blobs(repo, base, need)
    out, done = set(), set()
    for later, earlier, by_docket in pairs:
        if later in done or later not in texts or earlier not in texts:
            continue
        ta, tb = B.file_text(texts[later]), B.file_text(texts[earlier])
        if not by_docket and min(len(ta.strip()), len(tb.strip())) < 300:
            continue  # a short order with only a shared name: not enough to call it one decision
        if B.same_text(ta, tb):
            done.add(later)
            out.add((later, 'same-decision-as', earlier, 'appended from the CAP bulk data; the same decision (day, '
                     + ('docket' if by_docket else 'name') + ' and text) was already here'))
    stats['append_duplicates_marked'] = len(out)
    return out


def doc_files(repo, base, names, slug, key, idx, drop, addrows, overlay, rels, dates_rows, files, rendered, corrections,
              assigned, sc, stats, today) -> dict[str, bytes]:
    docs = {}
    for f, b in write_index(idx, drop, addrows).items():
        docs[f] = b
    if rels:
        docs['decision-relations.tsv'] = (REL_HEAD + '\n' + ''.join('\t'.join(r) + '\n' for r in sorted(rels))).encode('utf-8')
    old_dates = git(repo, 'show', f'{base}:decision-dates.tsv', check=False) if 'decision-dates.tsv' in names else ''
    dr = {tuple(l.split('\t')[:3]) for l in old_dates.splitlines()[1:] if l.strip()} | dates_rows
    if dr:
        docs['decision-dates.tsv'] = (DATES_HEAD + '\n' + ''.join('\t'.join(r) + '\n' for r in sorted(dr))).encode('utf-8')
    for lp, rows in overlay.items():
        f = f'judgments/{B.LAW_CORPUS}/{lp[:-3]}.jsonl'
        old = git(repo, 'show', f'{base}:{f}', check=False) if f in names else ''
        new = ''.join(json.dumps(x, ensure_ascii=False, sort_keys=True) + '\n' for x in sorted(rows, key=lambda x: (x['decided'], x['citation'])))
        docs[f] = (old + new).encode('utf-8')
    total = len(files) + len(rendered)
    hm = sum(1 for p, k in assigned.items() if not B.has_opinion(k))
    try:
        cj = json.loads(git(repo, 'show', f'{base}:corpus.json'))
    except ValueError:
        cj = {}
    cov = cj.get('coverage') or ''
    tail = cov.split(';', 1)[1].strip() if ';' in cov else ''
    cj['coverage'] = (f'{total} published decisions, one file each, dated the day decided: {total - hm} with their opinion text and '
                      f'{hm} with CAP head matter only (CAP holds no opinion text for them)' + (f'; {tail}' if tail else '.'))
    cj['checked_on'] = today
    cj['decision_relations'] = {'path': 'decision-relations.tsv', 'relations': {
        'same-decision-as': 'the file is the same decision as the target (same day, docket or name, and text); both are kept',
        'moved-to': 'the decision was routed here by mistake and is now at the target (<repo>/<path>); this file was removed'}}
    cj['decision_dates'] = {'path': 'decision-dates.tsv', 'note': 'decisions whose CAP date gives only the month or the year '
                            '(precision month or year): committed on the first day of it, In-force likewise'}
    firsts = [x[0] for x in rendered]
    if firsts and (not cj.get('history_from') or min(firsts) < cj['history_from']):
        cj['history_from'] = min(firsts)
    cj.setdefault('corrections', []).append({'on': today, 'decisions_added': len(rendered), 'files_corrected': len(corrections),
                                             'note': 'CAP deep-check fixes (README: Corrections)'})
    if key == 'new-jersey':
        cj['record_note'] = (cj.get('record_note') or '') + (' The Supreme Court of New Jersey before 15 September 1948 was not the '
                             'highest court (the Court of Errors and Appeals was): its decisions of before then are kept here '
                             'under the name CAP gives them; see the README.')
    docs['corpus.json'] = (json.dumps(cj, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    readme = git(repo, 'show', f'{base}:README.md')
    readme = readme.replace("| Decisions given in a window | `git log --since=1973-01-01 --until=1973-01-31 --format='%ad %an' --date=short` |",
                            "| Decisions given in a window | `git log --format='%(trailers:key=In-force,valueonly,separator=) %an' \\| awk '$1 >= \"1973-01-01\" && $1 <= \"1973-01-31\"'`: "
                            "read the `In-force:` trailer, not the commit date (see Corrections), or use the global-law tools |")
    docs['README.md'] = (readme.rstrip('\n') + '\n\n' + readme_section(slug, key, stats, sc, total, hm, rendered, corrections, rels, dates_rows, today)).encode('utf-8')
    rep = git(repo, 'show', f'{base}:REPRODUCE.md', check=False)
    docs['REPRODUCE.md'] = (rep.rstrip('\n') + f"""

## The fixes of {today}
After the build and its appends, `tools/cap_fix_forward.py` brought this repo in line with the fixed builder, as new
commits on main (nothing rewritten). Reproduce it from the same inputs:

```
python tools/cap_plan.py <cap-bulk>                     # the fixed router: plan.json and route.tsv
python tools/cap_route_extract.py <cap-bulk> <parsed>   # every volume whose marker its files do not match is extracted again
python tools/cap_route_extract.py <cap-bulk> <parsed> --verify
python tools/cap_fix_forward.py {slug} --today {today}  # decisions added, files corrected, index and relations
```

`tools/build_state.py` is the fixed builder (text-checked merges, head-matter fallback, counts checked against the
extraction and plan.json, the append filter on commit day and text). A fresh build with it gives the same decisions
and files as this repo's main after the fixes, though not the same history (here the fixes are later commits).
""").encode('utf-8')
    for f in TOOLS:
        if (HERE / f).exists():
            docs[f'tools/{f}'] = (HERE / f).read_bytes()
    return docs


def readme_section(slug, key, stats, sc, total, hm, rendered, corrections, rels, dates_rows, today) -> str:
    s = [f'## Corrections ({today})',
         f'An independent check of the CAP case-law repos found data defects; they are fixed here by new commits on top of the',
         f'history (nothing is rewritten). Now {total:,} decisions.', '']
    if rendered:
        s.append(f'- {len(rendered):,} decisions added, each as its own commit dated the day decided, in date order: decisions with '
                 'text the first build missed (volumes whose extract was lost, decisions merged with a different decision '
                 'of the same day, docket and name, decisions CAP files under no court name or under another place\'s '
                 'jurisdiction).')
    n_hm = stats.get('corr_head_matter', 0)
    if n_hm:
        s.append(f'- {n_hm:,} files had no text: CAP holds no opinion text for them, only head matter. Each now gives the head '
                 'matter under `## CAP head matter` (`text_source` in the front matter), in a `Kind: correction` commit on the '
                 'same file (`Corrected:`, `Corrects:` the commit that added it).')
    if stats.get('corr_court'):
        s.append(f"- {stats['corr_court']:,} decisions carried the name of a court that did not sit that day (CAP's); the court is "
                 'corrected and `cap_court` keeps CAP\'s name (correction commits).')
    if stats.get('corr_decided_as_given'):
        s.append(f"- {stats['corr_decided_as_given']:,} decisions with a day their month does not have now give CAP's date as "
                 '`decided_as_given` (correction commits).')
    if stats.get('index_rows_dropped') or stats.get('index_rows_added'):
        s.append(f"- decision-index.tsv: {stats.get('index_rows_added', 0):,} rows added (the citations and CAP ids of decisions "
                 f"held here under another reporter, and of the new decisions), {stats.get('index_rows_dropped', 0):,} rows that "
                 'gave a citation to a different decision removed.')
    if any(r[1] == 'same-decision-as' for r in rels):
        n = sum(1 for r in rels if r[1] == 'same-decision-as')
        s.append(f'- `decision-relations.tsv` lists {n:,} files as `same-decision-as` another file here: decisions the CAP '
                 'append added although the same decision (same day, docket or name, and text) was already here. Both files '
                 'are kept; the tools read the relation.')
    s.append(f'- `decision-dates.tsv` lists the {len(dates_rows):,} decisions whose CAP date gives only the month or the year '
             '(precision `month` or `year`); they are committed on the first day of it, and In-force says that day.')
    s.append('')
    s.append('Dates in the commit history: git cannot hold dates before 1970, so a decision before 1970 is committed from '
             '1970-01-01 noon UTC-5, a second apart.')
    if sc['spill']:
        s.append(f"Here {sc['pre1970']:,} decisions are before 1970, so {sc['spill']:,} of their commits are dated 1970-01-02 "
                 'or later' + (f", {sc['april']:,} of them in or after April 1970 (an append continued an earlier count)" if sc['april'] else '')
                 + '. The commit date is not the day decided: `git log --since/--until` over 1970 mixes them with the '
                 'decisions of 1970. Read the `In-force:` trailer (as the window recipe above now does) or use the '
                 'global-law tools.')
    else:
        s.append('Read the `In-force:` trailer for the day decided, not the commit date.')
    if key in FEDERAL:
        s += ['', "Head matter: this repo's first build (from CourtListener and CAP) includes each decision's head matter "
              "(syllabus, headnotes, counsel); the decisions appended from the CAP bulk data leave it out (the publisher's "
              'text), except where CAP has no opinion text, which gives the head matter under `## CAP head matter`.']
    if key == 'new-jersey':
        s += ['', 'New Jersey\'s courts: until 15 September 1948 the Court of Errors and Appeals was the highest court and the '
              '"Supreme Court of New Jersey" was below it (an intermediate court of law). Its decisions of before then are '
              'kept here under the name CAP gives them, with the court as named; read them as the decisions of that '
              'court, not of the highest court. CAP files 3,256 orders of 2015-2019 under the Court of Errors and Appeals, '
              'abolished in 1948: they are the Supreme Court of New Jersey\'s, and are corrected.']
    if slug == 'supreme-court':
        s += ['', 'The CAP append also left 44 empty commits (a re-append of decisions whose CAP date gives only the month or '
              'year); they change no file and cannot be removed without rewriting the history.']
    return '\n'.join(s) + '\n'


def finish(repo: Path, slug: str, base: str, a) -> dict:
    """check_corpus --strict, then the push (fetch first; fast-forward only, in steps of 40,000 commits)."""
    head = git(repo, 'rev-parse', 'main').strip()
    out = {'head': head, 'commits': int(git(repo, 'rev-list', '--count', f'{base}..{head}').strip())}
    gl = D / 'global-law-us-index'
    r = subprocess.run([sys.executable, '-X', 'utf8', 'tools/check_corpus.py', str(repo), '--strict'], cwd=gl,
                       capture_output=True, encoding='utf-8', errors='replace')
    out['check'] = r.returncode
    out['check_tail'] = (r.stdout + r.stderr).strip().splitlines()[-4:]
    if r.returncode:
        out['pushed'] = False
        return out
    if not a.push:
        out['pushed'] = False
        return out
    git(repo, 'fetch', '-q', 'origin', 'main')
    if git(repo, 'rev-parse', 'origin/main').strip() != base:
        raise SystemExit(f'{slug}: origin/main moved while fixing; not pushed (rerun)')
    shas = git(repo, 'rev-list', '--reverse', '--first-parent', f'{base}..{head}').split()
    steps = [shas[i] for i in range(CHUNK - 1, len(shas), CHUNK)] + [head]
    for s in steps:
        for i in range(5):
            p = subprocess.run(['git', '-C', str(repo), 'push', '-q', 'origin', f'{s}:refs/heads/main'], capture_output=True, text=True)
            if not p.returncode:
                break
            time.sleep(20 * (i + 1))
        else:
            raise SystemExit(f'push {slug}: {p.stderr[-300:]}')
    out['remote'] = git(repo, 'ls-remote', 'origin', 'main').split('\t')[0]
    out['pushed'] = out['remote'] == head
    return out


def moves(slug: str, a) -> dict:
    """Phase 2 in a repo the fixed router sends decisions away from: each file whose every CAP id now routes to another
    repo, and is held there (its CAP id in that repo's index on origin/main), is removed in one documentation commit
    (the federal dedupe's pattern), with its index and overlay rows, and listed as moved-to in decision-relations.tsv."""
    t0 = time.time()
    plan = json.loads((BULK / 'plan.json').read_text(encoding='utf-8'))
    key, _ = key_era(slug, plan)
    repo = ROOT / slug
    base = sync(repo)
    names = git(repo, 'ls-tree', '-r', '--name-only', base).splitlines()
    files = {n for n in names if n.startswith('decisions/') and n.endswith('.md')}
    idx = index_files(repo, names)
    new_route = {}
    with open(BULK / 'route.tsv', encoding='utf-8') as fh:
        for line in fh:
            c, r = line.rstrip('\n').split('\t')
            new_route[int(c)] = r
    caps = defaultdict(set)
    for rows in idx.values():
        for ref, p in rows:
            if ref.startswith('cap-') and ref[4:].isdigit() and p in files:
                caps[p].add(int(ref[4:]))
    leaving = {p: ids for p, ids in caps.items() if all(new_route.get(i, key) != key for i in ids)}
    dest_index = {}
    rels, skipped = set(), []
    for p, ids in sorted(leaving.items()):
        dests = {new_route[i] for i in ids}
        if len(dests) != 1:
            skipped.append((p, 'routed to several repos'))
            continue
        d = dests.pop()
        if d not in dest_index:
            drepo = ROOT / d
            git(drepo, 'fetch', '-q', 'origin', 'main')
            m = {}
            for f in [n for n in git(drepo, 'ls-tree', '--name-only', 'origin/main').split() if re.fullmatch(r'decision-index(?:-\d{4})?\.tsv', n)]:
                for line in git(drepo, 'show', f'origin/main:{f}').splitlines()[1:]:
                    ref, _, dp = line.partition('\t')
                    if ref.startswith('cap-'):
                        m[ref] = dp
            dest_index[d] = m
        dp = next((dest_index[d][f'cap-{i}'] for i in sorted(ids) if f'cap-{i}' in dest_index[d]), None)
        if not dp:
            skipped.append((p, f'not yet in {d}'))
            continue
        rels.add((p, 'moved-to', f'{d}/{dp}', f'the fixed router sends CAP {", ".join(map(str, sorted(ids)))} to {d}'))
    res = {'repo': slug, 'base': base, 'leaving': len(leaving), 'moved': len(rels), 'skipped': len(skipped), 'skipped_examples': skipped[:10]}
    if a.dry_run or not rels:
        res['secs'] = round(time.time() - t0)
        return res
    gone = {r[0] for r in rels}
    drop = {(ref, p) for rows in idx.values() for ref, p in rows if p in gone}
    docs = {f: b for f, b in write_index(idx, drop, set()).items()}
    rel_old = git(repo, 'show', f'{base}:decision-relations.tsv', check=False) if 'decision-relations.tsv' in names else ''
    allrels = {tuple(l.split('\t')[:4]) for l in rel_old.splitlines()[1:] if l.strip()} | rels
    docs['decision-relations.tsv'] = (REL_HEAD + '\n' + ''.join('\t'.join(r) + '\n' for r in sorted(allrels))).encode('utf-8')
    ov = [n for n in names if n.startswith('judgments/') and n.endswith('.jsonl')]
    for f, text in B.read_blobs(repo, base, ov).items():
        if any(g in text for g in gone):
            keep = [l for l in text.splitlines() if l.strip() and json.loads(l).get('decision') not in gone]
            docs[f] = ''.join(l + '\n' for l in keep).encode('utf-8')
    cj = json.loads(git(repo, 'show', f'{base}:corpus.json'))
    total = len(files) - len(gone)
    cj['coverage'] = re.sub(r'^\d+', str(total), cj.get('coverage') or f'{total} decisions')
    docs['corpus.json'] = (json.dumps(cj, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    by_dest = Counter(r[2].split('/', 1)[0] for r in rels)
    readme = git(repo, 'show', f'{base}:README.md').rstrip('\n') + (
        f"\n\n## Decisions moved to their own repos ({a.today})\n{len(rels):,} decisions were here by a routing mistake (CAP gives "
        'them a jurisdiction their court name contradicts, or a court name the router did not read). Each is now in its '
        'own repo, and its file here is removed; `decision-relations.tsv` gives each removed path as `moved-to '
        '<repo>/<path>`. ' + ', '.join(f'{n:,} to united-states-{d}-caselaw' for d, n in by_dest.most_common()) + f'. Now {total:,} decisions.\n')
    docs['README.md'] = readme.encode('utf-8')
    now = int(time.time())
    msg = (f'Documentation: {len(rels)} decisions moved to the repos of their courts\n\n'
           'The fixed router (cap_plan.py, 2026-11-09) sends these decisions to another repo, where each is now held. '
           'Each file here is removed; decision-index.tsv and overlay rows that named it are removed, and '
           'decision-relations.tsv lists it as moved-to its new path. No history is rewritten.\n\n'
           + '\n'.join(f'{r[0]} -> {r[2]}' for r in sorted(rels)) + '\n').encode('utf-8')
    w = []
    fi_commit(w, 'Lexifina', 'corpus@lexifina.com', now, now, msg, base, docs, deletes=sorted(gone))
    w.append(b'done\n')
    run_fast_import(repo, w)
    res.update(finish(repo, slug, base, a))
    res['secs'] = round(time.time() - t0)
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('repos', nargs='+'), ap.add_argument('--dry-run', action='store_true'), ap.add_argument('--push', action='store_true')
    ap.add_argument('--today', default='2026-11-09'), ap.add_argument('--moves', action='store_true')
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    for s in a.repos:
        if a.moves:
            res = moves(s, a)
        else:
            res = fix(s, a)
        line = json.dumps(res, ensure_ascii=False)
        print(line, flush=True)
        with open(OUT / ('dry.jsonl' if a.dry_run else 'fix.jsonl'), 'a', encoding='utf-8') as f:
            f.write(line + '\n')


if __name__ == '__main__':
    main()
