# Curriculum reads

The read repository serves detached course, module, and period objects to every curriculum feature, and one shared selector chooses each course's representative version.

## The repository and its shared loaders

`src/course_explorer/curriculum.py` holds the frozen, closed read DTOs and the reader protocols. `src/course_explorer/curriculum_repository.py` holds `CurriculumRepository`, which implements `CurriculumRead`, `CurriculumStructureRead`, and `CurriculumDiscoveryRead` over a `sessionmaker[Session]`. Each public read opens its own session, so every returned DTO is detached from the ORM.

`CurriculumRepository._scoped_courses` builds the one activity and identity scope. It selects whole `Course` rows, or the four activity/identity columns when `teaching_only` is set, orders by `Course.group_id`, applies `Course.is_active == active_only` when `active_only` is not `None`, and applies a `group_ids` membership filter when one is given. The remaining loaders build on it.

| Loader | Builds | Used by |
|---|---|---|
| `_load_courses` | `CatalogueCourse` through `_assemble_full_courses`, or `CatalogueTeachingCourse` through `_representative_teaching_texts(include_identity=True)` with the requested field set | `list_courses`, `list_teaching_courses`, `list_prompt_courses`, `get_courses` |
| `_load_course_summaries` | `RepresentedCourseSummary` or the shared `CourseSummary` | `list_represented_summaries`, `get_represented_summaries`, `get_course_summaries` |
| `_load_modules` | `CatalogueModule`, or `CatalogueModuleLookup` when `lookup_only` is set, with each version's official name cells | `list_modules`, `list_module_lookup`, `get_modules` |
| `_representative_teaching_texts` | `CatalogueRepresentativeTeachingText`, or `CatalogueTeachingCourseVersion` when `include_identity` is set, carrying the requested official fields: name, content and outcomes for a teaching read, all five for a prompt read | `list_teaching_courses`, `get_representative_teaching_texts`, `list_teaching_text_inputs`, `list_prompt_courses` |
| `_module_periods_by_version` | the `CatalogueCurriculumPeriod` tuple of every module version | both module loaders |

The scoped getters (`get_courses`, `get_modules`, `get_course_summaries`, `get_represented_summaries`, `get_course_activities`, `get_representative_teaching_texts`) share those loaders and differ only by identity scope and projection. Each returns `()` before any I/O for an empty scope, and each omits a requested identity that the database does not hold.

`list_prompt_courses` is the inference prompt projection. It carries every selected representative's five official text fields and no prerequisite, substitution, credit, period or URL structure, and the inference operational read consumes it. The teaching-only reads request only the name, content and outcomes cells.

`_require_representative_versions` is the one guard that turns a selected representative with no persisted version row into a `ValueError`, and every read that loads representative data calls it.

## The representative selector

`select_representative_source_ids(session, group_ids, *, current_academic_year_start, activity_window_years)` returns one exact source version identity or `None` per requested canonical group. One statement joins `Course` to `CourseVersion` and outer-joins the curriculum-period memberships and their periods, grouping by `(Course.group_id, CourseVersion.source_version_id, CourseVersion.source_modification_ordinal)`. It selects the greatest eligible period year and the count of distinct curriculum-period memberships for each version.

Python ranks each version with an eligible year as `(2, year)` for the current academic year, `(1, year)` inside `is_within_activity_window`, and `(0, year)` otherwise, builds the candidate key `(rank, -1 if ordinal is None else ordinal, source_id)`, and retains the greatest key per group.

When a group has no version with an eligible dated membership, it ranks only the versions whose membership count is zero by `(ordinal, source_id)` and retains the greatest. A version whose memberships are all after the current academic year is ineligible.

`CurriculumRepository._representative_source_ids` groups the scoped courses by their stored `(activity_as_of_academic_year_start, activity_window_years)` pair and calls the selector once per distinct pair, so one read can rank different courses under different recorded windows.

