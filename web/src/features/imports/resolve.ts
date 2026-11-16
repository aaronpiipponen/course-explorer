/**
 * The one catalogue and structure matcher between an extracted study plan and the served official
 * content.
 *
 * One catalogue read supplies exact course codes, exact module codes and names, eligible module
 * versions and the version-to-group identity. Structure reports are read in batches through the
 * ordinary cached owner, and only canonical known identities reach the network. A captured course
 * resolves by exact code. A module resolves by its exact version identity, then by exact code and
 * exact Finnish-or-English name inside its resolved parent's direct-child scope. Only a top-level
 * module's code is matched against the whole catalogue. Unmatched items stay in the plan as
 * null-identity placeholders with transient issues, so the placement owner decides their outcome.
 * The step writes nothing.
 */
import {
  acceptOperationIdentity,
  cancellationError,
} from "../../app/cachedRead";
import type { ReadCache } from "../../app/readCache";
import type {
  CatalogueModule,
  CatalogueReport,
  StudyStructureReport,
  StructureModule,
  StructureRule,
} from "../../api/types";
import { readCachedCatalogue } from "../../shared/course/catalogue";
import { readCachedStudyStructures } from "../structure/structureReads";
import {
  type ImportIssue,
  type ImportIssueCandidate,
  type ExtractedItem,
  type ExtractedModule,
  type ExtractedPlan,
  type ImportedItem,
  type ImportedModule,
  type ImportedPlan,
  type ExtractedCourse,
} from "./plan";
import { sameOfficialName } from "./names";

const DEGREE_PROGRAMME = "DegreeProgramme";
const INVALID_PLAN_FILE = "Invalid plan file";

/** One exact module candidate of the served catalogue. */
type CatalogueModuleCandidate = CatalogueModule & { group_id: string };

/** Exact catalogue lookups built once from the one catalogue read. */
type CatalogueIndex = {
  coursesByCode: Map<string, Set<string>>;
  modulesByCode: Map<string, Set<string>>;
  modulesByGroup: Map<string, readonly CatalogueModuleCandidate[]>;
  modulesByVersion: Map<string, string>;
};

/** One walk's plan, issues, newly needed report roots and whether it recorded a sole selection. */
type WalkResult = {
  plan: ImportedPlan;
  issues: ImportIssue[];
  neededRoots: string[];
  recordedSelection: boolean;
};

/** The loaded official content one walk reads. */
type WalkContext = {
  index: CatalogueIndex;
  catalogue: CatalogueReport;
  tree: ReadonlyMap<string, StructureModule>;
  selections: Record<string, string>;
  unavailable: ReadonlySet<string>;
  selectionRequired: ReadonlySet<string>;
};

/** Return the display name of one official module summary. */
function moduleName(module: {
  name_fi: string | null;
  name_en: string | null;
}): string {
  return module.name_fi ?? module.name_en ?? "";
}

/** Add one value to the set a map holds under one exact key. */
function addTo<K>(map: Map<K, Set<string>>, key: K, value: string): void {
  const held = map.get(key);
  if (held) held.add(value);
  else map.set(key, new Set([value]));
}

/** Build the exact catalogue lookups, the canonical group candidates and the version identities. */
function catalogueIndex(report: CatalogueReport): CatalogueIndex {
  const coursesByCode = new Map<string, Set<string>>();
  for (const course of report.courses)
    addTo(coursesByCode, course.code, course.group_id);
  const modulesByCode = new Map<string, Set<string>>();
  const modulesByGroup = new Map<string, readonly CatalogueModuleCandidate[]>();
  const modulesByVersion = new Map<string, string>();
  for (const group of report.modules) {
    const candidates = group.candidates.map((candidate) => ({
      ...candidate,
      group_id: group.group_id,
    }));
    modulesByGroup.set(group.group_id, candidates);
    for (const candidate of candidates) {
      if (candidate.code !== null)
        addTo(modulesByCode, candidate.code, group.group_id);
      modulesByVersion.set(candidate.source_version_id, group.group_id);
    }
  }
  return { coursesByCode, modulesByCode, modulesByGroup, modulesByVersion };
}

