/**
 * The neutral shape every study-plan reader returns and the profile steps shared by every importer.
 *
 * A reader names only the names, codes, nesting, completion marks and module version identity its
 * file carries, with no site identity and no official group. Resolution adds those identities, and
 * the profile step stores the plan and its module versions until Structure of studies places it.
 */
import { initialProfileState } from "../../state/defaults";
import type { DisplayLanguage, Profile, ProfileState } from "../../state/types";

/** One captured course unit with its file code, name and attainment mark. */
export type ExtractedCourse = {
  kind: "course";
  code: string;
  name: string;
  completed: boolean;
};
/** One captured module with its file code, name, exact version identity and children in file order. */
export type ExtractedModule = {
  kind: "module";
  code: string | null;
  name: string;
  /** The exact module version the file names, when it names one. */
  sourceVersionId: string | null;
  children: ExtractedItem[];
};
/** One captured entry of a module. */
export type ExtractedItem = ExtractedCourse | ExtractedModule;
/** A captured study plan: its programmes in file order, with no site identity. */
export type ExtractedPlan = { programmes: ExtractedModule[] };

/** One resolved course unit with its canonical group id, null when no catalogue course matches. */
export type ImportedCourse = {
  kind: "course";
  groupId: string | null;
  code: string;
  name: string;
  completed: boolean;
};
/** One resolved module with its canonical group id, its exact version id and its children. */
export type ImportedModule = {
  kind: "module";
  groupId: string | null;
  /** The exact official version the module resolves to, null when unresolved. */
  versionId: string | null;
  code: string | null;
  name: string;
  children: ImportedItem[];
};
/** One resolved entry of a module. */
export type ImportedItem = ImportedCourse | ImportedModule;
/** A resolved study plan: its programmes in file order. */
export type ImportedPlan = { programmes: ImportedModule[] };
/** One distinct official candidate an ambiguous match named, for confirmation only. */
export type ImportIssueCandidate = {
  groupId: string;
  code: string | null;
  name: string;
};
/** One captured item the import did not place, named by its captured name and code. */
export type ImportIssue = {
  name: string;
  code: string | null;
  reason:
    | "unresolved"
    | "module_not_placed"
    | "course_not_represented"
    | "no_official_match"
    | "ambiguous_official_match"
    | "version_choice_pending";
  /** The distinct official candidates one ambiguous match named, for confirmation only. */
  candidates?: readonly ImportIssueCandidate[];
};

const INVALID_PLAN_FILE = "Invalid plan file";

function planObject(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw new Error(INVALID_PLAN_FILE);
  return value as Record<string, unknown>;
}

function planIdentity(value: unknown): string {
  if (typeof value !== "string" || value === "")
    throw new Error(INVALID_PLAN_FILE);
  return value;
}

function planNullableIdentity(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  return planIdentity(value);
}

function storedModule(value: unknown): ImportedModule {
  const saved = planObject(value);
  if (saved.kind !== "module") throw new Error(INVALID_PLAN_FILE);
  if (!Object.hasOwn(saved, "versionId")) throw new Error(INVALID_PLAN_FILE);
  return {
    kind: "module",
    groupId: planNullableIdentity(saved.groupId),
    versionId: planNullableIdentity(saved.versionId),
    code: planNullableIdentity(saved.code),
    name: planIdentity(saved.name),
    children: storedItems(saved.children),
  };
}

function storedItem(value: unknown): ImportedItem {
  const saved = planObject(value);
  if (saved.kind === "module") return storedModule(saved);
  if (saved.kind !== "course" || typeof saved.completed !== "boolean")
    throw new Error(INVALID_PLAN_FILE);
  return {
    kind: "course",
    groupId: planNullableIdentity(saved.groupId),
    code: planIdentity(saved.code),
    name: planIdentity(saved.name),
    completed: saved.completed,
  };
}

