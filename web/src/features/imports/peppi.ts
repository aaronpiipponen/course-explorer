/**
 * The Peppi HOPS saved-page and capture readers and the shared HOPS markup reader.
 *
 * The markup is parsed as data only. A module is a learning-unit container whose own study header
 * opens a STUDY_MODULE modal, and a course is a row whose own name button opens a COURSE_UNIT
 * modal. A container or row with another modal type is display-only and contributes nothing.
 */
import { cancellationError } from "../../app/cachedRead";
import type {
  ExtractedCourse,
  ExtractedItem,
  ExtractedModule,
  ExtractedPlan,
} from "./plan";

/** The capture file type the Peppi script downloads. */
export const PEPPI_CAPTURE_TYPE = "course-explorer/peppi-hops/1";
const INVALID_PLAN_FILE = "Invalid plan file";
const LU_CONTAINER = ".lu-container";
const LU_ITEM = `tr${LU_CONTAINER}, section${LU_CONTAINER}, article${LU_CONTAINER}`;

/** The refusal message of a saved page that is not a Peppi HOPS page. */
const PEPPI_PAGE_REFUSAL =
  "This file is not a saved Peppi HOPS page. Open the HOPS page (Tarkastelu) and save the page again.";

/** Return the nearest learning-unit container an element sits in, excluding the element itself. */
function closestLu(element: Element): Element | null {
  return element.parentElement?.closest(LU_CONTAINER) ?? null;
}

/** Elements of one selector below this element but not inside a nested learning-unit container. */
function own(root: Element, selector: string): Element[] {
  return [...root.querySelectorAll(selector)].filter(
    (found) => closestLu(found) === root,
  );
}

/** Return the code argument of one element's learning-unit modal call, or null for another type. */
function modalCode(
  button: Element,
  modalType: "STUDY_MODULE" | "COURSE_UNIT",
): string | null {
  const onclick = button.getAttribute("onclick") ?? "";
  const match = new RegExp(
    `openLearningUnitInfoModal\\(\\s*'${modalType}'\\s*,\\s*'([^']*)'`,
  ).exec(onclick);
  return match ? match[1] : null;
}

/** Return the first button below one element that opens one modal type. */
function modalButton(
  root: Element,
  modalType: "STUDY_MODULE" | "COURSE_UNIT",
): Element | null {
  return (
    [...root.querySelectorAll("button[onclick]")].find(
      (button) => modalCode(button, modalType) !== null,
    ) ?? null
  );
}

/** Return one required own text value or fail with the unsupported-plan message. */
function requiredText(root: Element, selector: string): string {
  const text = own(root, selector)[0]?.textContent?.trim();
  if (!text) throw new Error(INVALID_PLAN_FILE);
  return text;
}

/** Return one course row's captured course, or null for a display-only row. */
function parseCourse(row: Element): ExtractedCourse | null {
  const button = modalButton(row, "COURSE_UNIT");
  if (!button) return null;
  const code = modalCode(button, "COURSE_UNIT");
  if (!code) throw new Error(INVALID_PLAN_FILE);
  return {
    kind: "course",
    code,
    name: requiredText(row, ".lu-name"),
    completed: own(row, ".status-badge.status-completed").length > 0,
  };
}

/**
 * Return one learning-unit container's captured module, or null for a display-only container.
 *
 * The module's children are its own course rows and its directly contained module containers, in
 * document order.
 */
function parseModule(container: Element): ExtractedModule | null {
  const header = own(container, "h4.study-header")[0];
  if (!header) return null;
  const button = modalButton(header, "STUDY_MODULE");
  if (!button) return null;
  const children: ExtractedItem[] = [];
  for (const child of container.querySelectorAll(LU_ITEM)) {
    if (closestLu(child) !== container) continue;
    const item =
      child.localName === "tr" ? parseCourse(child) : parseModule(child);
    if (item) children.push(item);
  }
  return {
    kind: "module",
    code: modalCode(button, "STUDY_MODULE"),
    name: requiredText(container, ".lu-name"),
    sourceVersionId: null,
    children,
  };
}

/** Return the top-level plan entries of one structure in document order. */
function structureItems(structure: Element): ExtractedItem[] {
  const items: ExtractedItem[] = [];
  for (const child of structure.querySelectorAll(LU_ITEM)) {
    const ancestor = child.parentElement?.closest(LU_CONTAINER) ?? null;
    if (ancestor !== null) continue;
    const item =
      child.localName === "tr" ? parseCourse(child) : parseModule(child);
    if (item) items.push(item);
  }
  return items;
}

/**
 * Parse one Peppi HOPS structure markup into the neutral extracted plan.
 *
 * The programme heading splits at its first comma into the programme code and name, and a heading
 * without a comma is the programme code alone, which also serves as its name. A blank heading, a
 * blank code or name, and a structure with no readable module or course fail with
 * `Invalid plan file`.
 */
function parsePeppiHopsMarkup(
  programme: string,
  markup: string,
): ExtractedPlan {
  const comma = programme.indexOf(",");
  const code = (comma === -1 ? programme : programme.slice(0, comma)).trim();
  const name = comma === -1 ? code : programme.slice(comma + 1).trim();
  if (!code || !name) throw new Error(INVALID_PLAN_FILE);
  const document = new DOMParser().parseFromString(markup, "text/html");
  const structure = document.querySelector("#structure");
  if (!structure) throw new Error(INVALID_PLAN_FILE);
  const children = structureItems(structure);
  if (!children.length) throw new Error(INVALID_PLAN_FILE);
  return {
    programmes: [
      {
        kind: "module",
        code,
        name,
        sourceVersionId: null,
        children,
      },
    ],
  };
}

/**
 * Read one browser-saved Peppi HOPS page.
 *
 * The saved document must hold the programme heading and the structure. A missing element or
 * unreadable plan markup becomes `PEPPI_PAGE_REFUSAL` so a bad saved page says what to do.
 */
export async function readPeppiPage(
  file: File,
  signal: AbortSignal,
): Promise<ExtractedPlan> {
  const saved = new DOMParser().parseFromString(await file.text(), "text/html");
  if (signal.aborted) throw cancellationError(signal);
  const heading = saved.querySelector("#header-row-div h2");
  const structure = saved.querySelector("#structure");
  if (!heading || !structure) throw new Error(PEPPI_PAGE_REFUSAL);
  try {
    return parsePeppiHopsMarkup(
      heading.textContent?.trim() ?? "",
      structure.outerHTML,
    );
  } catch (caught) {
    if (caught instanceof Error && caught.message === INVALID_PLAN_FILE)
      throw new Error(PEPPI_PAGE_REFUSAL);
    throw caught;
  }
}

/**
 * Read one Peppi capture the script downloads.
 *
 * The record must carry the exact capture type and a non-empty programme and markup string. The
 * markup is read as it is, so a parser refusal reaches the caller unchanged.
 */
export function readPeppiCapture(
  record: Record<string, unknown>,
): ExtractedPlan {
  if (
    record.type !== PEPPI_CAPTURE_TYPE ||
    typeof record.programme !== "string" ||
    typeof record.markup !== "string"
  )
    throw new Error(INVALID_PLAN_FILE);
  if (!record.programme.trim() || !record.markup.trim())
    throw new Error(INVALID_PLAN_FILE);
  return parsePeppiHopsMarkup(record.programme, record.markup);
}
