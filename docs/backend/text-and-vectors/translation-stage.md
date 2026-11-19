# Translation stage

The translation stage selects the teaching text cells to translate, translates them through one local runtime, and retains a resumable output file.

The entries it produces reach the derived table through the import described in [Teaching text store](/docs/backend/text-and-vectors/teaching-text-store.md), and [Translation](/docs/backend/text-and-vectors/translation.md) describes the cells the stage fills, its output, and its pause and stop controls.

## The selector

`select_translation_work` in `src/course_explorer/translation/service.py` returns a `TranslationWorkSelection`. `_collect` reads every course with `CurriculumRepository.list_teaching_courses(active_only=None)`, `_collect_translation_work` applies an optional `course_limit` to that order before it filters representatives, and `_collect_course_work` classifies one course's mapped target cells.

A cell whose official target text exists counts as `official_target`. Otherwise the selector reads the target's source list from the translation priority map in order and takes the first source whose same-field official cell is nonblank. No such source counts as `missing_source`. Otherwise the cell becomes a `TranslationWorkItem` carrying the group id, the source identity, the field, the target language, the first populated listed source language, the exact source text, and the hash of that text. The cell counts as `current_derived` when a stored manual row uses any listed source and that source's official text hash equals the hash the row records, or when a model row uses that same first source, the same source hash, and `TranslationSettings.provenance_model`. Every other candidate cell is `pending` and appears in `items`. A target language outside the priority map produces no cell or work, and a derived row is never read as a source.

The four counts are per field and mapped target language in `TranslationSelectionCount`, and `tests/translation/test_translation_service.py` proves that the pending counts sum to `len(selection.items)`. Selection never writes, updates, or deletes a row.

## The provider

`TranslationProvider` in `src/course_explorer/translation/provider.py` is the only path that calls the translation runtime. It builds one `httpx.Client` from `TranslationSettings.base_url`, which is derived from the configured loopback host and port, with `trust_env=False`, `follow_redirects=False`, and the configured request timeout.

`_payload` posts to `v1/chat/completions` the configured alias, one `user` message whose single text part carries `source_lang_code`, `target_lang_code`, and the exact source text, the same two configured code strings in `chat_template_kwargs`, the configured temperature and maximum tokens, and `stream: false`.

`_validate_inputs` refuses blank text and two equal languages before any request, and `translate` on a closed client raises `TranslationRequestError`.

`_validate_response` decodes the body through `decode_json_response` into `_ChatCompletionResponse` with `_ChatChoice` and `_ChatMessage` (`strict=True`, `extra="ignore"`). A response must carry `object` `chat.completion`, the configured alias as `model`, a non-blank `system_fingerprint` ending in a hyphen and the first nine characters of the configured runtime revision, exactly one choice at index 0 with the `assistant` role and `finish_reason` `stop`, and non-blank content without a NUL character. Anything else raises `TranslationContractError`.

`request_json_with_retries` in `src/course_explorer/http_retry.py` owns the attempt loop with `max_attempts=settings.translation_max_attempts`, `retry_backoff_seconds=settings.translation_retry_backoff_seconds`, `retry_contract_errors=True`, and the `before_request` and `request_scope` callbacks of the worker. An `httpx.TransportError`, a 429 or 5xx status, and a response that fails the runtime contract are retryable, the delay before the retry that follows attempt N is the configured backoff times `2 ** (N - 1)`, and every other status is terminal. A refusal before the first request raises `RequestNotStarted` before any I/O, and a refusal during a backoff raises the terminal request error instead of sleeping again.

`TranslationSettings.provenance_model` in `src/course_explorer/config/translation.py` composes the persisted model identity from the model repository, the pinned revision, the artifact quantization, and the artifact SHA-256.

## The resumable worker and its evidence

`TranslationWorkerService.run` translates the selected items in their order. `_load_retained_output` reads a retained output through [`load_translation_file`](/docs/backend/text-and-vectors/teaching-text-store.md#the-translation-dtos-and-the-file-loader) and refuses two entries for one identity, and `_validate_retained` refuses an entry that is not a candidate of the selected scope, an entry whose source language or source text differs from the official source, an entry that is not a model translation, and an entry from another provenance model.

Each success appends the entry and republishes the whole array in candidate order through `atomic_write_private_text` in `src/course_explorer/administration.py`, which writes a temporary file in the target directory, flushes and fsyncs it, sets mode 0600, and renames it over the target. `on_checkpoint` then runs with the retained count, and a failure record beside the output is deleted. `before_pending_item` is consulted immediately before the provider call, and a refusal ends the run without that call, so the retained output is the partial report.

`_translate_item` builds the `DerivedTranslationInput` from the item, the returned text, `TranslationMethod.MODEL`, the configured provenance model, and the clock. A provider failure other than `RequestNotStarted` writes one `TranslationFailureRecord` through `_write_failure` and re-raises. The record holds the cell identity, `error_type`, and `error_message`, and `_safe_failure_message` accepts only a short fixed-shape provider message and otherwise returns the exception class name, so neither the source text nor the generated text reaches the file. The cell tuple is the failure identity and the record type identifies the stage.

`run_translation_command` in `src/course_explorer/translation/commands.py` composes one database context, one `CurriculumRepository`, one `DerivedTranslationRepository`, one `TranslationProvider`, and one `TranslationWorkerService`, and closes the provider before the engine is disposed.

The administrative half lives in `src/course_explorer_admin/translation/`. `TranslationWorkerStage` implements the stage seam of `course_explorer_admin.worker_session`, and `course_explorer_admin.translation.controller_worker` is the module the adapter launches.

`_publish_prepared_session` runs the selector on the session database and writes the prepared control and state, `run` owns the runtime and the checkpoint writes, `_retained_output_count` counts the retained output through the strict loader, and `_finalize` loads that output, requires its entry count to equal the recorded completed count, hashes it once, imports it through the [sole write path](/docs/backend/text-and-vectors/teaching-text-store.md#the-sole-write-path) of the store, and writes `TranslationControllerImportReceipt`.

`_read_import_receipt`, `_stop_updates`, and `_require_cleanup_evidence` keep the recorded counts in step with the retained evidence. The state DTOs are `TranslationControllerPolicy`, `TranslationControllerWorkerState`, `TranslationControllerStatus`, and `TranslationControllerImportReceipt` in `src/course_explorer_admin/translation/controller.py`. `TranslationControllerPolicy` is schema 3 and carries one `configuration_sha256` from `translation_configuration_sha256()`, which hashes the provenance model plus only the nondefault official languages and translation priority. `translation_stage()` in `src/course_explorer_admin/remote_stages.py` binds the adapter and uses the same fingerprint for its stage identity.

## Commands and tests

`scripts/admin.py translation <start|pause|resume|status|stop>` runs one session action through `course_explorer_admin.cli` and `run_translation_lifecycle_command`. The operator menu reaches the stage actions.

`tests/translation/test_translation_service.py` covers the selector partition, the reuse rule, and the pending order. `tests/translation/test_translation_provider.py` covers the request shape, the retries, the response contract, and the content-free errors. `tests/translation/test_translation_recovery.py` covers retained-output validation, atomic publication, the failure record, and the lifecycle callbacks. `tests/admin/translation/` covers the prepared session, the shared control path, the one-time import, resumption from retained output, and the cleanup evidence.