function storedItems(value: unknown): ImportedItem[] {
  if (!Array.isArray(value)) throw new Error(INVALID_PLAN_FILE);
  return value.map(storedItem);
}

/**
 * Validate one stored or pending hierarchy and project it onto the current plan fields.
 *
 * A duplicate non-null top-level programme identity is rejected, because a programme has one
 * top-level place, while a repeated course occurrence is a supported capture outcome. An unknown
 * field is discarded.
 */
export function validateImportedPlan(value: unknown): ImportedPlan {
  const saved = planObject(value);
  if (!Array.isArray(saved.programmes) || saved.programmes.length === 0)
    throw new Error(INVALID_PLAN_FILE);
  const programmes = saved.programmes.map(storedModule);
  const identities = programmes.flatMap((programme) =>
    programme.groupId === null ? [] : [programme.groupId],
  );
  if (new Set(identities).size !== identities.length)
    throw new Error(INVALID_PLAN_FILE);
  return { programmes };
}

/**
 * Build the profile one plan imports into: every resolved course is selected, a course captured as
 * completed is completed, the top-level programmes keep their file order, and every resolved module
 * version is recorded before any structure read. The hierarchy waits in `pendingPlanImport` until
 * Structure's first read places it.
 */
export function prepareImportedProfile(
  plan: ImportedPlan,
  name: string,
  language: DisplayLanguage,
): Profile {
  const base = initialProfileState();
  const selected: string[] = [];
  const completed = new Set<string>();
  const moduleVersions: Record<string, string> = {};
  const collect = (items: readonly ImportedItem[]) => {
    for (const item of items) {
      if (item.kind === "module") {
        if (item.groupId !== null && item.versionId !== null) {
          const stored = moduleVersions[item.groupId];
          if (stored !== undefined && stored !== item.versionId)
            throw new Error("The plan selects two versions of a module");
          moduleVersions[item.groupId] = item.versionId;
        }
        collect(item.children);
        continue;
      }
      if (item.groupId === null) continue;
      if (!selected.includes(item.groupId)) selected.push(item.groupId);
      if (item.completed) completed.add(item.groupId);
    }
  };
  collect(plan.programmes);
  return {
    name,
    state: {
      ...base,
      language,
      selectedCourseIds: selected,
      completedCourseIds: selected.filter((id) => completed.has(id)),
      structure: {
        ...base.structure,
        programmeGroupIds: plan.programmes.flatMap((programme) =>
          programme.groupId === null ? [] : [programme.groupId],
        ),
        moduleVersions,
      },
      pendingPlanImport: structuredClone({ programmes: plan.programmes }),
    },
  };
}

/**
 * Return whether one profile holds a waiting plan beside saved structure edits.
 *
 * A saved choice or place and a waiting plan are both profile data, so the plan cannot be placed
 * without discarding the other. A choice entry counts even when its list is empty, and a placement
 * counts even when its target is `null`, because both are saved edits. Programme identities,
 * version selections and the shared course sets alone do not count, because a prepared plan fills
 * them without an edit.
 */
export function pendingImportBlocked(
  state: Pick<ProfileState, "pendingPlanImport" | "structure">,
): boolean {
  if (state.pendingPlanImport === null) return false;
  return (
    Object.keys(state.structure.choices).length > 0 ||
    Object.keys(state.structure.coursePlacements).length > 0 ||
    Object.keys(state.structure.modulePlacements).length > 0 ||
    state.structure.placedCourseIds.length > 0 ||
    state.structure.courseOrder.length > 0
  );
}

/** Return every resolved module identity of one plan in plan order, for the pending reads. */
export function pendingImportModuleIds(plan: ImportedPlan): string[] {
  const ids: string[] = [];
  const visit = (items: readonly ImportedItem[]) => {
    for (const item of items) {
      if (item.kind !== "module") continue;
      if (item.groupId !== null && !ids.includes(item.groupId))
        ids.push(item.groupId);
      visit(item.children);
    }
  };
  visit(plan.programmes);
  return ids;
}
