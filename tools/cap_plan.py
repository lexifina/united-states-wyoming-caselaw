"""Which repo each CAP case goes to (granularity per the US case-law sizing report): per state, its highest court
(united-states-<state>-caselaw), its intermediate appellate courts (-appellate) and its other courts (-other-courts);
federal district courts by circuit (united-states-<cN>-district-caselaw), bankruptcy courts (-bankruptcy), the other
federal courts (Claims, Customs, Tax, military ...: -federal-special); the Supreme Court and the courts of appeals go to
the 14 existing repos (only what they miss is appended); territories as states; tribal courts in one repo. A repo over
MAX decisions is split by era at year boundaries (-<from>-<to>).

  python cap_plan.py <cap-bulk dir>   -> <cap-bulk>/plan.json (repo -> courts, counts, dates) and route.tsv (case id -> repo)"""
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

MAX = 300_000
MIN = 500
CIRCUIT = {}
for c, states in {
    'ca1': 'Maine|Massachusetts|New Hampshire|Rhode Island|Puerto Rico',
    'ca2': 'Connecticut|New York|Vermont',
    'ca3': 'Delaware|New Jersey|Pennsylvania|Virgin Islands',
    'ca4': 'Maryland|North Carolina|South Carolina|Virginia|West Virginia',
    'ca5': 'Louisiana|Mississippi|Texas|Canal Zone',
    'ca6': 'Kentucky|Michigan|Ohio|Tennessee',
    'ca7': 'Illinois|Indiana|Wisconsin',
    'ca8': 'Arkansas|Iowa|Minnesota|Missouri|Nebraska|North Dakota|South Dakota|Dakota',
    'ca9': 'Alaska|Arizona|California|Hawaii|Idaho|Montana|Nevada|Oregon|Washington|Guam|Northern Mariana Islands',
    'ca10': 'Colorado|Kansas|New Mexico|Oklahoma|Utah|Wyoming|Indian Territory',
    'ca11': 'Alabama|Florida|Georgia',
    'cadc': 'District of Columbia|Columbia'}.items():
    for s in states.split('|'):
        CIRCUIT[s] = c
APPEALS = {'First': 'ca1', 'Second': 'ca2', 'Third': 'ca3', 'Fourth': 'ca4', 'Fifth': 'ca5', 'Sixth': 'ca6', 'Seventh': 'ca7',
           'Eighth': 'ca8', 'Ninth': 'ca9', 'Tenth': 'ca10', 'Eleventh': 'ca11', 'Six': 'ca6'}
STATE_HIGH = {  # names that are a state's highest court though the general rule would not say so
    'New York Court of Appeals', 'Maryland Court of Appeals', 'District of Columbia Court of Appeals', 'Texas Court of Criminal Appeals',
    'Oklahoma Court of Criminal Appeals', 'Supreme Court of Errors of Connecticut', 'Connecticut Supreme Court of Errors',
    'High Court of American Samoa'}
NOT_HIGH = re.compile(r'New York Supreme Court|Supreme Court,? Appellate|Appellate Division|Appellate Term|Supreme Court of the District of Columbia'
                      r'|Supreme Court of New York|Supreme Court for the|^Supreme Court, |Trial Term|Special Term|General Term')
HIGH = re.compile(r'Supreme Court|Supreme Judicial Court|Court of Errors and Appeals|Court of Errors|Supreme Court of Appeals|Court for the Correction of Errors'
                  r'|High Court of Errors')
HIGH_BY_JUR = {'Maryland': r'Court of Appeals', 'New York': r'^(?:New York )?Court of Appeals', 'Texas': r'Court of Criminal Appeals',
               'Oklahoma': r'Court of Criminal Appeals', 'District of Columbia': r'^(?:District of Columbia Court of Appeals|Court of Appeals of the District of Columbia)$'}
INTERMEDIATE = re.compile(r'Courts? of Appeals?|Courts? of Civil Appeals|Commission of Appeals|Appellate Court|Appellate Division|Appeals Court|Court of Civil Appeals|Court of Criminal Appeals'
                          r'|District Court of Appeal|Commonwealth Court|Superior Court of Pennsylvania|Pennsylvania Superior Court|Appellate Term'
                          r'|Intermediate Court|Court of Special Appeals', re.I)


def slug(s):
    return re.sub(r'[^a-z0-9]+', '-', s.lower()).strip('-')


