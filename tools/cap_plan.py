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


# Corrections to CAP's court names (2026-11-09 deep check), applied before routing by cap_plan and cap_route_extract
# alike (cap_court keeps CAP's name):
# - CAP court id 25221 has no name; its 307 decisions (S.C., S.E.2d 2006-2016, court_abbr "S.C.") are the Supreme
#   Court of South Carolina by their own head matter ("No. 26219. Supreme Court of South Carolina.").
COURT_BY_ID = {'25221': 'Supreme Court of South Carolina'}
# - a court CAP names for a day it did not sit: (CAP's name, the court that sat, from, until) (until exclusive).
RENAMES = [('New York Court for the Correction of Errors', 'New York Court of Appeals', '1848-01-01', '9999'),
           ('Delaware Court of Errors and Appeals', 'Delaware Supreme Court', '1951-07-01', '9999'),
           # abolished by the 1947 Constitution (in force 1948-09-15); CAP files 3,256 orders of 2015-2019 under it
           ('New Jersey Court of Errors and Appeals', 'Supreme Court of New Jersey', '1948-09-15', '9999'),
           # the Supreme Court of Kentucky sits from 1976; before, the Kentucky Court of Appeals was the highest court
           # (and before statehood, 1783-1792, the Supreme Court for the District of Kentucky: 1 Hughes)
           ('Supreme Court of Kentucky', 'Supreme Court for the District of Kentucky', '0000', '1792-06-01'),
           ('Supreme Court of Kentucky', 'Kentucky Court of Appeals', '1792-06-01', '1976-01-01'),
           # 2 Dall. 1-41 (1781-1787): the Court of Appeals in Cases of Capture of the Continental Congress, not the
           # Federal Circuit (1982)
           ('United States Court of Appeals for the Federal Circuit', 'Court of Appeals in Cases of Capture', '0000', '1982-10-01')]
# CAP names a court with a different jurisdiction than its own: these names are another place's court, and are
# routed as such (only where the reporter is a regional one that prints several states: "North Carolina Court of
# Appeals" in the South Carolina Law reports is CAP's name for South Carolina's old Court of Appeals, and "Kansas
# Court of Appeals" in Missouri Appeal Reports is the Kansas City Court of Appeals).
NAMED_PLACE = {'Supreme Court of Appeals of West Virginia': 'West Virginia', 'District of Columbia Court of Appeals': 'District of Columbia',
               'Court of Appeals of the District of Columbia': 'District of Columbia'}
REGIONAL = {'a', 'a2d', 'a3d', 'ne', 'ne2d', 'ne3d', 'nw', 'nw2d', 'se', 'se2d', 'so', 'so2d', 'so3d', 'sw', 'sw2d', 'sw3d', 'p', 'p2d', 'p3d'}
# Courts CAP gives the jurisdiction "United States" that are a state's, a territory's or the District's own (104 in
# federal-special until 2026-11-09): routed as that place's courts.
LOCAL_UNDER_US = {'Commonwealth Court of Pennsylvania': ('Commonwealth Court of Pennsylvania', 'Pennsylvania'),
                  'Florida Superior Court': ('Florida Superior Court', 'Florida'),
                  'District of Columbia Court of Appeals': ('District of Columbia Court of Appeals', 'District of Columbia'),
                  'District of Columbia Criminal Court': ('District of Columbia Criminal Court', 'District of Columbia'),
                  "District of Columbia Orphans' Court": ("District of Columbia Orphans' Court", 'District of Columbia'),
                  'United States Supreme Court of the District of Columbia': ('Supreme Court of the District of Columbia', 'District of Columbia'),
                  'Delaware Court of Chancery': ('Delaware Court of Chancery', 'Delaware'),
                  'Superior Court of the Territory of Utah': ('Superior Court of the Territory of Utah', 'Utah'),
                  'Superior Court, D. Arkansas': ('Superior Court, D. Arkansas', 'Arkansas'),
                  'Dakota Territorial Court': ('Dakota Territorial Court', 'Dakota Territory'),
                  'District Court of the Virgin Islands': ('District Court of the Virgin Islands', 'Virgin Islands'),
                  'District Court of the Virgin Islands, Division of St. Croix': ('District Court of the Virgin Islands', 'Virgin Islands')}
_PLACES = sorted(set(CIRCUIT) | {'D.C.', 'New Mexico'}, key=len, reverse=True)


def court_name(court: str, court_id, day: str) -> tuple[str, str | None]:
    """(the court's name to use, CAP's name where it differs, else None)."""
    cap = court
    if not court and str(court_id or '') in COURT_BY_ID:
        court = COURT_BY_ID[str(court_id)]
    for old, new, since, until in RENAMES:
        if court == old and since <= (day or '') < until:
            court = new
    return court, (cap if court != cap else None)


