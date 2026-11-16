# Ingestion

Ingestion captures the public curriculum source and converts one completed capture into the official catalogue of the Working dataset.

## What ingestion does

Ingestion runs as one stage with two steps. The [Capture](/docs/reference/glossary.md#capture-and-data-preparation) reads the anonymous public curriculum source and keeps exactly what it received. [Normalization](/docs/reference/glossary.md#capture-and-data-preparation) then validates the completed capture and converts it into official courses, modules, their versions, and their rules. The result is the official catalogue of the Working dataset: canonical courses and modules, exact versions, curriculum periods, official text cells, prerequisite options, substitution options, and module rule trees.

The capture runs on one shared spine: the lifecycle stage, raw checkpoints and resume, one neutral normalized course and module record, one database writer, change detection, representative selection and activity. The configured university's map entry supplies the source family, and that family supplies discovery, fetching and a parser to the shared spine. [Source families](/docs/backend/catalogue/source-families.md) describes each implemented family, its discovery walk, its fetch variant and the university table facts it reads. One instance reads one university's source.

The source family decides which routes the capture reads, which responses and pages it fetches, and how a failed read ends the run.

The source base is the canonical public HTTPS base with no credentials in it, a read outside the source's public query paths is refused, and a redirect is not followed. An error response is retried inside the configured attempt budget, and only a 429 or 5xx status is retried. A successful response whose body is not the structured content the source contract describes ends the run instead of being retried, because the same request returns the same body again.

Requests are spaced. `KORI_REQUEST_INTERVAL_SECONDS` sets the minimum number of seconds between source request starts across every concurrent request, and a retry of a failed request is spaced in the same way. The request timeout, attempt budget, retry backoff and request interval are the shared request policy of every source family.

## Captures are immutable and verified

Each capture writes one manifest and one stored response for every response it received. A stored response holds the exact bytes of one source response, is named by the request it answered and by the hash of its body, and is never overwritten, so the same response always lands in the same file with the same bytes. The manifest records every response it holds, with the stored name, the body hash, the body size, and one entry per source object carrying the source identity, the group identity, the position inside the response, and the content hash. A Source snapshot is one such object entry tied to the capture that produced it.

A capture commits its responses in bounded batches of `CAPTURE_CHECKPOINT_RESPONSE_COUNT` responses and publishes each batch together with the manifest update that records it. An interrupted capture therefore holds every committed batch and no partial one.

Verification decides whether the stored evidence matches the manifest. It re-reads every stored response, checks its bytes, its size, and its body hash against the manifest, checks the object positions and content hashes inside it against the manifest entries, and refuses a file in the capture directory that is not a stored response or that a completed manifest does not name. A capture reopened after a failure re-checks its retained responses from the beginning before it does any other work, and a completed capture refuses any change to its evidence.

A stopped or failed capture continues from its own evidence. The continuation re-verifies the retained responses and re-walks its family's discovery. A request the continuation repeats reaches the source again, and a body whose bytes match the stored one is adopted into that stored response rather than recorded a second time.

Each family continuation resumes its own walk:

- A Sisu continuation re-reads the period and version-reference queries, then fetches only the exact course and module versions the manifest does not represent.
- A Peppi legacy continuation repeats its navigation, organisation, education and programme reads, and a Peppi PSG continuation repeats its discovery walk and every plan read.
- Both Peppi continuations skip the course details the manifest already holds.
- A guide continuation re-reads its recorded root, programme and structure responses and fetches only the pages it lacks.

A source edited between pause and resume fails at the checkpoint instead of continuing with mixed evidence. A capture that finished its downloads continues into normalization without reading the source again. The [Curriculum capture and replay](/docs/experiments/runtime-storage-and-serving/curriculum-capture-and-replay.md) report records the scale of a full-catalogue capture and two source-free replays that reproduced the same normalized course versions and table counts.

![The public curriculum source is read by a capture that writes the stored responses with the manifest. The capture and the verification step are marked as resume points with a dashed outline, because a stopped run continues from a committed checkpoint. Verification checks every stored response, and normalization then converts the whole capture into the official catalogue of the Working dataset in one transaction.](/docs/diagrams/ingestion-pipeline.svg)

## Pause, stop, and the normalization transaction

Pause and stop act only at the durable boundaries of the run: after a committed batch of responses and before the next source request, between two stored responses during verification, and between a completed capture and normalization. A run that pauses or stops at one of those boundaries keeps the manifest and the stored responses it has and is not marked failed, so the next run continues rather than starts over.

Normalization is one transaction. It reads the completed capture, validates it, and writes the whole batch, and no control request interrupts it. The run report reconciles the captured object counts with the versions the batch wrote plus the versions it left out, so a batch that lost an object fails instead of publishing a partial catalogue.

Normalization drops a membership that names a period the captured period response does not publish. The version is kept, and the normalization report counts the dropped memberships and the ingestion status reports that count.

> [!NOTE]
> A normalization that fails writes no row and keeps the capture it read, so the same verified capture can be normalized again without reading the source.

When a stopped stage returns to idle, its cleanup removes the capture runs it does not retain under the raw data root and keeps the run the stage continues from.

The stage runs through the administration command. A start needs a Working dataset whose database is at the single migration head, and `INSTANCE_KEY` naming one implemented university whose map entry supplies its source block.

```bash
uv run python scripts/admin.py ingestion start
uv run python scripts/admin.py ingestion status
```

The start action begins the stage and the status action reports its lifecycle state. The steps the operator runs are described in [Stage lifecycle](/docs/operations/stage-lifecycle.md) and the [Operator menu](/docs/operations/operator-menu.md).

## What a refresh changes

A refresh reads the source again and may receive responses that an earlier capture already holds. The capture deletes nothing and overwrites nothing, and a stored response keeps its exact bytes.

Normalization then compares the content hash of each captured object with the hash of the version already stored under that exact `source_version_id`. A Sisu record hashes its raw object, so request-time values never enter the identity. A Peppi legacy record hashes its extracted course or module record, so its request-time values never enter the identity either. An identical version writes no official text cell, prerequisite, substitution or module rule payload, so the derived work made from it is reusable and a retained cell that the write policy would suppress is left as stored. A changed version retains its exact source identity and its record, and its official cells and dependent rows are written from the capture. A version the catalogue did not hold is added.

An added or changed version stores official text only in the configured university's declared official languages. The languages are ordered, and when two configured languages carry the same value on one field, the language earlier in that order keeps the exact value and the other stores none. A supplied language outside the configured list is not stored. A hash-equal retained version is never rewritten by this rule.

A completed import replaces the stored listing of every curriculum period it covered with exactly the membership set the source lists for it, for courses and modules alike. A stored membership in a covered period that the source does not list is removed. A membership in a period outside the coverage forms the stored history, and an incoming membership outside the coverage is added. Courses, modules and versions are never deleted for being absent.

Activity is recalculated for every stored course after each completed import, not only the courses in the batch. A version that moved outside the activity window makes its course inactive, and a course that becomes active from inactive has its inference processing marker reset unless an operator disposition is set. The representative version is chosen from the stored versions under the same year and window.

A course version whose official code is null is dropped from the catalogue together with its official text cells, its curriculum-period memberships, and its options, and a course left with no coded version is dropped with it. Its stored raw response is kept on disk, and normalization reports the exclusion. A completed import removes every database source snapshot that no stored version references, then removes a completed run that owns no snapshot. A running or failed run, a referenced snapshot and retained raw evidence are never pruned. A source that publishes no course minimum credit or no document state stores none, and a course with no official minimum credit is displayed as unknown and counted at the configured assumed value in every credit calculation.

Downstream work is invalidated by changed content. A Translation records the hash of the official text it was made from, so a changed official text leaves the stored translation in place and reports it as a stale translation, which is never used. An embedding chunk records the representative version's source hash and the text it was made from, so the next embedding run rewrites the chunks whose inputs changed and removes the chunks of a version that is not the representative. [Translation](/docs/backend/text-and-vectors/translation.md) and [Embeddings](/docs/backend/text-and-vectors/embeddings.md) describe those two stages.

## The catalogue summary

A Catalogue summary reports the counts of one dataset: curriculum periods, canonical courses, active courses, course versions, canonical modules, and module versions. The counts are read from the normalized tables of the reported role, so they describe what the last successful normalization published.

The operator menu reports the catalogue summary of the named role, and a dataset verification records the same counts with the teaching-text, embedding, and inference coverage of that dataset. [Operator menu](/docs/operations/operator-menu.md) describes the report, and [Data model](/docs/backend/catalogue/data-model.md) describes the tables behind it.

## Configuration keys

`config/curriculum.toml` owns the capture and normalization values. The source base, the university root identity, the first academic year and the course-guide base come from the configured instance's map entry. The keys an operator sets are:

| Key | Meaning |
|---|---|
| `RAW_DATA_ROOT` | The repository-relative directory that holds the capture runs |
| `CAPTURE_CHECKPOINT_RESPONSE_COUNT` | How many responses one committed checkpoint covers |
| `CURRENT_ACADEMIC_YEAR` | The current academic year, which sets the activity window and the representative preference |
| `ACTIVE_ACADEMIC_YEAR_WINDOW` | The number of academic years inside which a listed version keeps a course active |
| `KORI_SEARCH_PAGE_SIZE` | How many search results one page holds |
| `KORI_BATCH_SIZE` | How many exact versions one request asks for |
| `KORI_REQUEST_TIMEOUT_SECONDS` | How long one source request may take |
| `KORI_MAX_ATTEMPTS` | How many attempts one source request gets, the first attempt included |
| `KORI_RETRY_BACKOFF_SECONDS` | The delay before the first retry, doubled for each further retry |
| `KORI_REQUEST_INTERVAL_SECONDS` | The minimum seconds between source request starts across every concurrent request |
| `KORI_CONCURRENCY` | How many Sisu source requests run at once |
| `KORI_MAX_PARTITION_DEPTH` | The greatest partition-prefix length a truncated search query may reach before the capture fails |
| `INGESTION_CONTROLLER_STATE_ROOT` | The repository-relative directory of the ingestion stage state |

The Sisu request keys `KORI_SEARCH_PAGE_SIZE`, `KORI_BATCH_SIZE` and `KORI_MAX_PARTITION_DEPTH` shape its discovery and fetch requests, and `KORI_CONCURRENCY` bounds how many of its requests run at once, so the four apply to no other family. The request timeout, attempt budget, retry backoff and request interval change the timing of the reads without changing the captured work.

The example sets a checkpoint of 100 responses, a search page of 1000 results, an exact batch of 100 versions, three attempts, four concurrent requests and a request interval of 0.4 seconds.

The capture and normalization internals are described in [Capture and normalization](/docs/backend/catalogue/capture-and-normalization.md), and the reads that consume the catalogue are described in [Representative versions and activity](/docs/backend/catalogue/representative-versions.md).
