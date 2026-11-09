# US case law: The Supreme Court of Wyoming

Part of [lexifina/global-law](https://github.com/lexifina/global-law), one git history for every country's law: this is the case law of the Supreme Court of Wyoming, United States, which global-law reads at `united-states/wyoming/caselaw/`.

Every published opinion of the Supreme Court of Wyoming in the Caselaw Access Project, as a git history: one file per decision, one commit
per decision, dated the day it was decided. 10,894 decisions from 1870-05-01 to the end of the source (P.2d, P.3d, Wyo.).

## Questions as git commands

| Question | Command |
|---|---|
| A decision by its citation | `grep -P '^<citation>\t' decision-index.tsv` then `cat <path>` |
| Decisions given in a window | `git log --format='%(trailers:key=In-force,valueonly,separator=) %an' \| awk '$1 >= "1973-01-01" && $1 <= "1973-01-31"'`: read the `In-force:` trailer, not the commit date (see Corrections), or use the global-law tools |
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

## Corrections (2026-11-09)
An independent check of the CAP case-law repos found data defects; they are fixed here by new commits on top of the
history (nothing is rewritten). Now 10,897 decisions.

- 3 decisions added, each as its own commit dated the day decided, in date order: decisions with text the first build missed (volumes whose extract was lost, decisions merged with a different decision of the same day, docket and name, decisions CAP files under no court name or under another place's jurisdiction).
- 2 files had no text: CAP holds no opinion text for them, only head matter. Each now gives the head matter under `## CAP head matter` (`text_source` in the front matter), in a `Kind: correction` commit on the same file (`Corrected:`, `Corrects:` the commit that added it).
- decision-index.tsv: 46 rows added (the citations and CAP ids of decisions held here under another reporter, and of the new decisions), 5 rows that gave a citation to a different decision removed.
- `decision-dates.tsv` lists the 142 decisions whose CAP date gives only the month or the year (precision `month` or `year`); they are committed on the first day of it, and In-force says that day.

Dates in the commit history: git cannot hold dates before 1970, so a decision before 1970 is committed from 1970-01-01 noon UTC-5, a second apart.
Read the `In-force:` trailer for the day decided, not the commit date.
