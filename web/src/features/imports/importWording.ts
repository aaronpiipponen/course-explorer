/** Interface wording keys for every study-plan import omission and waiting reason. */
import type { UiTextKey } from "../../shared/wording";
import type { ImportIssue } from "./plan";

/** The one reason-key table shared by the confirmation and the import result notice. */
export const IMPORT_REASON_KEYS: Record<ImportIssue["reason"], UiTextKey> = {
  unresolved:
    "The plan did not identify this item uniquely, so it was left out.",
  module_not_placed:
    "The catalogue has no structure for this module, so it was left out. Its courses that are in the catalogue are in Unplaced studies.",
  course_not_represented:
    "This course is not in the catalogue, so it was left out together with its completion.",
  no_official_match: "No official match was found, so this item was left out.",
  ambiguous_official_match:
    "Several official items match, so this item was left out.",
  version_choice_pending:
    "This module is matched after you choose the version of the module above it.",
};
