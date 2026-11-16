/**
 * One import script record per platform, each carrying the committed script's raw text, label,
 * help key and guide anchor.
 *
 * The copy control copies the script text as the file holds it, and nothing assembles or rewrites
 * it.
 */
import sisuPlan from "./scripts/sisu-plan.js?raw";
import peppiHops from "./scripts/peppi-hops.js?raw";
import type { UiTextKey } from "../../shared/wording";
import type { StudyPlanScriptId } from "./formats";

/** One study-plan import script: its label, exact text, help key and documentation anchor. */
export type ImportScript = {
  label: UiTextKey;
  text: string;
  help: UiTextKey;
  guide: string;
};

/** The one script record per platform. */
export const IMPORT_SCRIPTS: Record<StudyPlanScriptId, ImportScript> = {
  sisu: {
    label: "Sisu import script",
    text: sisuPlan,
    help: "Open your plan in Sisu's Structure of studies and expand every module. Copy import script copies a console script. Read the whole script before running it in that Sisu tab. It reads the open page and downloads sisu-plan-capture.json without making a network request.",
    guide: "/docs#guide-sisu-import/the-script",
  },
  peppi: {
    label: "Peppi import script",
    text: peppiHops,
    help: "Open your HOPS page in Peppi's Tarkastelu view. Copy import script copies a console script. Read the whole script before running it in that Peppi tab. It reads the open page and downloads peppi-hops-capture.json without making a network request.",
    guide: "/docs#guide-peppi-import/the-script",
  },
};
