# Source families

The configured university's source family decides how the capture discovers and fetches its curriculum data.

An instance presents one university through the instance key, and that university's entry in the [university table](/docs/reference/glossary.md#configuration) names its source family and one source block. The table holds 36 entries. Of these, 35 name a real university: nine use the Sisu family, eight read the Peppi legacy public JSON API, three read the Peppi Solenovo PSG public JSON API and 15 read the server-rendered Peppi guide pages, six over plain HTTP and nine through a headless browser. The last entry names the fictional Example University the example configuration presents, and its Sisu block names a fictional source. The table refuses an entry whose family is not one of the four, and an entry that does not carry exactly one source block.

Every family reads one anonymous public source. The family supplies the discovery walk and the fetching into the shared checkpoints, and a parser converts the captured objects into the neutral record. The shared capture, its checkpoints and the request policy are described in [Ingestion](/docs/backend/catalogue/ingestion.md). The course and module identities each family's records carry, and the parser rules each source shape follows, are described in [Capture and normalization](/docs/backend/catalogue/capture-and-normalization.md).

## The Sisu family

The Sisu family reads the public Sisu Kori JSON API. Nine universities use it: Aalto, Helsinki, Jyväskylä, LUT, Hanken, Tampere, Arcada, LAB and Åland. The fictional Example University also uses the family with a fictional Kori base. The entry's source block supplies the source base, the university root identity, the first academic year and the course-guide base.

The capture selects every published curriculum period whose start year lies between the mapped first academic year and `CURRENT_ACADEMIC_YEAR`, and it returns those periods in year order. A source that publishes a gap between periods is read as it is, because the selection keeps the periods the source lists. A response that yields no selected period fails, because it is not a completed catalogue.

Discovery reads the selected periods, then the course version references and the module version references of each period. A query the source reports as truncated is partitioned over the code prefix for a course query, or over the full-text prefix for a module query, until every partition is complete, and a truncated partition at the configured `KORI_MAX_PARTITION_DEPTH` fails the capture. Every page is read to the total the source reports. The exact course and module versions that no reference represents are fetched in batches of `KORI_BATCH_SIZE` versions, with at most `KORI_CONCURRENCY` requests open at once. The typed references of every fetched object then close the walk: each course or module group is fetched once, and a group that returns no object stays as an unresolved reference of its own type.

A search result set that cannot be enumerated completely is refused rather than accepted in part, so the capture never records a partial catalogue. A successful response whose body does not match the Sisu contract ends the run, as does a search response that carries an `ERROR` notification, a truncated response that carries items or a non-positive total, two periods that share one academic year, a repeated id inside one query, and an exact batch whose ids differ from the request.

## The Peppi legacy family

The Peppi legacy family reads the Peppi legacy public JSON API. Eight universities use it: Turku, Eastern Finland, Oulu, Vaasa, Vaasa UAS, Åbo Akademi, Lapland and the University of the Arts Helsinki. The entry's source block supplies the API origin, the guide origin, the discovery variant, the published curriculum periods and the course-page templates.

Their map entries list the published curriculum periods in order, and the capture reads every listed period. Every period-dependent request carries its period, and the navigation and education-type reads that do not depend on a period happen once.

Discovery follows the mapped variant.

| Variant | What it reads |
|---|---|
| `organisation_navigation` | The navigation entries, every top-level organisation and its nested units, and the education route of each organisation and navigation entry |
| `navigation_education_type` | The education route of each navigation entry |
| `organisation_education_type` | The mapped organisation-unit navigation entries, or the top-level organisations when the navigation lists none, then each organisation's education types and their education routes |

Every distinct programme is fetched once in each listed period, and its tree supplies the programme's modules and the course references inside them. Each distinct course detail is fetched once per period and code in sorted order. The course-page templates hold the course page of each official language.

A discovery route that answers HTTP 404 is an absent combination and stores no evidence. A course detail that answers HTTP 404 for one listed period stores no version for that period. A plan code the source omits in every listed period is kept once as an unresolved course reference, and the run completes. A course whose official code differs from its plan code is not reported unresolved. A plan response that holds more than one tree fails the run, as does one course code that carries two path ids in one period. A 404 on a programme tree and every other source failure fail the run.

## The Peppi PSG family

The Peppi PSG family reads the Peppi Solenovo PSG public JSON API. Three universities use it: Centria, HAMK and SAMK. The entry's source block supplies the API origin, the guide origin, the published academic years and the course-page template.

Discovery runs once for every listed year, and it is one fixed chain:

1. The published years.
2. The education types of each listed year.
3. Each education type's degree programmes.
4. One group read for every education-type and degree-programme pair.
5. Each group's accomplishment plans.
6. Every plan's learning units, then each distinct course detail.

A degree programme that appears under several education types is read once for each pair, because each listing can carry its own groups and plans. Plans are deduplicated by plan id, and a plan listed under several years supplies one version per listing year. Each plan's learning units become that plan's structure, and the plan entry supplies the course credit for its year when the detail carries none. A course with no detail credit and no plan credit stores an unknown credit.

Every PSG route is required, so a failed request fails the run. The captured years must carry every mapped year, a response outside the source contract fails the run, and one course code that carries two learning-unit ids in one plan fails the run. An empty plan publishes no structure and supplies no record. The source publishes no structured prerequisite, substitution or document state, so none is stored.

## The Peppi guide family

The Peppi guide family reads the server-rendered Peppi guide pages. Fifteen universities use it. Six read the guides over plain HTTP: Diak, Humak, JAMK, Kajaani University of Applied Sciences, Laurea and Novia. Nine read the same guides through one ephemeral headless Chromium context: Metropolia, Seinäjoki, TAMK, Lapland UAS, Oulu UAS, Haaga-Helia, Karelia, Savonia and Turku UAS. The entry's source block supplies the guide origin, the fetch variant and the published academic-year periods.

The walk starts at the guide root, keeps the programmes and programme categories it links, reads each programme page's year groups, and reads one structure page per group. The structure page supplies the programme root, its modules and its course rows, and a course row's page is fetched once because the course detail is period-independent. The root and programme pages are recorded as evidence-only responses, each structure page is recorded as a module response with its course rows, and each course page is recorded as a course response.

Their map entries list every published one-year academic period, and the capture reads each published year group, which must name a mapped period. A mapped period with no published year group is not covered by the run. A structure year outside the mapped periods fails the run, so a year outside the map is added by hand.

The fetch variant decides the transport.

| Variant | Transport |
|---|---|
| `plain_http` | One anonymous plain-HTTP client reads the guide origin and follows no redirect |
| `headless_browser` | One ephemeral Chromium context renders one page per request, keeps certificate verification on, follows no redirect and records the rendered DOM as the page response, and closes the page, the context, the browser and its driver on every exit |

The capture reads a page it already recorded from its stored response instead of requesting it again, and a stopped capture re-reads its recorded pages and fetches only the pages it lacks.

A course page that keeps answering HTTP 500 after the request attempt budget, or answers HTTP 200 with the guide's own rendered `500 Server Error` page, stores no version for that code and is recorded once as an unresolved course reference, and the reference closure resolves the code. Every other page failure fails the run. A guide root that links no programme page fails the run, as does one structure path published under two periods and one course code that carries two listing facts. The same parser reads the plain-HTTP body and the rendered page, so the guide root, the programme year groups, the course-page rule and the optionality vocabulary are the same for both variants.
