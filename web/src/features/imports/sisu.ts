/**
 * The Sisu saved-page and capture readers and the shared Sisu page markup reader.
 *
 * The markup is parsed as data only and contributes names, codes, nesting, a completion marker and
 * the module version id of a status cell. The capture holds only that markup, so no lookup map is
 * read and no network request is made.
 */
import { cancellationError } from "../../app/cachedRead";
import type {
  ExtractedCourse,
  ExtractedItem,
  ExtractedModule,
  ExtractedPlan,
} from "./plan";

/** The capture file type the Sisu script downloads. */
export const SISU_CAPTURE_TYPE = "course-explorer/sisu/1";
const INVALID_PLAN_FILE = "Invalid plan file";
const MODULE_TAG = "plan-module";
const COURSE_BOX_TAG = "course-unit-box";

/** The refusal message of a saved Sisu page that is not a fully expanded plan. */
const SISU_PAGE_REFUSAL =
  "This file is not a saved Sisu plan page with every module expanded. Open the plan in Sisu's Structure of studies, expand every module, and save the page again.";

/**
 * Return the elements one selector finds below this element but not inside a module nested in it.
 */
function own(root: Element, selector: string): Element[] {
  const owner = root.closest(MODULE_TAG);
  return [...root.querySelectorAll(selector)].filter(
    (found) => found.closest(MODULE_TAG) === owner,
  );
}

/** Return the modules and course boxes that sit directly in this module, in document order. */
function ownChildren(module: Element): Element[] {
  return [
    ...module.querySelectorAll(`${MODULE_TAG}, ${COURSE_BOX_TAG}`),
  ].filter((child) => child.parentElement?.closest(MODULE_TAG) === module);
}

function ownText(module: Element, selector: string): string | null {
  const text = own(module, selector)[0]?.textContent?.trim();
  return text ? text : null;
}

function requiredText(module: Element, selector: string): string {
  const text = ownText(module, selector);
  if (text === null) throw new Error(INVALID_PLAN_FILE);
  return text;
}

function parseModule(element: Element): ExtractedModule {
  const statusId = own(element, ".status [id]")[0]?.id;
  return {
    kind: "module",
    code: ownText(element, '[ng-bind=":: $ctrl.module.code"]'),
    name: requiredText(element, '[data-cy="plan-module-name"]'),
    sourceVersionId: statusId ? statusId.replace(/-statuscell.*$/, "") : null,
    children: parseChildren(element),
  };
}

function parseChildren(module: Element): ExtractedItem[] {
  return ownChildren(module).map((child) =>
    child.localName === MODULE_TAG ? parseModule(child) : parseCourse(child),
  );
}

function parseCourse(box: Element): ExtractedCourse {
  const code = requiredText(box, ".courseunit-code");
  const title = requiredText(box, ".title-box");
  const name = title.replace(code, "").replace(/\s+/g, " ").trim();
  if (!name) throw new Error(INVALID_PLAN_FILE);
  return {
    kind: "course",
    code,
    name,
    // The marker states that the course unit has an attainment. No grade value is read.
    completed:
      box.querySelector("app-course-unit-attainment-notification") !== null,
  };
}

/** Return the top-level plan modules of one page, in document order. */
function topModules(doc: Document): Element[] {
  return [...doc.querySelectorAll(MODULE_TAG)].filter(
    (module) => module.parentElement?.closest(MODULE_TAG) === null,
  );
}

/**
 * Return whether one top-level module is the education row that wraps the degrees of a plan.
 *
 * A degree carries the graduation flag and an information button, and a grouping row carries the
 * grouping title. A header with none of the three is the education container.
 */
function isEducationHeader(module: Element): boolean {
  return (
    own(module, ".graduate").length === 0 &&
    own(module, ".title.grouping-module").length === 0 &&
    own(module, '[id^="module-info-modal-open-"]').length === 0
  );
}

function parseProgrammes(doc: Document): ExtractedModule[] {
  const programmes: ExtractedModule[] = [];
  for (const top of topModules(doc)) {
    if (!isEducationHeader(top)) {
      programmes.push(parseModule(top));
      continue;
    }
    // The education is a container, so its degree rows become the top-level programmes.
    for (const degree of ownChildren(top).filter(
      (child) => child.localName === MODULE_TAG,
    )) {
      if (own(degree, ".graduate").length === 0)
        throw new Error(INVALID_PLAN_FILE);
      programmes.push(parseModule(degree));
    }
  }
  return programmes;
}

/**
 * Parse one Sisu plan page's module markup into the neutral extracted plan.
 *
 * The markup is parsed as data and never inserted into the document or evaluated. Markup whose
 * page holds no readable programme fails with `Invalid plan file`.
 */
function parseSisuPlanMarkup(markup: string): ExtractedPlan {
  const programmes = parseProgrammes(
    new DOMParser().parseFromString(markup, "text/html"),
  );
  if (!programmes.length) throw new Error(INVALID_PLAN_FILE);
  return { programmes };
}

/**
 * Read one browser-saved Sisu structure page.
 *
 * The saved document must hold at least one top-level plan module and no collapsed module. A
 * missing plan, a collapsed module or unreadable plan markup becomes `SISU_PAGE_REFUSAL` so a bad
 * saved page says what to do.
 */
export async function readSisuPage(
  file: File,
  signal: AbortSignal,
): Promise<ExtractedPlan> {
  const saved = new DOMParser().parseFromString(await file.text(), "text/html");
  if (signal.aborted) throw cancellationError(signal);
  const top = topModules(saved);
  if (
    top.length === 0 ||
    saved.querySelector(`${MODULE_TAG} .collapse[aria-expanded="false"]`) !==
      null
  )
    throw new Error(SISU_PAGE_REFUSAL);
  try {
    return parseSisuPlanMarkup(top.map((module) => module.outerHTML).join(""));
  } catch (caught) {
    if (caught instanceof Error && caught.message === INVALID_PLAN_FILE)
      throw new Error(SISU_PAGE_REFUSAL);
    throw caught;
  }
}

/**
 * Read one Sisu capture the script downloads.
 *
 * The record must carry the exact capture type and a non-empty markup string. The markup is read as
 * it is, so a parser refusal reaches the caller unchanged.
 */
export function readSisuCapture(
  record: Record<string, unknown>,
): ExtractedPlan {
  if (record.type !== SISU_CAPTURE_TYPE || typeof record.markup !== "string")
    throw new Error(INVALID_PLAN_FILE);
  if (record.markup.trim() === "") throw new Error(INVALID_PLAN_FILE);
  return parseSisuPlanMarkup(record.markup);
}
