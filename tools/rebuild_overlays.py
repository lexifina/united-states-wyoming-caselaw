"""Rebuild the US Code judgment overlay of one US case-law repo from its own decision files, with the citation reader of
usc_cite.py, as a new Documentation commit on top of its main (append-only: nothing rewritten, no force).

  python -X utf8 rebuild_overlays.py <repo dir> [--tsv out.tsv] [--commit] [--push]

Every decision file at HEAD that may cite the US Code (one git grep) is read once; its front matter gives the day, name,
court and citation, and its opinion text (not the publisher's head matter) the citations. A file that decision-
relations.tsv names as the same decision as another, or as moved, gives no rows (the decision kept gives them once); a
decision redated by the maintainer (`Redates: <commit> <day>`) is read at its corrected day. Each row is pinned to the
section's version in the US Code record on the day decided (reconcile.fix_row), as build.py pins it.

--tsv writes the rows as cap-interplay-check's overlays.tsv does (to score before committing); --commit writes the
overlay files (and corpus.json's count, and the tools/ copies) in one fast-import commit from HEAD; --push pushes it
(git fetch first: if origin moved, nothing is pushed and the rebuild must run again on the new head)."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import Counter
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
_b = importlib.util.spec_from_file_location('us_build', HERE / 'build.py')
B = importlib.util.module_from_spec(_b)
_b.loader.exec_module(B)
LAW = Path.home() / 'Documents' / 'caselaw-law-mirrors' / 'united-states-law.git'
PREFILTER = r'U ?\.? ?S ?\.? ?C|United States Code|[Tt]itle [0-9]{1,2}'
CORRECTION_NOTE = ('each decision is added by one Kind: court commit; a Kind: correction commit (Corrects: the commit it '
                   "corrects, Corrected: the day) later fixes a file's text or details, dated with the same In-force day, and "
                   'is credited only with the lines it changed.')
REDATES = re.compile(r'^Redates:\s*([0-9a-f]{7,40})\s+(\d{4}-\d{2}-\d{2})\s*$', re.M)


def git(repo: Path, *args, inp: bytes | None = None, check: bool = True) -> str:
    r = subprocess.run(['git', '-C', str(repo), *args], input=inp, capture_output=True)
    if check and r.returncode:
        raise RuntimeError(f'git {args[0]}: {r.stderr.decode(errors="replace")[-400:]}')
    return r.stdout.decode('utf-8', 'replace')


def blobs(repo: Path, specs: list[str]):
    """(spec, text) for each <rev>:<path>, streamed from one cat-file --batch."""
    p = subprocess.Popen(['git', '-C', str(repo), 'cat-file', '--batch'], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    threading.Thread(target=lambda: (p.stdin.write(''.join(s + '\n' for s in specs).encode('utf-8')), p.stdin.close()), daemon=True).start()
    out = p.stdout
    for spec in specs:
        head = out.readline().split()
        if len(head) == 3 and head[1] == b'blob':
            data = out.read(int(head[2]))
            out.read(1)
            yield spec, data.decode('utf-8', 'replace')
        else:
            yield spec, None
    p.wait()


def front_matter(text: str) -> tuple[dict, str]:
    """(front matter, the opinions' text: the publisher's head matter left out)."""
    fm = {}
    body = text
    if text.startswith('---'):
        end = text.find('\n---', 3)
        for line in text[3:end].splitlines():
            k, sep, v = line.partition(': ')
            if sep:
                try:
                    fm[k] = json.loads(v)
                except ValueError:
                    fm[k] = v
        body = text[end + 4:]
    h = re.search(r'^# (.+)$', body, re.M)
    fm['_name'] = h.group(1).strip() if h else ''
    parts = re.split(r'(?m)^(## .*)$', body)
    keep = [parts[0]] if not h else []
    for i in range(1, len(parts) - 1, 2):
        if not re.match(r'## (?:CAP )?[Hh]ead matter', parts[i]):
            keep.append(parts[i + 1])
    return fm, '\n'.join(keep)


def day_of(decided) -> str | None:
    d = str(decided or '')[:10]
    if re.fullmatch(r'\d{4}-\d{2}', d):
        d += '-01'
    elif re.fullmatch(r'\d{4}', d):
        d += '-01-01'
    return d if re.fullmatch(r'\d{4}-\d{2}-\d{2}', d) else None


def relations(repo: Path) -> dict[str, str]:
    t = git(repo, 'show', 'HEAD:decision-relations.tsv', check=False)
    return {a[0]: a[1] for a in (l.split('\t') for l in t.splitlines()[1:]) if len(a) >= 3 and a[0]}


def redated(repo: Path) -> dict[str, str]:
    """decision path -> its corrected day, from the maintainer's Redates: trailers."""
    out = {}
    for m in REDATES.finditer(git(repo, 'log', '--author=Lexifina', '--grep=^Redates:', '--format=%B', 'HEAD', check=False)):
        for p in git(repo, 'show', '--name-only', '--format=', m.group(1), check=False).split():
            if p.startswith('decisions/') and p.endswith('.md'):
                out[p] = m.group(2)
    return out