/**
 * Resolve one module group's sole eligible version and record it for the reads.
 *
 * `recorded` reports whether this call recorded a selection that was absent before, which is the
 * only signal a second batch needs.
 */
function soleVersion(
  index: CatalogueIndex,
  groupId: string,
  selections: Record<string, string>,
): { versionId: string | null; recorded: boolean } {
  const versions = new Set(
    (index.modulesByGroup.get(groupId) ?? []).map(
      (candidate) => candidate.source_version_id,
    ),
  );
  const [only] = [...versions];
  const versionId = versions.size === 1 && only !== undefined ? only : null;
  const recorded = versionId !== null && selections[groupId] === undefined;
  if (recorded && versionId !== null) selections[groupId] = versionId;
  return { versionId, recorded };
}

/** Return whether one group has several eligible versions. */
function severalVersions(index: CatalogueIndex, groupId: string): boolean {
  return (
    new Set(
      (index.modulesByGroup.get(groupId) ?? []).map(
        (candidate) => candidate.source_version_id,
      ),
    ).size > 1
  );
}

/** Return the transient candidate details of one module group. */
function moduleCandidate(
  index: CatalogueIndex,
  groupId: string,
): ImportIssueCandidate {
  const [first] = index.modulesByGroup.get(groupId) ?? [];
  return {
    groupId,
    code: first?.code ?? null,
    name: first ? moduleName(first) : "",
  };
}

/** Return the transient candidate details of one course group. */
function courseCandidate(
  report: CatalogueReport,
  groupId: string,
): ImportIssueCandidate {
  const found = report.courses.find((item) => item.group_id === groupId);
  return {
    groupId,
    code: found?.code ?? null,
    name: found ? (found.name_fi ?? found.name_en ?? found.code) : "",
  };
}

/** Index every module node of the loaded reports by its canonical group identity. */
function moduleNodes(
  reports: Readonly<Record<string, StudyStructureReport>>,
): Map<string, StructureModule> {
  const nodes = new Map<string, StructureModule>();
  const visit = (module: StructureModule) => {
    nodes.set(module.module.group_id, module);
    const walk = (rule: StructureRule) => {
      if (rule.module) visit(rule.module);
      for (const child of rule.children) walk(child);
    };
    walk(module.rule);
  };
  for (const report of Object.values(reports)) visit(report.root);
  return nodes;
}

/**
 * Return one module's direct official module children.
 *
 * Transparent composite and choice rules are flattened and the walk stops at a nested module,
 * matching the placement owner's scope traversal rather than every same-named descendant.
 */
function directModuleChildren(
  module: StructureModule,
): readonly StructureModule["module"][] {
  const children = new Map<string, StructureModule["module"]>();
  const walk = (rule: StructureRule) => {
    if (rule.module) {
      children.set(rule.module.module.group_id, rule.module.module);
      return;
    }
    for (const child of rule.children) walk(child);
  };
  walk(module.rule);
  return [...children.values()];
}

/** Return whether one catalogue group is an eligible degree programme. */
function isDegreeGroup(index: CatalogueIndex, groupId: string): boolean {
  return (index.modulesByGroup.get(groupId) ?? []).some(
    (candidate) => candidate.module_type === DEGREE_PROGRAMME,
  );
}

/** Return the unique degree group of one code, when it names exactly one. */
function degreeGroup(index: CatalogueIndex, code: string): string | null {
  const groups = [...(index.modulesByCode.get(code) ?? [])].filter((groupId) =>
    isDegreeGroup(index, groupId),
  );
  return groups.length === 1 ? groups[0] : null;
}

