# Study-plan import

The study-plan import reads one saved page, plan PDF or script capture into one neutral plan, resolves its official identities and places the resolved choices into the structure of studies.

## The neutral plan and the format registry

`web/src/features/imports/plan.ts` holds the neutral plan and the profile steps. `ExtractedCourse` and `ExtractedModule` carry only the name, code, nesting, completion mark and exact module version a file shows. `ImportedCourse` adds the canonical `groupId` the resolve step sets, and `ImportedModule` adds its `groupId` and `versionId`. `ExtractedPlan` and `ImportedPlan` hold the programmes in file order, `ImportIssue` and `ImportIssueCandidate` hold the transient omissions, and `validateImportedPlan` is the untrusted stored-value boundary: it raises `Invalid plan file` when two top-level programmes carry the same non-null identity, discards an unknown field and accepts a repeated course occurrence.

`prepareImportedProfile(plan, name, language)` builds the profile one import creates: one ordered distinct selected identity per resolved course, a completion for every identity a course captured as completed, one exact version pair per resolved module, the top-level programme identities in plan order and one independent copy of the hierarchy in `pendingPlanImport`. Two different exact ids for one group raise `The plan selects two versions of a module`. `pendingImportBlocked(state)` answers whether a waiting plan sits beside saved structure edits, and `pendingImportModuleIds(plan)` returns every resolved module identity for the pending reads.

`web/src/features/imports/formats.ts` is the one format registry. `IMPORT_FORMATS` holds one record per format id: `sisu_page`, `sisu_pdf`, `sisu_capture`, `peppi_page` and `peppi_capture`. Each record carries its accepted suffixes and media types, its capture type, its reader, its label and help keys and its guide anchor. `formatsForStudyPlan(ids)` returns the configured formats in registry order and rejects an unknown id, and `scriptForStudyPlan(id)` returns the configured script for a served id, rejects an unknown id and returns null for a null id. `readImportFile(file, formats, signal)` matches a configured file format by its suffix or media type, resolves a configured capture by its `type`, and falls back to the own `course-explorer/profile/3` and `course-explorer/state/3` files. Any file that no configured format recognises raises `This file is not one of this site's import formats. Use a file type listed in the import area.`

## The Sisu readers

`web/src/features/imports/sisu.ts` holds the shared markup reader, `readSisuPage`, `readSisuCapture` and the refusal constant `SISU_PAGE_REFUSAL`. The markup is parsed with `DOMParser` as data and is never inserted into the document or evaluated. One reader walks the `plan-module` elements: it reads each element's own `.status [id]` and strips its `-statuscell` suffix into `sourceVersionId`, its own `[ng-bind=":: $ctrl.module.code"]`, its own `[data-cy="plan-module-name"]`, and its own `plan-module` and `course-unit-box` children in document order. A course carries `.courseunit-code`, the `.title-box` text without the code and an `app-course-unit-attainment-notification` completion marker. A top-level header with no graduation flag, no grouping title and no module information control is the education container, and its direct degree rows become the programmes.

`readSisuPage` requires at least one top-level plan module and no `plan-module .collapse[aria-expanded="false"]`, joins the top-level modules' `outerHTML` and passes it to the markup reader. A missing plan, a collapsed module or unreadable markup raises `SISU_PAGE_REFUSAL`. `readSisuCapture(record)` receives the record the dispatch parsed, requires the exact `course-explorer/sisu/1` type and a non-empty `markup` string and reads no lookup map.

## The Peppi readers

`web/src/features/imports/peppi.ts` holds the shared markup reader, `readPeppiPage`, `readPeppiCapture` and the refusal constant `PEPPI_PAGE_REFUSAL`. The programme heading splits at its first comma into the programme code and name, and a heading without a comma carries the programme code alone and uses the code as its name. Inside `#structure`, a learning-unit container whose own `h4.study-header` button opens `openLearningUnitInfoModal('STUDY_MODULE', ...)` is a module, and its name is its own `.lu-name` text. Its children are its own `tr.lu-container` course rows and its directly contained module containers. A row whose own button opens `openLearningUnitInfoModal('COURSE_UNIT', ...)` is a course, with its own `.lu-name` name and its `.status-badge.status-completed` completion. A container or row with another modal type is display-only and contributes nothing. The container is either a `section` or an `article` element.

