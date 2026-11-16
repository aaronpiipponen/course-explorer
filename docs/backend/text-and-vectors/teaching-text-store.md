# Teaching text store

The store holds the official course and module text cells and the derived translations of the Working dataset.

## The derived table

`DerivedCourseText` in `src/course_explorer/db/models.py` maps the table `derived_course_texts`. One row is one derived translation of one field and target language for one exact course version, and `course_version_id` references `course_versions.id` with `CASCADE`.

The table refuses a duplicate `(course_version_id, field, language)` through `uq_derived_course_texts_version_field_language`. Its checks are `_not_blank("text")` and `_not_blank("source_text_hash")`, `source_and_target_languages_differ`, `language_canonical` and `source_language_canonical` for canonical lower-case language codes, `source_text_hash_sha256` for a lowercase 64-character hash, and `method_model_consistent`, which allows a `manual` row with no `translation_model` and a `model` row with a non-blank one. The column list, the enum types, and the migration that creates the table are in [Tables and migrations](/docs/backend/catalogue/tables-and-migrations.md).

The table is written only through the seam in [The sole write path](#the-sole-write-path). No code path writes these rows into the official text cells, and no read merges official and derived text into one field.

## The official text tables

`OfficialCourseText` and `OfficialModuleText` in `src/course_explorer/db/models.py` map `official_course_texts` and `official_module_texts`. One course row is one official cell of one field and official language for one exact course version, keyed by `(course_version_id, field, language)`, and `field` is one `OfficialTextField` value. One module row is one official name cell of one exact module version, keyed by `(module_version_id, field, language)`, and its `field` check admits the value `name` alone. Both tables refuse a language outside the canonical lower-case code form, and both references cascade from their version.

`_insert_official_texts` in `src/course_explorer/ingestion/normalization_repository.py` is the only product writer of either table. It writes the five course fields as one cell per field and kept language, and one name cell per kept language for a module version. [The normalization transaction](/docs/backend/catalogue/capture-and-normalization.md#the-normalization-transaction) states the language filter and the ordered duplicate rule the writer applies.

The cells belong to the write of one exact version. A version the batch adds or classifies `changed` receives its cells in the same transaction, after `_delete_version_payload` removes the cells of a changed version. A hash-equal retained version writes no cell, so `_insert_official_texts` never runs for it.

No translation path writes either table: `_translation_values` carries derived cells alone, as [The sole write path](#the-sole-write-path) states. Resolution reads these cells first and treats an official cell as effective text whenever it is nonblank.

## The translation DTOs and the file loader

`DerivedTranslationInput` and `DerivedTranslation` in `src/course_explorer/teaching_text/service.py` are frozen, closed models on `_DerivedTranslationModel`. A blank `source_version_id`, `text`, or `source_text` is refused, `generated_at` is required to be timezone-aware through `require_aware`, equal source and target languages are refused, `_method_model_consistent` applies the method and model rule of the table, and the persisted `source_text_hash` goes through `require_sha256`. The input DTO carries the exact `source_text`, and the persisted DTO carries the hash of that text instead.

`teaching_text_sha256` hashes the exact UTF-8 bytes of a non-blank string, so a trailing space changes the hash.

`load_translation_file` in `src/course_explorer/teaching_text/commands.py` reads one UTF-8 file, parses it with `json.loads`, refuses anything but a top-level array with at least one entry, and validates the array through `TypeAdapter(tuple[DerivedTranslationInput, ...])`. Malformed JSON, a non-array, an empty array, an extra field, and a malformed entry each raise `ValueError`, and the loader returns nothing partial, so a caller that writes only after a successful load never writes part of a file.

## Resolution and the coverage accumulator

`official_teaching_text` returns the non-blank official value of one field and language from a `CatalogueCourseVersion` or a `CatalogueRepresentativeTeachingText`. It reads the requested language from the version's keyed official cells and no other field, so the prerequisite text and the additional information of a version never enter the resolution. An absent field, absent language or blank value returns `None`, and the read never falls back to another official language.

`_resolve_cell` classifies one cell. It returns the resolved `EffectiveTeachingText` or `None`, one classification of `official`, `derived`, `stale`, or `missing`, and the current source hash of a stale cell. Official text wins. An official cell sets `text_hash` and `source_text_hash` to the hash of that same text, and a derived cell carries the recorded `source_text_hash` together with the method, model identity, and generation time of the row. A stored row is classified `missing` when its target is unmapped or its source is not listed for that target, so policy exclusion leaves saved bytes without effective text or a stale report.

`_resolve_representative_cells` walks `TeachingTextField` and the configured official-language order, increments `counts[(field, language)][classification]`, appends one `StaleDerivedText` per stale cell with its `stored_source_text_hash` and `current_source_text_hash`, and returns the cells in that order.

`_resolve_teaching_inputs` is the single accumulator shared by the effective catalogue and the coverage read. It indexes the derived rows through `index_derived_translations`, which refuses two rows for one identity, counts one cell per field and configured language, sorts `stale_texts` by group id, source id, field, and language, and lists the courses with no representative. It also records two course counts of the report: `name_only_course_count` for every Name-only course and `unembeddable_course_count` for every Unembeddable course.

For one cell, `official_count + derived_count + stale_count + missing_count` equals `representative_course_count`, because every represented course classifies each of its configured cells exactly once.

`TeachingTextService.build_catalogue` resolves every representative and attaches `representative_source_hash` and `is_active` from the read rows. `TeachingTextService.coverage` reads `CurriculumRepository.list_teaching_text_inputs` and the same accumulator, so it returns the identical `TeachingTextCoverageReport` without materializing a `CatalogueCourse`, and `tests/teaching_text/test_teaching_text.py` and `tests/teaching_text/test_teaching_text_repository.py` prove the two reads agree.

The report and carrier DTOs are `EffectiveTeachingText`, `EffectiveCourseTeachingText`, `TeachingTextCoverageCount`, `TeachingTextCoverageReport`, and `EffectiveTeachingTextCatalogue`. `DerivedTranslationRead` in the same module is the read protocol the resolver consumes.

## The sole write path

`DerivedTranslationRepository.upsert` in `src/course_explorer/teaching_text/repository.py` is the only writer of the table, and `list_for_course_versions` is its only reader. The stage that produces these entries is described in [Translation stage](/docs/backend/text-and-vectors/translation-stage.md).

`_prepare_inputs` validates the whole batch before any write. It refuses two entries for one identity, an unconfigured target or source language, a target that is not mapped, a source that is not listed for that target, refuses a version that `CurriculumRepository.get_representative_teaching_texts` did not return as a selected representative, refuses an entry whose target cell holds official text, refuses an entry whose source cell holds no official text in the stated source language, and refuses an entry whose `source_text` differs from that official text. It returns each prepared entry with the `teaching_text_sha256` of its source text, computed by the store rather than supplied by the caller, so one invalid entry leaves no row behind.

The prepared batch then runs in one transaction. A version id that is not stored is refused, the stored rows of the batch's version ids are loaded, and each `_translation_values` mapping is compared field by field. An absent row is inserted, an identical row is counted in `unchanged_count`, and a differing row is updated in place. The outcome is returned as `TranslationImportReport` with `requested_count`, `inserted_count`, `updated_count`, and `unchanged_count`. An empty sequence returns a zero report without touching the database.

The official course cells and the official module name cells are never part of `_translation_values`.

## Commands and tests

`scripts/admin.py coverage teaching-text --role working` prints the `TeachingTextCoverageReport` of the named role through `run_administrative_coverage`. The operator menu offers the same report for a chosen dataset role.

`tests/teaching_text/test_teaching_text.py` covers the resolution, the duplicate-key refusal, and the projected coverage read. `tests/teaching_text/test_teaching_text_command.py` covers the file loader. `tests/teaching_text/test_teaching_text_repository.py` is the opt-in PostgreSQL proof of the write path, its refusals, and the staleness report.