/** Return the group one top-level extracted module resolves to, when it names exactly one. */
function topGroup(
  index: CatalogueIndex,
  module: ExtractedModule,
): string | null {
  if (module.sourceVersionId !== null) {
    const group = index.modulesByVersion.get(module.sourceVersionId);
    if (group !== undefined) return group;
  }
  if (module.code === null) return null;
  const degree = degreeGroup(index, module.code);
  if (degree !== null) return degree;
  const groups = [...(index.modulesByCode.get(module.code) ?? [])];
  return groups.length === 1 ? groups[0] : null;
}

/** One top-level extracted module and the programme group it resolves to, when it resolves. */
type TopEntry = {
  module: ExtractedModule;
  groupId: string | null;
  ambiguous: ImportIssueCandidate[];
};

/**
 * Collect the top-level extracted modules, promoting the children of a top-level container that
 * carries no code. An unresolved top-level module keeps its place as a null-identity programme so
 * its omission is named instead of dropped.
 */
function programmeEntries(
  plan: ExtractedPlan,
  index: CatalogueIndex,
): TopEntry[] {
  const entries: TopEntry[] = [];
  const add = (module: ExtractedModule) => {
    if (module.code === null) {
      for (const child of module.children)
        if (child.kind === "module") add(child);
      return;
    }
    const groupId = topGroup(index, module);
    if (groupId !== null) {
      entries.push({ module, groupId, ambiguous: [] });
      return;
    }
    const groups = [...(index.modulesByCode.get(module.code) ?? [])];
    entries.push({
      module,
      groupId: null,
      ambiguous:
        groups.length > 1
          ? groups.map((group) => moduleCandidate(index, group))
          : [],
    });
  };
  for (const module of plan.programmes) add(module);
  return entries;
}

/** Visit every extracted module of a plan in document order. */
function everyModule(
  items: readonly ExtractedItem[],
  visit: (module: ExtractedModule) => void,
): void {
  for (const item of items) {
    if (item.kind !== "module") continue;
    visit(item);
    everyModule(item.children, visit);
  }
}

/** A module match's distinct scoped pairs and the candidates an ambiguity names. */
type ScopedMatch = {
  groups: string[];
  candidates: ImportIssueCandidate[];
};

/** Match one nested module among the direct module children of its resolved parent scope. */
function scopedModuleMatch(
  module: ExtractedModule,
  parent: StructureModule | undefined,
): ScopedMatch {
  if (!parent) return { groups: [], candidates: [] };
  const children = directModuleChildren(parent);
  const byCode =
    module.code === null
      ? []
      : children.filter((child) => child.code === module.code);
  const matched = byCode.length
    ? byCode
    : children.filter((child) => sameOfficialName(child, module.name));
  const groups: string[] = [];
  const candidates: ImportIssueCandidate[] = [];
  for (const child of matched) {
    if (!groups.includes(child.group_id)) groups.push(child.group_id);
    candidates.push({
      groupId: child.group_id,
      code: child.code,
      name: moduleName(child),
    });
  }
  return { groups, candidates };
}

/** Build one resolved module node. */
function moduleNode(
  module: ExtractedModule,
  groupId: string,
  versionId: string | null,
): ImportedModule {
  return {
    kind: "module",
    groupId,
    versionId,
    code: module.code,
    name: module.name,
    children: [],
  };
}

/** Build one null-identity module placeholder with the extracted code and name. */
function placeholderModule(module: ExtractedModule): ImportedModule {
  return {
    kind: "module",
    groupId: null,
    versionId: null,
    code: module.code,
    name: module.name,
    children: [],
  };
}

/** Build one resolved or null-identity course node and record its issue when not unique. */
function courseNode(
  course: ExtractedCourse,
  context: WalkContext,
): { node: ImportedItem; issue: ImportIssue | null } {
  const groups = [...(context.index.coursesByCode.get(course.code) ?? [])];
  if (groups.length === 1) {
    return {
      node: { ...course, groupId: groups[0] },
      issue: null,
    };
  }
  return {
    node: { ...course, groupId: null },
    issue: {
      name: course.name,
      code: course.code,
      reason:
        groups.length === 0 ? "no_official_match" : "ambiguous_official_match",
      candidates:
        groups.length > 1
          ? groups.map((groupId) => courseCandidate(context.catalogue, groupId))
          : undefined,
    },
  };
}