def route(court: str, jur: str, day: str) -> str | None:
    if jur == 'United States' or court.startswith('United States '):  # a federal court, whatever the jurisdiction CAP gives it
        if court == 'Supreme Court of the United States':
            return 'supreme-court'
        m = re.match(r'United States (?:Circuit )?Court of Appeals for the (\w+) Circuit', court)
        if m and m.group(1) in APPEALS:
            return APPEALS[m.group(1)]
        if re.match(r'United States Court of Appeals(?: for the|,) District of Columbia', court) or court == 'United States Court of Appeals for the District of Columbia':
            return 'cadc'
        if court == 'United States Court of Appeals for the Federal Circuit':
            return 'cafc'
        if re.search(r'Bankruptcy', court):
            return 'bankruptcy'
        m = re.match(r'United States (?:District|Circuit) Court(?: for the)?(?: (?:Northern|Southern|Eastern|Western|Middle|Central) District of| District of| District Court of|) ?(.+)$', court)
        if m:
            place = m.group(1).replace('the ', '').strip()
            for s, c in sorted(CIRCUIT.items(), key=lambda x: -len(x[0])):
                if place.startswith(s) or place.endswith(s):
                    return f'{c}-district'
        return 'federal-special'
    if jur in ('Tribal Jurisdictions', 'Navajo Nation'):
        return 'tribal'
    if jur == 'United Kingdom':
        return None
    st = slug(jur)
    by_jur = HIGH_BY_JUR.get(jur)
    if by_jur and re.search(by_jur, court) and not re.search(r'Special Appeals|Appellate Division|Appellate Term', court):
        return st
    if court in STATE_HIGH or (court == 'Kentucky Court of Appeals' and day < '1976-01-01') or (HIGH.search(court) and not NOT_HIGH.search(court)):
        return st
    if INTERMEDIATE.search(court):
        return f'{st}-appellate'
    return f'{st}-other-courts'


if __name__ == '__main__':
    B = Path(sys.argv[1])
    skipped = Counter()
    rows = []
    bucket_n = Counter()
    with open(B / 'inventory.tsv', encoding='utf-8') as fh:
        for line in fh:
            cid, rep, vol, court_id, court, jur, day, cites, docket = line.rstrip('\n').split('\t')
            r = route(court, jur, day)
            if r is None:
                skipped[(court, jur)] += 1
                continue
            rows.append((cid, r, court, rep, day))
            bucket_n[r] += 1
    # A bucket of a state under MIN decisions joins the state's other courts, and other courts under MIN join the
    # state's repo: a repo of a handful of decisions is not worth its own corpus.
    fold = {}
    for b, n in list(bucket_n.items()):
        if b.endswith('-appellate') and n < MIN:
            fold[b] = b.removesuffix('-appellate') + '-other-courts'
            bucket_n[fold[b]] += n
    for b, n in list(bucket_n.items()):
        if b.endswith('-other-courts') and n < MIN and b not in fold:
            fold[b] = b.removesuffix('-other-courts')
            bucket_n[fold[b]] += n
    for a in list(fold):
        while fold[a] in fold:
            fold[a] = fold[fold[a]]
    repos = defaultdict(lambda: {'n': 0, 'courts': Counter(), 'reporters': Counter(), 'years': Counter()})
    with open(B / 'route.tsv.part', 'w', encoding='utf-8', newline='\n') as fo:
        for cid, r, court, rep, day in rows:
            r = fold.get(r, r)
            fo.write(f'{cid}\t{r}\n')
            x = repos[r]
            x['n'] += 1
            x['courts'][court] += 1
            x['reporters'][rep] += 1
            x['years'][day[:4]] += 1
    Path(B / 'route.tsv.part').replace(B / 'route.tsv')
    FEDERAL = {'supreme-court', 'cadc', 'cafc'} | {f'ca{i}' for i in range(1, 12)}
    plan = {}
    for r, x in sorted(repos.items()):
        years = sorted(x['years'].items())
        plan[r] = {'n': x['n'], 'from': years[0][0], 'to': years[-1][0], 'courts': dict(x['courts'].most_common()),
                   'reporters': dict(x['reporters'].most_common()), 'split': None, 'existing': r in FEDERAL}
        if x['n'] > MAX and r not in FEDERAL:
            parts, cur, start = [], 0, years[0][0]
            target = x['n'] / -(-x['n'] // MAX)
            for y, n in years:
                if cur and cur + n > target * 1.02:
                    parts.append([start, prev, cur])
                    start, cur = y, 0
                cur += n
                prev = y
            parts.append([start, years[-1][0], cur])
            plan[r]['split'] = parts
    (B / 'plan.json').write_text(json.dumps({'repos': plan, 'folded': fold, 'skipped': {f'{c} ({j})': n for (c, j), n in skipped.items()}},
                                            indent=1, ensure_ascii=False), encoding='utf-8')
    for r, p in sorted(plan.items(), key=lambda kv: -kv[1]['n']):
        print(f"{r:40s} {p['n']:8d} {p['from']}-{p['to']} courts {len(p['courts']):4d}  {'split ' + str([s[:2] for s in p['split']]) if p['split'] else ''}")
    print(len(plan), 'repos;', sum(p['n'] for p in plan.values()), 'cases; skipped', sum(skipped.values()), '; folded', len(fold))