def tax_court_reader():
    """The Tax Court's own way of citing the Code (bare "sec. 6662(a)" under its standing note, "I.R.C. § 61"), as
    tax-court-work/build_tc.py reads it when it builds lexifina/united-states-tax-court-caselaw."""
    f = Path.home() / 'Documents' / 'tax-court-work' / 'build_tc.py'
    spec = importlib.util.spec_from_file_location('build_tc', f)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.irc_cites


def rebuild(repo: Path, ver: dict, have: set) -> tuple[dict[str, list[dict]], dict]:
    head = git(repo, 'rev-parse', 'HEAD').strip()
    irc = tax_court_reader() if repo.name == 'tax-court' else None
    old_rows = {}
    for n in git(repo, 'ls-tree', '-r', '--name-only', head, 'judgments').split('\n'):
        if n.endswith('.jsonl'):
            old_rows[n] = None
    old_meta, old_total = {}, 0
    for spec, text in blobs(repo, [f'{head}:{n}' for n in old_rows]):
        old_rows[spec.split(':', 1)[1]] = text
        for line in (text or '').splitlines():
            try:
                j = json.loads(line)
            except ValueError:
                continue
            old_total += 1
            old_meta.setdefault(j.get('decision'), {'citation': j.get('citation'), 'name': j.get('name'), 'url': j.get('url'), 'court': j.get('court')})
    grep = subprocess.run(['git', '-C', str(repo), 'grep', '--threads=4', '-l', '-I', '-E', '-e', PREFILTER if irc is None else
                           PREFILTER + r'|[Ss]ec(tion)?s?\.? ?[0-9]|§|I\.R\.C', head, '--', 'decisions'],
                          capture_output=True)
    files = [l.split(':', 1)[1] for l in grep.stdout.decode('utf-8', 'replace').splitlines() if ':' in l]
    rel = relations(repo)
    redate = redated(repo)
    overlay: dict[str, list[dict]] = {}
    stats = Counter()
    for spec, text in blobs(repo, [f'{head}:{f}' for f in files]):
        path = spec.split(':', 1)[1]
        if text is None:
            continue
        if path in rel:
            stats['skipped_relation'] += 1
            continue
        fm, body = front_matter(text)
        day = redate.get(path) or day_of(fm.get('decided'))
        if not day:
            stats['undated'] += 1
            continue
        found = B.usc_cites(body, have, day, ver)
        if irc is not None:  # the Tax Court's bare section numbers, where the US Code reader found nothing at the place
            taken = {how for _, how in found}
            found += [(p, how) for p, how in irc(body, have, day, ver) if how not in taken]
        if not found:
            continue
        counts = Counter(p for p, _ in found)
        om = old_meta.get(path) or {}
        cites = fm.get('citations') or []
        citation = om.get('citation') or (cites[0] if cites else None) or fm.get('docket') or str(fm.get('cap_id') or path)
        done = set()
        for p, how in found:
            if p in done:
                continue
            done.add(p)
            row = {'decided': day, 'name': om.get('name') or fm['_name'], 'court': om.get('court') or fm.get('court') or '',
                   'citation': citation, 'url': om.get('url') or fm.get('source_url') or '', 'how': how, 'mentions': counts[p], 'decision': path}
            overlay.setdefault(p, []).append(B.RC.fix_row(row, p, ver))
            stats['rows'] += 1
    stats['old_rows'] = old_total
    stats['files_read'] = len(files)
    return overlay, {'head': head, 'old_files': old_rows, **stats}