/** Walk one plan with the loaded official tree and build the resolved plan and its issues. */
function walkPlan(
  entries: readonly TopEntry[],
  context: WalkContext,
): WalkResult {
  const result: ImportedPlan = { programmes: [] };
  const issues: ImportIssue[] = [];
  const neededRoots = new Set<string>();
  let recordedSelection = false;

  const versionFor = (groupId: string): string | null => {
    const recorded = context.selections[groupId];
    if (recorded !== undefined) return recorded;
    const { versionId, recorded: changed } = soleVersion(
      context.index,
      groupId,
      context.selections,
    );
    recordedSelection ||= changed;
    return versionId;
  };

  const issue = (
    item: ExtractedItem,
    reason: ImportIssue["reason"],
    candidates?: readonly ImportIssueCandidate[],
  ) => {
    issues.push({
      name: item.name,
      code: item.code,
      reason,
      ...(candidates?.length ? { candidates } : {}),
    });
  };

  const walkChildren = (
    node: ImportedModule,
    items: readonly ExtractedItem[],
    parentGroupId: string | null,
  ) => {
    const parent =
      parentGroupId === null ? undefined : context.tree.get(parentGroupId);
    const pending =
      parentGroupId !== null &&
      parent === undefined &&
      !context.unavailable.has(parentGroupId) &&
      (severalVersions(context.index, parentGroupId) ||
        context.selectionRequired.has(parentGroupId));
    for (const item of items) {
      if (item.kind === "course") {
        const resolved = courseNode(item, context);
        node.children.push(resolved.node);
        if (resolved.issue) issues.push(resolved.issue);
        continue;
      }
      const versionGroup =
        item.sourceVersionId === null
          ? undefined
          : context.index.modulesByVersion.get(item.sourceVersionId);
      let groupId: string | null = versionGroup ?? null;
      let ambiguous: ImportIssueCandidate[] = [];
      if (groupId === null) {
        const scoped = scopedModuleMatch(item, parent);
        if (scoped.groups.length === 1) groupId = scoped.groups[0];
        else if (scoped.groups.length > 1) ambiguous = scoped.candidates;
      }
      if (groupId !== null) {
        const versionId =
          versionGroup !== undefined
            ? item.sourceVersionId
            : versionFor(groupId);
        const resolved = moduleNode(item, groupId, versionId);
        node.children.push(resolved);
        if (!context.tree.has(groupId) && versionId !== null)
          neededRoots.add(groupId);
        walkChildren(resolved, item.children, groupId);
        continue;
      }
      const placeholder = placeholderModule(item);
      node.children.push(placeholder);
      issue(
        item,
        pending
          ? "version_choice_pending"
          : ambiguous.length
            ? "ambiguous_official_match"
            : "no_official_match",
        ambiguous,
      );
      walkChildren(placeholder, item.children, null);
    }
  };

  for (const entry of entries) {
    const { module, groupId, ambiguous } = entry;
    if (groupId === null) {
      const node = placeholderModule(module);
      result.programmes.push(node);
      issue(
        module,
        ambiguous.length ? "ambiguous_official_match" : "no_official_match",
        ambiguous,
      );
      walkChildren(node, module.children, null);
      continue;
    }
    const versionId = versionFor(groupId);
    const node = moduleNode(module, groupId, versionId);
    result.programmes.push(node);
    if (!context.tree.has(groupId) && versionId !== null)
      neededRoots.add(groupId);
    walkChildren(node, module.children, groupId);
  }
  return {
    plan: result,
    issues,
    neededRoots: [...neededRoots],
    recordedSelection,
  };
}

