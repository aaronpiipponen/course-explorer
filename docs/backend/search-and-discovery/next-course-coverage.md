# Next-course coverage and eligibility

Next-course eligibility decides which courses a chosen basis prepares a student for, through the discovery reads and the direct compilation.

## Next-course composition

`run_next_courses_command` in `src/course_explorer/next_courses/commands.py` is the one composition path for a discovery request:

1. It normalizes `basis` through `PrerequisiteCalculationBasis` and `mode` through `NextCourseMode`, and checks `display_language` against `DisplayLanguage`. A blank identity, an unknown basis, an unknown mode, and an unsupported language each raise `ValueError` before any settings load.
2. It resolves the database settings from a supplied value or through `load_database_settings`, resolves the search settings from a supplied value or through `load_search_settings` once, and opens one `database_context`.
3. It resolves every selected and completed input through the [course resolver](/docs/reference/glossary.md#search-and-comparison), which matches an outer-trimmed exact canonical `group_id` first and an exact current official code second, then reduces each side to a sorted set of canonical identities.
4. When no selected input was supplied the completed set becomes the selected set. Otherwise a completed identity outside the selected set raises `ValueError` naming the offenders.
5. It builds `PrerequisiteGraphCompiler(native_handle=native_handle.worker_handle(database_settings))` over the process's one database fact handle, and opens the request's `CurriculumRepository`, `InferredPrerequisiteRepository` and `InferredEquivalenceRepository` as separate locals.
6. It forms the source identities, reads the candidate superset, and compiles it with `PrerequisiteGraphCompiler.compile_direct_many`.
7. It evaluates the first pass through `evaluate_next_course_outcomes`, and when `include_inferred` is set it recompiles the eligible candidates with `include_inferred=False` and passes both compilations to the result service.
8. It frees both request-local native bundles and returns `NextCoursesReport`.

The result models are frozen with `extra="forbid"`. `NextCourseMode` holds `ALL = "all"` and `AT_LEAST_ONE = "at_least_one"`. `NextCourseResult` holds `course`, `required_coverage`, `recommended_coverage`, and `depends_on_inferred`. `NextCoursesReport` holds `selected_group_ids`, `completed_group_ids`, `basis`, `mode`, `display_language`, `include_inferred`, `active_only`, `results`, and `uses_assumed_credits`.

`scripts/next_courses.py` is the `argparse` adapter. `--selected` and `--completed` repeat, `--basis` defaults to `completed`, `--mode` defaults to `all`, `--display-language` is required, `--include-inferred` is a flag, and the mutually exclusive `--active-only` and `--all-courses` set `active_only`. It prints `report.model_dump_json()` as one line. `POST /api/next-courses` applies the site's inference switch to `include_inferred` before it calls the command, so the flag reaches the command true only when the site serves the inference features and the request asks for it, and the route returns the report with the serving dataset identity.

## Candidate reads

`CurriculumDiscoveryRead` in `src/course_explorer/curriculum.py` states the two reverse reads, and both return sorted distinct canonical `group_id` strings rather than payloads or ORM entities.

`CurriculumRepository.get_substitution_source_group_ids(member_group_ids)` in `src/course_explorer/curriculum_repository.py` selects a canonical course when any member of one of its stored official substitution options names a requested course.

`CurriculumRepository.get_prerequisite_candidate_group_ids(source_group_ids, *, active_only)` selects a canonical course when its stored official prerequisite option names a requested course directly or names a module that can contain one. The module scope is one recursive CTE seeded from `ModuleRuleNode.node_type == ModuleRuleType.COURSE_UNIT_RULE` references and closed over `MODULE_RULE` references with `UNION`, so a cycle terminates. The `active_only` flag adds the stored activity condition.

`InferredPrerequisiteRepository.find_candidate_group_ids(source_group_ids, *, active_only=True)` in `src/course_explorer/prerequisite/repository.py` selects canonical courses whose stored inferred expression contains any requested identity as a nested `course` leaf, through one `jsonb_path_exists` read over the stored inferred-set store. Its default active-only scope adds the stored activity condition. The all-courses scope passes `active_only=False`, which omits that condition and reaches an inactive target through its retained inferred expression.

The command's source set starts as the chosen basis. With `include_inferred` set it adds the members of each basis course's current equivalence groups from `InferredEquivalenceRepository.read_for_members`, and it adds the substitution sources of the whole set through `get_substitution_source_group_ids`. The candidate set is the official reverse read, unioned with the inferred reverse read when inference is enabled, minus the chosen basis. The inferred reverse read applies the request's activity scope.

## Direct compilation and native evaluation

`PrerequisiteGraphCompiler.compile_direct_many(target_group_ids, *, include_inferred, active_only, retained_group_ids)` compiles each candidate's own direct options without course ancestors:

- Targets are sorted and deduplicated, and an empty request returns `()` before any read.
- The per-worker native fact loader reads the canonical courses, versions, periods, prerequisite and substitution options, modules, rule nodes, inferred sets and equivalence groups for the request closure. The handle retains the facts, so a repeated request issues no query. An unknown target raises `ValueError`, and a target whose course has no representative is omitted.
- Every compilation context enables recommended prerequisites, sets `direct_only`, carries the request's `active_only` and `include_inferred`, and carries `retained_group_ids` as the chosen basis.
- Exclusions do not change a direct compilation.
- The result is a `DirectPrerequisiteSummaryCompilation` per candidate. It pairs a frozen `PrerequisiteSolverSummary`, which holds the structure's root identity, academic year and the candidate's own course summary, with the request-local `native_bundle`.
- The native emitter writes the tagged narrow projection of each compilation: only the root identity, academic year and root course summary, never the node, fact, diagnostic or use record. Discovery reads the root identity and its own course summary from that view, so it never materializes or walks the full drawing records. The command frees each request bundle after all its evaluations, and the per-worker fact handle is held open across requests.

The drawing and prerequisite commands compile one root through `compile`, so they read the full structure for one target and never use the direct candidate array. Neither decoded direct form carries an outcome. `NativeBundle.evaluate(basis, *, mode, assumed_course_credits)` in `src/course_explorer/prerequisite/native_compiler.py` computes the outcomes for a chosen basis in one native call, with one outcome per compiled structure. The assumed value in hundredths substitutes only a missing source-course minimum. Each `NativeOutcome` reports `uses_assumed_credits` when a credit-bound piece in its evaluation retained a course whose minimum credit was substituted. [Satisfaction evaluation](/docs/backend/prerequisites/satisfaction-evaluation.md) states the fold, its coverage values and the rule each mode applies.

The evaluator returns one outcome per structure in bundle order, and each outcome carries the structure it belongs to. `evaluate_next_course_outcomes` returns a frozen `NextCourseOutcomes` whose `by_group` maps each root group id to `(required, recommended)` or `None` and whose `uses_assumed_credits` ORs the native outcomes.

The native evaluator branches on the mode. Under **All** it requires every category the candidate carries to be satisfied, and a category the candidate does not carry is neutral. Under **Partially covered** it requires at least one covered slot in the required or recommended category, which includes a course whose prerequisites are all covered. A covered basis course satisfies a leaf even when the activity scope is active-only, so an inactive retained basis course satisfies the courses that depend on it. Under active-only an inactive unretained candidate is denied by the activity scope before it can be returned. Under the all-courses scope an inactive candidate is evaluated like an active one, so the prerequisites it retains qualify it.

Under **Partially covered**, the list holds a course when at least one slot of its required or recommended category is covered, and that includes a course whose prerequisites are all covered. Both modes read the same coverage values, so a course's coverage values and its presence in the list never disagree.

## Coverage values and the marks

A result carries a required coverage fraction and a recommended coverage fraction, each measured on its own category. A course that carries no category holds `None` for that side rather than a zero share, so a partial mandatory set and a satisfied one are told apart. [Satisfaction evaluation](/docs/backend/prerequisites/satisfaction-evaluation.md#coverage) states how the slots of a branch are counted and what each requirement kind adds.

```text
ALL          : every non-None category root is eligible
AT_LEAST_ONE : at least one covered slot in required or recommended
depends_on_inferred = official-only compilation exists and its outcome is None
```

`eligible_next_course_group_ids` returns the sorted roots whose `by_group` outcome is not `None` and consumes a supplied carrier directly. `next_courses_from_compilations` builds the results and sets `depends_on_inferred` on a result only when an official-only compilation of the same candidate is present and its entry is `None`, so a candidate that is eligible from official prerequisites alone is never marked.

The command bounds the second pass with `eligible_next_course_group_ids(first_pass, chosen, mode=..., candidate_outcomes=first_pass_outcomes)`, so the official-only compilation covers exactly the candidates that can appear. Without `include_inferred` no official-only compilation exists, every mark is `False`, and no second compile runs.

## Tests

| File | Covers |
|---|---|
| `tests/next_courses/test_next_courses.py` | Both modes and their orders over the real direct compiler, required and recommended coverage with an absent category reported as `None`, relationship-local official precedence, free-choice and nearest-version source shapes that keep a real slot, the omitted basis courses and slot-less candidates, a partly covered option that qualifies only in `AT_LEAST_ONE`, a multi-course substitution that needs every member, a basis that satisfies direct knowledge without its ancestors, the activity scope, and the retained inactive basis. |
| `tests/next_courses/test_next_courses_command.py` | Input refusal before any settings or database read, the CLI defaults and toggles, report round-tripping, and an empty basis that returns an empty report. |
| `tests/next_courses/test_next_courses_repository.py` | An opt-in PostgreSQL proof over a normalized catalogue: current direct, recommended, module, substitution, equivalence-only, and inferred-only routes, a stale stored version that the reverse read overselects and the compiler drops, the all-courses scope, an inactive candidate qualified by its retained inferred relationship, the coverage order, partly covered substitutions, the exclusion of the same-input next courses from recommendations, and one persisted unknown-credit basis course that meets an official credit bound under a supplied nondefault assumption with the report flag true. |
| `tests/next_courses/test_report_cache.py` | The shared report cache of one canonical request and its raw-alias entries, including the assumed-credit policy in the key. |

The behaviour as the app presents it is described in [Next courses](/docs/backend/search-and-discovery/next-courses.md), and the recommendation list that reads this one is described in [Recommendation ranking](/docs/backend/search-and-discovery/recommendation-ranking.md).