def main():
    if os.name == 'nt':  # BelowNormal, inherited by git: the machine's law scrapers keep their share
        import ctypes
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
    ap = argparse.ArgumentParser()
    ap.add_argument('repo')
    ap.add_argument('--tsv', default='')
    ap.add_argument('--commit', action='store_true'), ap.add_argument('--push', action='store_true')
    ap.add_argument('--law-repo', default=str(LAW))
    a = ap.parse_args()
    repo = Path(a.repo)
    t0 = time.time()
    import shutil
    if shutil.disk_usage(str(repo.resolve().anchor)).free < 42 * 2**30:  # the machine's floor is 40 GB: stop short of it
        raise SystemExit(f'{repo.name}: under 42 GB free on the disk; not rebuilt')
    if a.commit or a.push:  # one rebuild of a repo at a time (side-by-side workers)
        lock = repo / '.git' / 'overlay-rebuild.lock'
        try:
            lock.mkdir()
        except FileExistsError:
            raise SystemExit(f'{repo.name}: another rebuild holds {lock}; left to it')
        import atexit
        atexit.register(lambda: lock.rmdir() if lock.exists() else None)
    if a.commit or a.push:  # start from origin's main: fast-forward to it, never past or around it
        git(repo, 'fetch', '-q', 'origin', 'main')
        local, remote = git(repo, 'rev-parse', 'refs/heads/main').strip(), git(repo, 'rev-parse', 'origin/main').strip()
        if local != remote:
            if subprocess.run(['git', '-C', str(repo), 'merge-base', '--is-ancestor', local, remote]).returncode == 0:
                git(repo, 'update-ref', 'refs/heads/main', remote, local)
            else:
                raise SystemExit(f'{repo.name}: local main {local[:10]} is not behind origin/main {remote[:10]}; left alone')
    ver = B.usc_versions(Path(a.law_repo), ref='main')
    law_head = git(Path(a.law_repo), 'rev-parse', 'main').strip()
    titles = sorted({int(p.split('/')[0][6:]) for p in ver if re.match(r'title-\d+/', p)})
    have = set(ver)
    overlay, st = rebuild(repo, ver, have)
    name = repo.name
    if a.tsv:
        with open(a.tsv, 'a', encoding='utf-8', newline='\n') as f:
            for p, rows in overlay.items():
                t, s = re.match(r'title-(\w+)/(.+)\.md$', p).groups()
                for r in rows:
                    f.write('\t'.join(str(x).replace('\t', ' ') for x in (name, t, s, r['decided'], r['citation'], r['decision'], r['url'],
                                                                          r['how'], r.get('law_commit', ''), r.get('law_record_from', ''),
                                                                          r['mentions'], r['court'])) + '\n')
    n_rows = sum(len(v) for v in overlay.values())
    if a.commit:
        docs = {}
        for p, rows in overlay.items():
            rows.sort(key=lambda x: (x['decided'], x['citation'], x['decision']))
            docs[f'judgments/{B.LAW_CORPUS}/{p[:-3]}.jsonl'] = ''.join(json.dumps(x, ensure_ascii=False, sort_keys=True) + '\n' for x in rows)
        gone = [f for f in st['old_files'] if f not in docs]
        cj_text = git(repo, 'show', f"{st['head']}:corpus.json", check=False)
        try:  # the overlay as before, read against the same titles, and the record note already right: nothing to commit
            cj_old = json.loads(cj_text)
            described = (cj_old.get('overlay') or {}).get('titles') == titles and 'every commit is Kind: court.' not in cj_old.get('record_note', '')
        except ValueError:
            described = True
        if not gone and all(st['old_files'].get(p) == t for p, t in docs.items()) and described:
            print(json.dumps({'repo': name, 'rows': n_rows, 'unchanged': True, 'pushed': True, 'head': st['head']}), flush=True)
            return
        try:
            cj = json.loads(cj_text)
            cj['coverage'] = re.sub(r'\d+ citations of the US Code linked', f'{n_rows} citations of the US Code linked', cj.get('coverage', ''))
            cj['overlay'] = {**(cj.get('overlay') or {}), 'rebuilt': date.today().isoformat(), 'law_commit': law_head,
                             'titles': titles,
                             'reader': 'tools/usc_cite.py: a page stamp ("USCA4") is no citation, "(a)(4)" a subsection, a range '
                                       '("§§ 101-1330", "§§ 1 through 16") links neither end (its ends are no more cited than the '
                                       'sections between them); a citation under an older numbering (before the title was recodified, '
                                       'before the section was added, an earlier edition, "former") is not linked; one row per decision '
                                       '(decision-relations.tsv duplicates left out)'}
            note = cj.get('record_note') or ''
            if 'every commit is Kind: court.' in note:
                cj['record_note'] = note.replace('every commit is Kind: court.', CORRECTION_NOTE)
            docs['corpus.json'] = json.dumps(cj, ensure_ascii=False, indent=2) + '\n'
        except ValueError:
            pass
        tracked = set(git(repo, 'ls-tree', '--name-only', st['head'], 'tools/').split())
        for f in ('usc_cite.py', 'build.py', 'build_state.py', 'rebuild_overlays.py'):
            if f'tools/{f}' in tracked or f in ('usc_cite.py', 'rebuild_overlays.py'):
                docs[f'tools/{f}'] = (HERE / f).read_text(encoding='utf-8')
        today = date.today().isoformat()
        ts = int(time.time())
        msg = (f'Documentation: judgment overlay rebuilt with the US Code citation reader of {today} ({n_rows} citations, '
               f'was {st["old_rows"]})\n\nPACER page stamps ("USCA4", "USCA11") are no longer read as citations, subsection '
               f'digits ("(a)(4)") no longer as sections, and a citation under an older numbering than the record\'s (a decision '
               f'before the title\'s recodification or before the section was added, an earlier edition, "former") is no longer '
               f'linked to today\'s section. Lists, "U.S.C.S.", "U.S.C., Sec." and long forms are read. '
               f'Each decision gives its rows once: files decision-relations.tsv names as duplicates give none.\n\nRead against the US '
               f'Code record at {law_head[:10]} (titles {", ".join(map(str, titles))}): title 26 is linked from this rebuild on, a '
               f'citation of the Internal Revenue Code of 1939 (a decision before 1954-08-16, or a pre-1954 edition) left out. A '
               f'range ("§§ 101-1330") links neither of its ends.\n').encode('utf-8')
        w = [b'commit refs/heads/main\n', b'author Lexifina <corpus@lexifina.com> %d +0000\ncommitter %s %d +0000\n' % (ts, B.COMMITTER, ts),
             b'data %d\n%s\n' % (len(msg), msg), b'from %s\n' % st['head'].encode()]
        for f in gone:
            w.append(b'D %s\n' % f.encode('utf-8'))
        for p in sorted(docs):
            b_ = docs[p].encode('utf-8')
            w.append(b'M 100644 inline %s\ndata %d\n%s\n' % (p.encode('utf-8'), len(b_), b_))
        w.append(b'done\n')
        if git(repo, 'rev-parse', 'refs/heads/main').strip() != st['head']:
            raise SystemExit(f'{name}: main moved during the rebuild; run again')
        git(repo, 'fast-import', '--quiet', '--done', inp=b''.join(w))
        st['new_head'] = git(repo, 'rev-parse', 'refs/heads/main').strip()
        if a.push:
            git(repo, 'fetch', '-q', 'origin', 'main')
            if git(repo, 'rev-parse', 'origin/main').strip() != st['head']:
                # leave main where origin has it, not on a commit beside it (another agent works in this clone too)
                git(repo, 'update-ref', 'refs/heads/main', git(repo, 'rev-parse', 'origin/main').strip(), st['new_head'], check=False)
                raise SystemExit(f'{name}: origin/main moved since {st["head"][:10]}; nothing pushed, rebuild on the new head')
            for i in range(5):
                pr = subprocess.run(['git', '-C', str(repo), '-c', 'http.postBuffer=1048576000', 'push', '-q', 'origin', 'main'], capture_output=True)
                if not pr.returncode:
                    st['pushed'] = True
                    break
                time.sleep(30 * (i + 1))
            else:
                raise SystemExit(f'{name}: push failed: {pr.stderr.decode(errors="replace")[-300:]}')
    print(json.dumps({'repo': name, 'rows': n_rows, **{k: v for k, v in st.items() if k != 'old_files'}, 'secs': round(time.time() - t0, 1)}), flush=True)


if __name__ == '__main__':
    main()