/** Read one batch of canonical roots and accept its dataset identity. */
async function readBatch(
  cache: ReadCache,
  roots: readonly string[],
  selections: Readonly<Record<string, string>>,
  operationIdentity: string,
  signal: AbortSignal,
): Promise<{
  reports: Readonly<Record<string, StudyStructureReport>>;
  unavailable: ReadonlySet<string>;
  selectionRequired: ReadonlySet<string>;
}> {
  if (!roots.length)
    return {
      reports: {},
      unavailable: new Set(),
      selectionRequired: new Set(),
    };
  const response = await readCachedStudyStructures(
    cache,
    roots,
    selections,
    true,
    signal,
  );
  acceptOperationIdentity(cache, operationIdentity, response.dataset_identity);
  if (signal.aborted) throw cancellationError(signal);
  return {
    reports: response.reports,
    unavailable: new Set(response.unavailable_root_group_ids),
    selectionRequired: new Set(response.version_selection_required),
  };
}

/**
 * Resolve one extracted plan against the served catalogue and official structures.
 *
 * One catalogue read fixes the operation identity. The programme roots, every version-resolved
 * module group and the sole group of a top-level code are read first, then one additional batch
 * covers newly resolved roots and a sole selection the walk recorded. Every batch read is checked
 * against the operation identity, and a mismatch fails with the ordinary dataset-changed error. A
 * plan with no resolved programme fails with `Invalid plan file`.
 */
export async function resolveExtractedPlan(
  plan: ExtractedPlan,
  cache: ReadCache,
  signal: AbortSignal,
): Promise<{ plan: ImportedPlan; issues: readonly ImportIssue[] }> {
  const catalogue = await readCachedCatalogue(cache);
  const operationIdentity = catalogue.dataset_identity;
  if (signal.aborted) throw cancellationError(signal);
  const index = catalogueIndex(catalogue);
  const entries = programmeEntries(plan, index);
  if (!entries.some((entry) => entry.groupId !== null))
    throw new Error(INVALID_PLAN_FILE);

  const selections: Record<string, string> = {};
  const firstRoots = new Set<string>();
  for (const entry of entries)
    if (entry.groupId !== null) firstRoots.add(entry.groupId);
  // Every version identity records its selection. Only a top-level module's code is matched
  // against the whole catalogue, so only a top-level code records a sole version or joins the
  // first batch.
  everyModule(plan.programmes, (module) => {
    if (module.sourceVersionId === null) return;
    const groupId = index.modulesByVersion.get(module.sourceVersionId);
    if (groupId === undefined) return;
    if (selections[groupId] === undefined)
      selections[groupId] = module.sourceVersionId;
    firstRoots.add(groupId);
  });
  for (const entry of entries) {
    if (entry.module.code === null) continue;
    const groups =
      index.modulesByCode.get(entry.module.code) ?? new Set<string>();
    let onlyGroup: string | null = null;
    for (const groupId of groups) {
      soleVersion(index, groupId, selections);
      onlyGroup = groupId;
    }
    if (groups.size === 1 && onlyGroup !== null) firstRoots.add(onlyGroup);
  }

  const first = await readBatch(
    cache,
    [...firstRoots],
    selections,
    operationIdentity,
    signal,
  );
  let reports = first.reports;
  const unavailable = new Set(first.unavailable);
  const selectionRequired = new Set(first.selectionRequired);
  const context: WalkContext = {
    index,
    catalogue,
    tree: moduleNodes(reports),
    selections,
    unavailable,
    selectionRequired,
  };
  let result = walkPlan(entries, context);
  if (result.neededRoots.length || result.recordedSelection) {
    const second = await readBatch(
      cache,
      [...new Set([...firstRoots, ...result.neededRoots])],
      selections,
      operationIdentity,
      signal,
    );
    reports = { ...reports, ...second.reports };
    for (const id of second.unavailable) unavailable.add(id);
    for (const id of second.selectionRequired) selectionRequired.add(id);
    result = walkPlan(entries, {
      ...context,
      tree: moduleNodes(reports),
      unavailable,
      selectionRequired,
    });
  }

  return { plan: result.plan, issues: result.issues };
}
