/**
 * The study-plan format registry and the lookups that select a format or an import script by id.
 *
 * Each record names its format id, its accepted suffixes, media types and capture type, its reader,
 * its label and help keys and its guide anchor. A file format carries its suffixes, media types and
 * reader, and a capture format carries its capture type and its parsed-record reader. The registry
 * array order is the offer order. The university table lists each format by this registry's id. A
 * file that no configured format recognises is refused with one message that says what to do.
 */
import { cancellationError } from "../../app/cachedRead";
import type { BrowserImport } from "../../state/types";
import { parseBrowserImport } from "../../state/store";
import type { UiTextKey } from "../../shared/wording";
import type { ExtractedPlan } from "./plan";
import { readSisuCapture, readSisuPage, SISU_CAPTURE_TYPE } from "./sisu";
import { readSisuPdfPlan } from "./pdfPlan";
import { PEPPI_CAPTURE_TYPE, readPeppiCapture, readPeppiPage } from "./peppi";
import { IMPORT_SCRIPTS, type ImportScript } from "./scripts";

/** One format id the university table and this registry share. */
type ImportFormatId =
  "sisu_page" | "sisu_pdf" | "sisu_capture" | "peppi_page" | "peppi_capture";
/** One configured study-plan import script. */
export type StudyPlanScriptId = "sisu" | "peppi";

/** One format's identity, accepted files and interface text. */
type ImportFormatBase = {
  id: ImportFormatId;
  suffixes: readonly string[];
  mediaTypes: readonly string[];
  label: UiTextKey;
  help: UiTextKey;
  guide: string;
};
/** One file format: resolved by its declared suffix or media type. */
type FileImportFormat = ImportFormatBase & {
  captureType: null;
  read(file: File, signal: AbortSignal): Promise<ExtractedPlan>;
};
/** One capture format: resolved by the capture `type` the file declares. */
type CaptureImportFormat = ImportFormatBase & {
  captureType: string;
  read(record: Record<string, unknown>): ExtractedPlan;
};
/** One configured study-plan import format. */
type ImportFormat = FileImportFormat | CaptureImportFormat;

/** The refusal message of a file that no configured format recognises. */
const IMPORT_FORMAT_REFUSAL =
  "This file is not one of this site's import formats. Use a file type listed in the import area.";

/**
 * The study-plan format registry: each platform's saved page and capture, and the Sisu plan PDF.
 */
const IMPORT_FORMATS: readonly ImportFormat[] = [
  {
    id: "sisu_page",
    suffixes: [".html", ".htm"],
    mediaTypes: ["text/html"],
    captureType: null,
    label: "Sisu saved page",
    help: "Open your plan in Sisu's Structure of studies and expand every module. Right-click the page, choose Save as, and keep the default type (Webpage, Complete). Import the saved .html file here and name the new profile. The file is read in this browser, not uploaded.",
    guide: "/docs#guide-sisu-import/saving-the-plan-page",
    read: readSisuPage,
  },
  {
    id: "sisu_pdf",
    suffixes: [".pdf"],
    mediaTypes: ["application/pdf"],
    captureType: null,
    label: "Sisu plan PDF",
    help: "A supported Sisu plan PDF imports the same way. In Sisu, open Structure of studies, choose ⋯ beside New study plan, choose Print study plan and save as PDF. The PDF is read in this browser. Official catalogue and structure reads use this site. Omitted items and their candidates are listed for you to check. Some modules may ask you to choose a version. The PDF contains personal identity data, so do not share it casually.",
    guide: "/docs#guide-sisu-import/printing-the-plan",
    read: readSisuPdfPlan,
  },
  {
    id: "sisu_capture",
    suffixes: [".json"],
    mediaTypes: ["application/json"],
    captureType: SISU_CAPTURE_TYPE,
    label: "Sisu capture",
    help: "Copy import script copies a console script that downloads sisu-plan-capture.json. Import the file here and name the new profile. The file is read in this browser, not uploaded.",
    guide: IMPORT_SCRIPTS.sisu.guide,
    read: readSisuCapture,
  },
  {
    id: "peppi_page",
    suffixes: [".html", ".htm"],
    mediaTypes: ["text/html"],
    captureType: null,
    label: "Peppi saved page",
    help: "Open your HOPS page in Peppi's Tarkastelu view. Right-click the page, choose Save as, and keep the default type (Webpage, Complete). Import the saved .html file here and name the new profile. The file is read in this browser, not uploaded.",
    guide: "/docs#guide-peppi-import/saving-the-hops-page",
    read: readPeppiPage,
  },
  {
    id: "peppi_capture",
    suffixes: [".json"],
    mediaTypes: ["application/json"],
    captureType: PEPPI_CAPTURE_TYPE,
    label: "Peppi capture",
    help: "Copy import script copies a console script that downloads peppi-hops-capture.json. Import the file here and name the new profile. The file is read in this browser, not uploaded.",
    guide: IMPORT_SCRIPTS.peppi.guide,
    read: readPeppiCapture,
  },
];

/** Return the configured formats in registry order and reject an unknown id. */
export function formatsForStudyPlan(
  ids: readonly string[],
): readonly ImportFormat[] {
  for (const id of ids)
    if (!IMPORT_FORMATS.some((format) => format.id === id))
      throw new Error(`Unknown study plan format: ${id}`);
  return IMPORT_FORMATS.filter((format) => ids.includes(format.id));
}

/** Return the configured script for one served script id, or null when none is configured. */
export function scriptForStudyPlan(id: string | null): ImportScript | null {
  if (id === null) return null;
  if (!Object.hasOwn(IMPORT_SCRIPTS, id))
    throw new Error(`Unknown study plan script: ${id}`);
  return IMPORT_SCRIPTS[id as StudyPlanScriptId];
}

/**
 * Read one imported file through the site's configured formats.
 *
 * A configured file format is matched by its declared suffix or media type. A typed capture is
 * resolved by its declared `type`, and the own profile and whole-state JSON kinds are the fallback.
 * The file is read once, and an unrecognised file raises the one refusal that says what to do.
 */
export async function readImportFile(
  file: File,
  formats: readonly ImportFormat[],
  signal: AbortSignal,
): Promise<BrowserImport | ExtractedPlan> {
  const name = file.name.toLowerCase();
  const mediaType = file.type;
  const byFile = formats.find(
    (format): format is FileImportFormat =>
      format.captureType === null &&
      (format.suffixes.some((suffix) => name.endsWith(suffix)) ||
        format.mediaTypes.includes(mediaType)),
  );
  if (byFile) return byFile.read(file, signal);
  const text = await file.text();
  if (signal.aborted) throw cancellationError(signal);
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    throw new Error(IMPORT_FORMAT_REFUSAL);
  }
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw new Error(IMPORT_FORMAT_REFUSAL);
  const record = value as Record<string, unknown>;
  const capture = formats.find(
    (format): format is CaptureImportFormat =>
      format.captureType !== null && format.captureType === record.type,
  );
  if (capture) return capture.read(record);
  if (
    record.type === "course-explorer/profile/3" ||
    record.type === "course-explorer/state/3"
  ) {
    // The own JSON is parsed here to read its `type` and again in the text boundary that validates it.
    return parseBrowserImport(text);
  }
  throw new Error(IMPORT_FORMAT_REFUSAL);
}
