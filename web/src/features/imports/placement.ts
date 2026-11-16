/**
 * Placement of one resolved study plan into the profile's structure choices.
 *
 * The resolve step matches every identity it can at import time. This step keeps its match for a
 * null-identity module under a scope the version choice had not unlocked, by exact code and then
 * exact official name, and it never re-matches an identity the resolve step already set. It reads
 * the supplied state and content and writes nothing.
 */
import {
  buildProgrammeForest,
  buildStructure,
  nextChoices,
  officialHomes,
  programmeKey,
  resolveStructure,
  type PlacedContent,
  type StructureNode,
  type StructureRuleOrigin,
} from "../structure/model";
import { placeCourse, placeModule } from "../structure/edits";
import { UNPLACED_KEY, type StructureEdits } from "../structure/state";
import type { StructureChange } from "../structure/actions";
import type { ProfileState } from "../../state/types";
import {
  pendingImportBlocked,
  pendingImportModuleIds,
  type ImportIssue,
  type ImportedItem,
} from "./plan";
import { sameOfficialName } from "./names";

const AMBIGUOUS_CHOICE = "The plan choice is ambiguous";

type Scope = {
  courses: Map<string, StructureRuleOrigin>;
  modules: Map<string, { origin: StructureRuleOrigin; node: StructureNode }>;
  ambiguous: Set<string>;
};

/**
 * Collect the official identities one module's own rule offers, through its transparent groups and
 * every option of its choice nodes, and stop at the boundary of a nested module.
 *
 * An identity that appears twice in that scope is recorded as ambiguous, so a caller fails instead
 * of choosing one of the two branches.
 */
function scopeOf(node: StructureNode): Scope {
  const scope: Scope = {
    courses: new Map(),
    modules: new Map(),
    ambiguous: new Set(),
  };
  const visit = (current: StructureNode) => {
    const entries =
      current.kind === "choice"
        ? current.options.flatMap((option) => option.entries)
        : current.entries;
    for (const entry of entries) {
      if (entry.kind === "course") {
        const id = entry.course.group_id;
        if (!entry.origin) continue;
        if (scope.courses.has(id)) scope.ambiguous.add(id);
        else scope.courses.set(id, entry.origin);
        continue;
      }
      if (entry.kind === "node") {
        const module = entry.node.module;
        // A group or choice entry carries no origin of its own. Only its leaves do.
        if (!module) {
          visit(entry.node);
          continue;
        }
        if (!entry.origin) continue;
        if (scope.modules.has(module.group_id))
          scope.ambiguous.add(module.group_id);
        else
          scope.modules.set(module.group_id, {
            origin: entry.origin,
            node: entry.node,
          });
      }
    }
  };
  visit(node);
  return scope;
}

/**
 * Resolve the pending plan into one structure change and its named omissions.
 *
 * Returns null while a pending import exists whose official content has not settled: every
 * resolved captured module identity needs a report or a recorded unavailable module, and every
 * selected identity needs a summary or a recorded unavailable course. It returns null as well
 * while the profile holds saved edits beside the waiting plan, so placement never rewrites a
 * restored profile. A captured module that the parent's rule does not name joins that parent as
 * an added module of its own report, and a module with no report is named and its courses are
 * kept loose. A null-identity module under a scope the version choice has not unlocked is matched
 * by its exact code and then its exact official name, and the step never re-matches an identity
 * the resolve step set. The change clears the pending record and removes exactly the captured
 * identities that no catalogue course represents.
 */