`readPeppiPage` requires `#header-row-div h2` and `#structure`, and a missing element or unreadable markup raises `PEPPI_PAGE_REFUSAL`. `readPeppiCapture(record)` receives the record the dispatch parsed, requires the exact `course-explorer/peppi-hops/1` type, a non-empty programme string and a non-empty `markup` string and lets a parser refusal through unchanged.

## The PDF reader and extractor

`web/src/features/imports/pdf.ts` is the PDF.js decoder. The module imports PDF.js and its same-version worker with the app bundle. `readSisuPdf(file, signal)` decodes a PDF only when the import reads one, reads the file's array buffer once and the text layer of every page once, never renders a page and never reads annotations, scripts, attachments or metadata. A decoder failure raises `Invalid Sisu PDF plan`.

`web/src/features/imports/pdfRows.ts` is the pure extractor. `extractSisuPdfRows(pages)` keeps body text of at most 9 points, groups items by page and a 2-point baseline bucket, splits the left name and code column at x 200 and merges runs with a geometric word gap of a fifth of the greater text height. Continuation rows merge across wrapped lines and page breaks, and ligatures and whitespace are normalized.

`web/src/features/imports/pdfPlan.ts` composes the neutral hierarchy. `extractedPlanFromRows(rows)` starts a top-level frame for a module row at the plan PDF's left margin and clears the stack there, attaches every other module row through its geometric candidates and joins a course row to the current frame. The frame carries no programme flag, because the plan PDF has no degree identity. The left-margin rule alone starts the top-level frames. A course with no open frame raises `Invalid Sisu PDF plan`. `readSisuPdfPlan` awaits the decoder and returns the composed plan. It reads no catalogue and no structure report.

## The resolve step

`web/src/features/imports/resolve.ts` holds `resolveExtractedPlan(plan, cache, signal)`, the only catalogue and structure matcher. It reads the catalogue once through `readCachedCatalogue` and takes its dataset identity as the operation identity. The catalogue index holds courses by exact code, module groups by exact candidate code and module groups by exact `source_version_id`.

The step records the exact version of every module whose `sourceVersionId` matches a catalogue candidate and the sole version of every uniquely code-resolved top-level programme before the first structure read. The programme roots, every version-resolved module group and the sole group of a top-level code are read in the first batch through `readCachedStudyStructures`, and one second batch covers roots the walk discovered and a sole selection it recorded. Every batch is checked against the operation identity with `acceptOperationIdentity`.

A top-level module resolves to a unique `DegreeProgramme` group or to one unique group as a programme, and a top-level container with no code promotes its children. A plan with no resolved programme raises `Invalid plan file`. A course resolves by exact catalogue code. A module resolves by exact `sourceVersionId`, then exact code and exact Finnish-or-English name among the direct module children of its resolved parent's scope. Only a top-level module's code is matched against the whole catalogue. A module whose parent scope is unreadable and whose parent has several versions or an unsatisfied selection reports `version_choice_pending`. Unmatched items stay in the plan as null-identity placeholders with their children, and the issues stay transient. The step writes nothing.

## Placement and the profile step

`web/src/features/imports/placement.ts` holds `placePendingImport(state, content, unavailable)`. It returns null while no plan waits, while `pendingImportBlocked` is true and while a read the plan needs has not settled.

Otherwise it walks each available programme in plan order, reads each scope's official homes again after a placement, selects a matched choice path through `nextChoices`, records the matched owner as the course or module place and descends into a matched module's node. A module with a resolved identity and no official scope joins its captured parent through `modulePlacements`, and its own report supplies its added content. The step keeps its match only for a null-identity module whose parent scope the version choice had not unlocked, by exact code and then exact Finnish-or-English official name, and it never re-matches an identity the resolve step set.