The selection rules the operator relies on are described in [Representative versions and activity](/docs/backend/catalogue/representative-versions.md). A change to the preference belongs in `select_representative_source_ids`, a change to the strict and fallback module candidate rules belongs in `module_version_candidates` and `_nearest_module_version`, and a change to the shared summary mapping belongs in `course_summary_from_fields`.

## The read protocols and their consumers

| Protocol | Methods | Consumers |
|---|---|---|
| `CurriculumRead` | `list_courses`, `list_teaching_courses`, `list_prompt_courses`, `list_teaching_text_inputs`, `list_modules`, `list_module_lookup`, `list_represented_summaries`, `get_represented_summaries`, `list_course_code_identities`, `summary` | `CourseResolver` (`search/resolver.py`), `run_catalogue_command` (`search/lookup_commands.py`), `TeachingTextService` (`teaching_text/service.py`), the translation selection (`translation/service.py`), the inference operational read (`inference/operational.py`), which reads `list_prompt_courses` |
| `CurriculumGraphRead` | `get_courses`, `get_modules` | `compose_proposal_from_work` (`inference/proposal_commands.py`) calls `get_courses` for the target and the candidate identities one accepted proposal names. `get_modules` has no consumer through this protocol. The graph facade `PrerequisiteGraphCompiler` holds the per-worker native fact handle, and the native compiler reads the canonical facts through it. The test support `tests/prerequisite/_graph_support.py` implements the protocol over supplied DTOs |
| `CurriculumStructureRead` | `get_modules`, `get_course_summaries` | `StudyStructureService` (`study_structure/service.py`), which prefetches every module and course card reachable from the requested roots in batches |
| `CurriculumDiscoveryRead` | `get_substitution_source_group_ids`, `get_prerequisite_candidate_group_ids` | the next-course command (`next_courses/commands.py`), which then compiles the candidates through the same graph compiler |

`CurriculumDiscoveryRead` extends `CurriculumGraphRead` because discovery compiles graphs over the identities it finds. Both discovery methods return sorted distinct canonical group identities, never payloads, and both are candidate supersets across the stored versions, so the current compiler alone decides real eligibility.