export function placePendingImport(
  state: ProfileState,
  content: PlacedContent,
  unavailable: { courseIds: readonly string[]; moduleIds: readonly string[] },
): { change: StructureChange; issues: ImportIssue[] } | null {
  const plan = state.pendingPlanImport;
  if (!plan) return null;
  if (pendingImportBlocked(state)) return null;
  const unavailableModules = new Set(unavailable.moduleIds);
  const unavailableCourses = new Set(unavailable.courseIds);
  for (const id of pendingImportModuleIds(plan))
    if (!content.reports[id] && !unavailableModules.has(id)) return null;
  for (const id of state.selectedCourseIds)
    if (!content.courses.has(id) && !unavailableCourses.has(id)) return null;

  const language = state.language;
  const issues: ImportIssue[] = [];
  const removed = new Set<string>();
  const available = plan.programmes.filter(
    (programme) =>
      programme.groupId !== null && content.reports[programme.groupId],
  );
  const forest = buildProgrammeForest(
    available.map((programme) => content.reports[programme.groupId!]),
    language,
  );
  const forestNodes = new Map<string, StructureNode>();
  for (const entry of forest.entries)
    if (entry.kind === "node") forestNodes.set(entry.node.key, entry.node);
  let edits: StructureEdits = {
    choices: {},
    coursePlacements: {},
    modulePlacements: {},
  };
  const homesNow = () => officialHomes(forest, edits, content);
  let homes = homesNow();
  const issue = (
    item: ImportedItem,
    reason: ImportIssue["reason"],
    candidates?: ImportIssue["candidates"],
  ) => {
    issues.push({
      name: item.name,
      code: item.code,
      reason,
      ...(candidates?.length ? { candidates } : {}),
    });
  };
  /**
   * Select one matched entry's choice steps. A step that is already selected is left as it is, so a
   * repeated hit does not toggle a choice off, and a single-choice node that already holds another
   * option fails instead of being overwritten.
   */
  const choosePath = (path: StructureRuleOrigin["choicePath"]) => {
    for (const step of path) {
      const current = edits.choices[step.node.key] ?? [];
      if (current.includes(step.optionKey)) continue;
      if (step.node.single && current.length > 0)
        throw new Error(AMBIGUOUS_CHOICE);
      edits = {
        ...edits,
        choices: nextChoices(edits.choices, step.node, step.optionKey),
      };
    }
  };
  // Every representable descendant course of an omitted module keeps its completion loose.
  const keepLoose = (items: readonly ImportedItem[]) => {
    for (const item of items) {
      if (item.kind === "module") {
        if (item.groupId === null) issue(item, "unresolved");
        keepLoose(item.children);
        continue;
      }
      if (item.groupId === null) {
        issue(item, "unresolved");
        continue;
      }
      if (!content.courses.has(item.groupId)) {
        issue(item, "course_not_represented");
        removed.add(item.groupId);
        continue;
      }
      edits = placeCourse(edits, homes, item.groupId, UNPLACED_KEY);
    }
  };
  const walk = (parent: StructureNode, items: readonly ImportedItem[]) => {
    // A placement made above changes the official homes below it, so each scope reads them again.
    homes = homesNow();
    const scope = scopeOf(parent);
    const capturedCourses = new Set<string>();
    const capturedModules = new Set<string>();
    for (const item of items) {
      if (item.kind === "course") {
        if (item.groupId === null) {
          issue(item, "unresolved");
          continue;
        }
        if (!content.courses.has(item.groupId)) {
          issue(item, "course_not_represented");
          removed.add(item.groupId);
          continue;
        }
        capturedCourses.add(item.groupId);
        if (scope.ambiguous.has(item.groupId))
          throw new Error(AMBIGUOUS_CHOICE);
        const match = scope.courses.get(item.groupId);
        if (!match) {
          edits = placeCourse(edits, homes, item.groupId, parent.key);
          continue;
        }
        choosePath(match.choicePath);
        edits = placeCourse(edits, homes, item.groupId, match.ownerKey);
        continue;
      }
      if (item.groupId === null) {
        // Every scope entry that carries an official module summary, collected once for the code
        // match, the name fallback and the candidate details an issue names.
        const named = [...scope.modules.entries()].flatMap(
          ([groupId, entry]) =>
            entry.node.module === null
              ? []
              : [{ groupId, entry, module: entry.node.module }],
        );
        const byCode =
          item.code === null
            ? []
            : named.filter(({ module }) => module.code === item.code);
        const matches = byCode.length
          ? byCode
          : named.filter(({ module }) => sameOfficialName(module, item.name));
        if (matches.length === 1 && !scope.ambiguous.has(matches[0].groupId)) {
          const [{ groupId, entry }] = matches;
          choosePath(entry.origin.choicePath);
          edits = placeModule(edits, homes, groupId, entry.origin.ownerKey);
          walk(entry.node, item.children);
          continue;
        }
        issue(
          item,
          matches.length === 0
            ? "no_official_match"
            : "ambiguous_official_match",
          matches.map(({ groupId, module }) => ({
            groupId,
            code: module.code,
            name: module.name_fi ?? module.name_en ?? "",
          })),
        );
        keepLoose(item.children);
        continue;
      }
      if (
        capturedModules.has(item.groupId) ||
        scope.ambiguous.has(item.groupId)
      )
        throw new Error(AMBIGUOUS_CHOICE);
      capturedModules.add(item.groupId);
      const match = scope.modules.get(item.groupId);
      if (match) {
        choosePath(match.origin.choicePath);
        edits = placeModule(edits, homes, item.groupId, match.origin.ownerKey);
        walk(match.node, item.children);
        continue;
      }
      const report = content.reports[item.groupId];
      if (!report) {
        issue(item, "module_not_placed");
        keepLoose(item.children);
        continue;
      }
      // The parent's rule does not name this module, so the captured module joins that parent as an
      // added module of its own official report, and its captured children go inside it.
      edits = placeModule(edits, homes, item.groupId, parent.key);
      walk(
        buildStructure(report, `${parent.key}/m:${item.groupId}`, language),
        item.children,
      );
    }
    // An official identity this scope does not capture is suppressed, so the official default
    // cannot silently select content the fully expanded capture does not hold. An identity a
    // capture already placed elsewhere keeps that place.
    for (const id of scope.courses.keys())
      if (!capturedCourses.has(id) && edits.coursePlacements[id] === undefined)
        edits = placeCourse(edits, homes, id, null);
    for (const id of scope.modules.keys())
      if (!capturedModules.has(id) && edits.modulePlacements[id] === undefined)
        edits = placeModule(edits, homes, id, null);
  };
  for (const programme of plan.programmes) {
    const id = programme.groupId;
    if (id !== null && content.reports[id]) continue;
    issue(programme, id === null ? "unresolved" : "module_not_placed");
    keepLoose(programme.children);
  }
  for (const programme of available) {
    const node = forestNodes.get(programmeKey(programme.groupId!));
    if (!node) {
      issue(programme, "module_not_placed");
      keepLoose(programme.children);
      continue;
    }
    walk(node, programme.children);
  }
  const projection = resolveStructure(forest, edits, content, language);
  return {
    change: {
      next: {
        programmeGroupIds: available.map((programme) => programme.groupId!),
        ...edits,
        moduleVersions: { ...state.structure.moduleVersions },
        placedCourseIds: projection.subtree.map((course) => course.group_id),
        courseOrder: [],
      },
      removedCourseIds: [...removed],
      pendingPlanImport: null,
    },
    issues,
  };
}
