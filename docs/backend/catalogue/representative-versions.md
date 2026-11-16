# Representative versions and activity

Every feature reads one representative version per course, chosen by a fixed preference from the stored versions and their curriculum-period memberships.

## One representative per course

A course is one canonical identity shared by all of its official versions. A [Course version](/docs/reference/glossary.md#courses-and-the-catalogue) is one exact official version as published for one [Curriculum period](/docs/reference/glossary.md#courses-and-the-catalogue), with its own code, names, credits, official URL, teaching text, prerequisite options, and substitution options.

Every feature reads one of those versions, the representative version, so search, comparison, prerequisites, discovery, and the structure of studies describe the same official data for a course. The selection runs on every read that needs a representative, over the versions and the period memberships the database holds.

## Choosing the representative

A version takes part through each curriculum-period membership whose academic year is at or before the current academic year. A version listed only in years after the current academic year has no eligible year, and a course whose every version is listed only after the current year has no representative.

Each version is reduced to its greatest eligible year. The candidates are then ordered in three steps.

1. A version whose greatest eligible year is the current academic year.
2. Otherwise a version whose greatest eligible year lies in the activity window, the greatest such year first.
3. Otherwise a version whose greatest eligible year is the greatest earlier year.

Two candidates that agree on the step and on the year are separated by the greater source modification ordinal, and a version with no recorded ordinal ranks below a version that has one. When the ordinals also agree, the lexicographically greatest exact source version identity is chosen.

When a course has no version with an eligible dated membership, the selection considers only its versions with no period membership at all. Those versions are ordered by the greatest source modification ordinal, with a missing ordinal below every present ordinal, and then by the lexicographically greatest exact source version identity. A version whose memberships are all after the current academic year is ineligible and never enters this fallback. The fallback keeps a course readable through its retained membership-free versions.

The selection reads the academic year and the window stored with each course, so a course's representative is chosen under the same values that decide its active state.

![Every version of a course is checked for a listing at or before the current academic year, and a version whose listings all lie after the current year takes part in neither ranking. When a version has such a listing, the candidates keep each version's greatest eligible year and are ordered by the current academic year, then the greatest year inside the activity window, then the greatest earlier year, with the final box breaking ties by the greater source modification ordinal and then the greatest exact source version identity. When no version has such a listing, the versions with no membership are ranked by ordinal and then by source version identity.](/docs/diagrams/representative-selection.svg)

## Activity and the window

An active course has at least one version listed in a curriculum period whose academic year lies inside the inclusive activity window. The window runs from the current academic year minus the window length plus one, up to and including the current academic year. A course with no version in the window is an inactive course.

`CURRENT_ACADEMIC_YEAR` sets the current academic year and `ACTIVE_ACADEMIC_YEAR_WINDOW` sets the window length, both in `config/curriculum.toml`. The example sets the window to three academic years, so a version listed in the current academic year or in either of the two before it keeps its course active.

The active state is stored with the canonical course record, together with the first and the last academic year in which any version of the course is listed, and with the year and window that were in force when that record was last written. A catalogue read takes the stored state as it stands and never recounts it.

[Normalization](/docs/backend/catalogue/ingestion.md) recomputes the active state and the listing years for every stored course after each completed import, and an activity policy save recomputes them for every course of every present role dataset. Both run from the period memberships of the versions stored for that course. Normalization and the reads apply one shared window bound, so a course is active or inactive by the same rule in every place that decides it.

## Modules for an academic year

A module read takes an academic year and selects one module version from that module's versions. A version takes part when it is a member of that exact academic year's curriculum periods.

When a module has no version with that exact membership, a reader also accepts a version with no period membership whose own validity bounds contain the requested year. A validity bound the version does not state leaves that side open, so a version with neither bound is a candidate for every academic year. An empty candidate set means the module has no version for that year, and more than one candidate is ambiguous. The structure of studies projects a root only from exactly one candidate or one explicit version selection, and a batch read reports a root with several candidates and no selection as `version_selection_required` instead of guessing. A module nested inside a projected tree with several candidates and no selection is reported as an unresolved node.

Module [narrowing](/docs/reference/glossary.md#courses-and-the-catalogue) filters the minimal official catalogue by module name and code in the browser. [Search](/docs/backend/search-and-discovery/search.md#the-minimal-official-catalogue) states which candidate a matched group displays, the order of the rows, and that the row carries every eligible candidate.

The [Prerequisite graph](/docs/backend/prerequisites/prerequisite-graph.md) is the one reader that also accepts a single nearest dated version when the strict candidate set is empty, and the graph discloses that fallback.

## Courses without a representative

These cases decide what the catalogue holds and what a read returns.

| Case | Outcome |
|---|---|
| Every version is listed only after the current academic year | The course has no representative and is inactive. A read that returns whole courses returns it with no representative attached, and a read that returns one summary per represented course omits it. A request for the summary of the course fails loudly. |
| No version has an eligible dated membership, and at least one version has no period membership | The greatest such version by ordinal and then by source version identity is the representative. The course has a readable summary and retained results. |
| The official code is null | Normalization leaves the version out of the catalogue with its teaching text, its curriculum-period memberships, and its options, and reports it as excluded. Its stored raw response is kept. Its database source snapshot is removed with the other unreferenced completed-run snapshots. The other versions of the course are stored as usual, and the course record is dropped only when no coded version is stored. |
| The official code is missing or blank | Normalization fails. |
| No stated credit maximum | The version keeps no maximum, and a summary of the version reports the minimum credit value only. |

The reads themselves are described in [Curriculum reads](/docs/backend/catalogue/curriculum-reads.md), and the stored course and version records are described in [Data model](/docs/backend/catalogue/data-model.md).
