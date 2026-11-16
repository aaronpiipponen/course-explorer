/**
 * The one PDF composition from one supported Sisu plan print to the neutral extracted plan.
 *
 * The reader decodes the print's positioned text, then this module rebuilds the module hierarchy
 * from the print's indentation alone. It reads no catalogue and no structure report: official
 * identity and the promotion of a container's children belong to the shared resolve step.
 */
import { cancellationError } from "../../app/cachedRead";
import { readSisuPdf } from "./pdf";
import {
  INDENT_TOLERANCE,
  INVALID_SISU_PDF_PLAN,
  type SisuPdfRow,
} from "./pdfRows";
import type { ExtractedCourse, ExtractedModule, ExtractedPlan } from "./plan";

/** One captured frame of the document-order module stack. */
type Frame = {
  node: ExtractedModule;
  x: number;
  anomaly: boolean;
};

/** Return the parent frame of one stack frame, or null for the first frame. */
function parentOf(stack: readonly Frame[], frame: Frame): Frame | null {
  const index = stack.indexOf(frame);
  return index > 0 ? stack[index - 1] : null;
}

/** Return every geometric parent frame one module row may belong to. */
function geometricCandidates(
  stack: readonly Frame[],
  x: number,
): { candidates: Frame[]; defaultParent: Frame | null } {
  const base =
    [...stack].reverse().find((frame) => !frame.anomaly) ??
    stack[stack.length - 1];
  const same = stack.filter(
    (frame) => !frame.anomaly && Math.abs(frame.x - x) <= INDENT_TOLERANCE,
  );
  if (same.length) {
    const candidates = [same[same.length - 1]];
    const sibling = parentOf(stack, same[0]);
    if (sibling) candidates.push(sibling);
    return { candidates, defaultParent: sibling };
  }
  if (base && x > base.x + INDENT_TOLERANCE)
    return { candidates: [base], defaultParent: base };
  const top = stack[stack.length - 1];
  if (!top) return { candidates: [], defaultParent: null };
  const candidates = [top];
  for (const frame of [...stack].reverse()) {
    if (Math.abs(frame.x - x) > INDENT_TOLERANCE) continue;
    const parent = parentOf(stack, frame);
    if (parent) candidates.push(parent);
  }
  return { candidates, defaultParent: candidates[0] };
}

/**
 * Build the neutral plan hierarchy from one supported print's extracted rows.
 *
 * The supported print puts every top-level programme, including the education header, on the left
 * margin, so a module row at that margin starts a top-level frame and clears the stack. Every other
 * module row attaches through the geometric candidates, and a course row joins the current frame. A
 * course with no open frame fails with `Invalid Sisu PDF plan`.
 */
export function extractedPlanFromRows(
  rows: readonly SisuPdfRow[],
): ExtractedPlan {
  const plan: ExtractedPlan = { programmes: [] };
  const moduleRows = rows.filter((row) => row.kind === "module");
  if (!moduleRows.length) throw new Error(INVALID_SISU_PDF_PLAN);
  const leftMargin = Math.min(...moduleRows.map((row) => row.x));
  let stack: Frame[] = [];
  const pushModule = (
    row: SisuPdfRow,
    node: ExtractedModule,
    frame: Frame,
    parent: Frame | null,
  ) => {
    if (parent) {
      parent.node.children.push(node);
      while (stack.length && stack[stack.length - 1] !== parent) stack.pop();
      if (stack[stack.length - 1] !== parent)
        throw new Error(INVALID_SISU_PDF_PLAN);
      frame.anomaly =
        row.x < parent.x - INDENT_TOLERANCE ||
        (Math.abs(row.x - parent.x) <= INDENT_TOLERANCE && parent.anomaly);
    } else {
      plan.programmes.push(node);
      stack = [];
    }
    stack.push(frame);
  };
  for (const row of rows) {
    if (row.kind === "course") {
      const current = stack[stack.length - 1];
      if (!current) throw new Error(INVALID_SISU_PDF_PLAN);
      const course: ExtractedCourse = {
        kind: "course",
        code: row.code ?? "",
        name: row.name,
        completed: row.completed,
      };
      if (course.code === "") throw new Error(INVALID_SISU_PDF_PLAN);
      current.node.children.push(course);
      continue;
    }
    const node: ExtractedModule = {
      kind: "module",
      code: row.code,
      name: row.name,
      sourceVersionId: null,
      children: [],
    };
    const atLeftMargin = Math.abs(row.x - leftMargin) <= INDENT_TOLERANCE;
    if (atLeftMargin) {
      pushModule(row, node, { node, x: row.x, anomaly: false }, null);
      continue;
    }
    const geometric = geometricCandidates(stack, row.x);
    if (!geometric.defaultParent) throw new Error(INVALID_SISU_PDF_PLAN);
    pushModule(
      row,
      node,
      { node, x: row.x, anomaly: false },
      geometric.defaultParent,
    );
  }
  return plan;
}

/** Read one supported Sisu plan PDF and build its neutral extracted plan. */
export async function readSisuPdfPlan(
  file: File,
  signal: AbortSignal,
): Promise<ExtractedPlan> {
  const rows = await readSisuPdf(file, signal);
  if (signal.aborted) throw cancellationError(signal);
  return extractedPlanFromRows(rows);
}
