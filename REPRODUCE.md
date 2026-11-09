# Reproducing this repository

```
python tools/cap_bulk_fetch.py <cap-bulk>                  # every volume zip of static.case.law, verified
python tools/cap_bulk_extract.py <cap-bulk> <parsed> --reporters <reporters> --courts '<court regex>' [--after <day>]
python tools/cap_plan.py <cap-bulk>                       # which repo each case goes to: plan.json
python tools/cap_route_extract.py <cap-bulk> <parsed>      # every case, filed by its repo
python tools/build_state.py wyoming <parsed>/wyoming <empty folder> --today 2026-11-08 --plan <cap-bulk>/plan.json
```

Input: the Caselaw Access Project's bulk files on static.case.law (CC0): VolumesMetadata.json and the volume zips it lists,
fetched 2026-11-08 at 16 to 32 parallel streams (about 12 MB/s), backing off on any error. static.case.law's robots.txt
disallows all paths (`User-agent: *` / `Disallow: /`); its documentation offers these files for bulk download, and they
were fetched as such, with no listing page crawled. Gives 10,894 decision commits and one documentation commit.

## The fixes of 2026-11-09
After the build and its appends, `tools/cap_fix_forward.py` brought this repo in line with the fixed builder, as new
commits on main (nothing rewritten). Reproduce it from the same inputs:

```
python tools/cap_plan.py <cap-bulk>                     # the fixed router: plan.json and route.tsv
python tools/cap_route_extract.py <cap-bulk> <parsed>   # every volume whose marker its files do not match is extracted again
python tools/cap_route_extract.py <cap-bulk> <parsed> --verify
python tools/cap_fix_forward.py wyoming --today 2026-11-09  # decisions added, files corrected, index and relations
```

`tools/build_state.py` is the fixed builder (text-checked merges, head-matter fallback, counts checked against the
extraction and plan.json, the append filter on commit day and text). A fresh build with it gives the same decisions
and files as this repo's main after the fixes, though not the same history (here the fixes are later commits).
