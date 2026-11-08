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