def _district_place(rest: str) -> str | None:
    """The state a federal district court sits in, from what follows "District Court" ("for the District of D.C.",
    ", D. South Dakota, Southern Division", ", S.D. West Virginia, Charleston Division")."""
    rest = rest.replace('the ', '')
    for s in _PLACES:
        if re.search(r'(?<![A-Za-z])' + re.escape(s) + r'(?![A-Za-z])', rest):
            return 'District of Columbia' if s == 'D.C.' else s
    return None


def route(court: str, jur: str, day: str, reporter: str = '') -> str | None:
    if court.startswith('nited States '):  # CAP's OCR dropped the U (164 decisions of the District of New Mexico)
        court = 'U' + court
    if court in LOCAL_UNDER_US and (jur == 'United States' or court.startswith('United States ')):
        court, jur = LOCAL_UNDER_US[court]
    elif jur == 'United States' and not court.startswith('United States '):
        m = re.match(r'(?:(?:Northern|Southern|Eastern|Western|Middle|Central) District of|Circuit Court for the District of) (.+)$', court)
        if m and _district_place(m.group(1)):
            return f'{CIRCUIT[_district_place(m.group(1))]}-district'
    if court in NAMED_PLACE and NAMED_PLACE[court] != jur and reporter in REGIONAL:
        jur = NAMED_PLACE[court]
    if jur == 'United States' or court.startswith('United States ') or court == 'Court of Appeals in Cases of Capture':
        if court == 'Supreme Court of the United States':
            return 'supreme-court'
        m = re.match(r'United States (?:Circuit )?Court of Appeals for the (\w+)(?: Circuitt?)?$', court)
        if m and m.group(1) in APPEALS:
            return APPEALS[m.group(1)]
        if re.match(r'United States Court of Appeals(?: for the|,) District of Columbia', court) or court == 'United States Court of Appeals for the District of Columbia':
            return 'cadc'
        if court == 'United States Court of Appeals for the Federal Circuit':
            return 'cafc'
        if re.search(r'Bankruptcy', court):
            return 'bankruptcy'
        m = re.match(r'United States (?:District|Circuit) Court\b,? ?(.+)$', court)
        if m and _district_place(m.group(1)):
            return f'{CIRCUIT[_district_place(m.group(1))]}-district'
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
    # An existing plan's folds and era splits are kept (the repos they name exist): a re-plan after a routing fix
    # moves decisions between the repos that exist, and does not make new ones or move an era's boundary.
    old = json.loads((B / 'plan.json').read_text(encoding='utf-8')) if (B / 'plan.json').exists() else None
    skipped = Counter()
    rows = []
    bucket_n = Counter()
    with open(B / 'inventory.tsv', encoding='utf-8') as fh:
        for line in fh:
            cid, rep, vol, court_id, court, jur, day, cites, docket = line.rstrip('\n').split('\t')
            court, _ = court_name(court, court_id, day)
            r = route(court, jur, day, rep)
            if r is None:
                skipped[(court, jur)] += 1
                continue
            rows.append((cid, r, court, rep, day))
            bucket_n[r] += 1
    # A bucket of a state under MIN decisions joins the state's other courts, and other courts under MIN join the
    # state's repo: a repo of a handful of decisions is not worth its own corpus.
    fold = dict(old['folded']) if old else {}
    for b, n in list(bucket_n.items()):
        if old and (b in old['repos'] or b in fold):
            continue
        if b.endswith('-appellate') and n < MIN:
            fold[b] = b.removesuffix('-appellate') + '-other-courts'
            bucket_n[fold[b]] += n
    for b, n in list(bucket_n.items()):
        if old and (b in old['repos'] or b in fold):
            continue
        if b.endswith('-other-courts') and n < MIN and b not in fold:
            fold[b] = b.removesuffix('-other-courts')
            bucket_n[fold[b]] += n
    for a in list(fold):
        while fold[a] in fold:
            fold[a] = fold[fold[a]]
    if old:
        new_keys = {fold.get(b, b) for b in bucket_n} - set(old['repos'])
        if new_keys:
            raise SystemExit(f'routing makes repos that do not exist: {sorted(new_keys)}')
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
        if old and r in old['repos'] and old['repos'][r]['split']:
            plan[r]['split'] = [[s, e, sum(n for y, n in years if s <= y <= e)] for s, e, _ in old['repos'][r]['split']]
        elif x['n'] > MAX and r not in FEDERAL:
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