An official identity a scope does not capture is suppressed through a null placement, unless a placement record already holds that identity elsewhere. An identity offered twice in one scope raises `The plan choice is ambiguous`.

The returned change clears `pendingPlanImport` in the same write that applies the choices.

`StructurePane` reads `pendingImportModuleIds` and shows the version pickers before that write, and `applyStructure` writes the change atomically. `ImportNotice` renders the transient placement result from `importWording.ts`.

## The dialog and the transfer surface

`web/src/features/imports/ImportPlanDialog.tsx` is the one plan dialog. It owns the `Profile name` field with input id `plan-profile-name`, the `Import study plan` title, the programme names in plan order, the waiting and omitted items, the candidates of an ambiguous item and the existing-profile overwrite action. It calls `onImport(trimmedName, existingProfileId)` or `onCancel`, and it renders the confirm error.

`web/src/features/profiles/ProfileTransfer.tsx` reads the served `study_plan_script` and `study_plan_formats`, builds the configured formats and script, and keeps one pending value that is either a `BrowserImport` or a resolved plan with its issues. `acceptFile` calls `readImportFile` once under one `AbortController`, resolves an extracted plan through `resolveExtractedPlan`, and hands the result to the dialog or the profile and whole-state confirmations. The always-shown area accepts the own JSON kinds and exactly the configured formats, the drop note names the JSON profile export plus the configured format labels, and **Copy import script** appears only when a script is configured. A drop accepts one file, and a drop of any other number raises `Choose one file`. The bottom row holds the help drawer, the conditional copy control and Export. The empty Structure area mounts the same transfer with its own heading and the same export list.

`web/src/features/imports/scripts.ts` imports the two complete scripts from `web/src/features/imports/scripts/sisu-plan.js` and `web/src/features/imports/scripts/peppi-hops.js` as raw text. Each script is one committed file copied verbatim. Nothing assembles or rewrites it. Neither script imports anything, calls `fetch` or names a university. `Sisu import script` and `Peppi import script` are the two script labels carried by the records, and each record carries its own help key and guide anchor.

## Where a change belongs

- A change to a file reader belongs in its `web/src/features/imports/sisu.ts`, `peppi.ts`, `pdf.ts` or `pdfPlan.ts` module.
- A change to the format list or file dispatch belongs in `web/src/features/imports/formats.ts` and the university table.
- A change to catalogue or structure matching belongs in `web/src/features/imports/resolve.ts`.
- A change to how a resolved plan becomes a profile belongs in `web/src/features/imports/plan.ts`.
- A change to how a plan is placed into the structure belongs in `web/src/features/imports/placement.ts`.
- A change to a reason's text belongs in `web/src/features/imports/importWording.ts`.
- A change to a script's text belongs in its own file under `web/src/features/imports/scripts/`.

## Tests

| File | Covers |
|---|---|
| `web/src/features/imports/studyPlanImport.test.ts` | The Sisu capture and saved-page parse and the Peppi capture and saved-page parse, the offer order, the unknown-id refusal and both page refusals, the import profile and scoped placement with suppressed uncaptured defaults and one atomic pending clear, the code match of a null-identity module in a readable scope, the named omissions with representable courses kept loose, a waiting plan beside restored edits placing nothing, the extraction of positioned FI/EN rows, and strict catalogue and parent-scope matching with transient candidates, a sole version and an unset multi-version module |
| `web/src/features/profiles/ProfileTransfer.test.ts` | The rendered format refusal and whole-state confirmation, the profile overwrite, a Peppi saved-page rendering with its script button, format help and drop note, and one study plan that stays transient through cancel and reselect for a PDF, a Sisu capture and a Peppi saved page, with candidates absent from the saved envelope |