The remaining repository methods are used directly where a consumer needs one narrow projection. `get_course_activities` returns the four activity columns for inference publication and reconciliation, and `summary` returns the `CatalogueSummary`, the [Catalogue summary](/docs/reference/glossary.md#administration) counts, for the operator menu and the dataset verification.

## Period objects and module version selection

`CatalogueCurriculumPeriod` carries the official period identity plus the explicit `academic_year_start`, and `CatalogueModuleVersionIdentity.curriculum_periods` exposes the tuple of those objects per module version. Both module loaders build them in `_module_periods_by_version`, ordered by academic year and then by period identity, so no caller parses a period identity to learn its year.

`module_version_candidates(versions, *, academic_year_start, allow_nearest=False)` returns the versions usable for one academic year and preserves the input order. It returns every version with an exact membership in that academic year. Only when none exists does it consider the membership-free versions whose own `validity_start` and `validity_end` bounds contain the requested year, and a bound that is `None` leaves that side open, so a version with neither bound is a candidate for every academic year. An empty result is a missing version and more than one result is ambiguous, so this helper selects a candidate set and never an arbitrary winner.

Callers that can disclose the fallback opt into `allow_nearest`, which then returns the single nearest dated version through `_nearest_module_version`, choosing the greatest preceding year and otherwise the least following year, and breaking a same-year tie by the greatest `source_version_id`. `_fallback_year` supplies each version's comparison year from its best preceding period year, otherwise its nearest following period year, otherwise a validity bound.

| Caller | Call |
|---|---|
| `StudyStructureService._select_version` | strict call, then `no_current_version` or `ambiguous_version` when the set is empty or holds more than one version. An explicit selection outside that set raises `ValueError` and aborts the whole batch |
| `run_catalogue_command` | strict call, omitting a module with no candidate and keeping every eligible candidate of a group with its exact identity, validity bounds and period memberships for the browser's local version selector |
| The native prerequisite compiler | No call. One native selection per module chooses an exact period membership first, then a validity-bound match, then the nearest dated version, and records a `module_version_fallback` ignored part when it falls back |

## The shared course summary

`CourseSummary` is the one official summary every course-returning read reuses, carrying `group_id`, the displayed summary name of each interface language, the representative's official code, the nullable credit range, the active flag, and the official URL.

The two displayed summary names are projections: each interface language takes its first nonblank exact official value in the university map's fallback order, and neither name is written back into a missing official-language cell.

A null minimum means the source publishes no minimum credit and is displayed as unknown, never as an invented value.

`course_summary(course, *, display_fallback)` maps it from a full `CatalogueCourse` and raises `ValueError` when the course has no representative. `course_summary_from_fields` is the one mapping used by `_load_course_summaries`, so the projected read and the full read produce the identical DTO. `RepresentedCourseSummary` pairs the summary with the representative's `source_version_id` and `source_hash` for consumers that must match durable semantic scores to the current representative.

The repository holds the configured university's display policy once at construction. A read therefore resolves the two summary names against that policy, and a caller may supply the same map entry explicitly for a test or a scripted proof. The module narrowing names are resolved by `search/lookup_commands.py`, and the structure module names by `study_structure/service.py`, from the university passed to each.

The full-catalogue command and read times of that projection are recorded in [Full-catalogue read projection](/docs/experiments/runtime-storage-and-serving/full-catalogue-read-projection.md).

The prerequisite compiler does not call `course_summary`. The native compiler selects the representative version and reads its course summary through the fact bundle, and the frozen DTOs carry those values without mapping a `CatalogueCourse`. `StudyStructureService` and the administrative reads use the scoped `get_course_summaries` projection. The catalogue command maps only the narrowing fields from `list_represented_summaries` and `list_module_lookup`, so its report carries no official URL, source hash, rule tree or teaching text. Its module candidates copy the already-projected version identity, validity bounds and period memberships, because the browser must distinguish several eligible versions of one group.

`src/course_explorer/search/projection.py` builds `LookupProjection` from one `list_represented_summaries(active_only=None)` read and one `list_module_lookup` read. The projection retains each official summary with the normalized code and name keys the lexical matchers compare, the `RepresentedCourseSummary` records the semantic method pairs its hits with, the module identities, an index of represented records by canonical group and current representative code, and one fuzzy-name token table per display language.

`CourseLookupService` answers the represented index and the three lexical methods from that required projection, whose rows `exact_code_search` and `substring_search` read and whose per-language name lists and fuzzy-name token tables `fuzzy_name_search` reads. `UnifiedCourseSearchService` takes only the represented records from it, for the semantic method's identity pairing. `search/resolver.py` reads the projection's `by_group_id` and `by_code` identity maps, and `run_catalogue_command` maps the same rows and module identities into the narrowing report.

The [course resolver](/docs/reference/glossary.md#search-and-comparison) takes an optional projection, and without one it uses its scoped reads and the whole-catalogue code projection.

## Tests

- `tests/db/test_curriculum_repository.py` proves the read seam against a normalized database. It asserts the selected representatives and their teaching text cells, the prompt projection's full official cell set and representative identities, the membership-free fallback and its native parity, the activity scopes, the agreement between every projected read and the corresponding full read, the SQL statements a projected read issues, the scoped getters, the omission of unknown and unrepresented identities, the module rule trees, the Swedish-first display fallback across the full, projected, catalogue, study and native reads, and the `CatalogueSummary` counts against direct SQL aggregates.
- `tests/ingestion/test_normalization_repository.py` and `tests/ingestion/test_normalization_recovery.py` cover the selector through normalization, the tie-break fixture where two versions share a year and an absent ordinal, and the representative reselection after a changed version.
- `tests/search/test_search_service.py`, `tests/search/test_course_lookup.py`, `tests/study_structure/test_study_structure.py` and the prerequisite tests cover the consumers over the same DTOs.
