/** Study-plan import: the Sisu and Peppi readers, the import profile, scoped placement, named
    omissions and the PDF composition and resolution. */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  CourseSummary,
  ModuleSummary,
  StructureModule,
  StructureRule,
  StudyStructureReport,
} from "../../api/types";
import {
  createBrowserStore,
  parseBrowserImport,
  BROWSER_STORAGE_KEY,
} from "../../state/store";
import type { BrowserState } from "../../state/types";
import { applyStructure } from "../structure/actions";
import { READ_CACHE_CAPACITY, ReadCache } from "../../app/readCache";
import {
  buildProgrammeForest,
  indexResolved,
  programmeKey,
  resolveStructure,
  type PlacedContent,
} from "../structure/model";
import {
  prepareImportedProfile,
  type ImportedModule,
  type ImportedPlan,
} from "./plan";
import { placePendingImport } from "./placement";
import { resolveExtractedPlan } from "./resolve";
import { readSisuCapture, readSisuPage } from "./sisu";
import { readPeppiCapture, readPeppiPage } from "./peppi";
import { formatsForStudyPlan, scriptForStudyPlan } from "./formats";
import {
  extractSisuPdfRows,
  type SisuPdfTextItem,
  type SisuPdfTextPage,
} from "./pdfRows";
import { extractedPlanFromRows } from "./pdfPlan";

vi.mock("../structure/structureStorage", () => ({
  readPersistedStructures: vi.fn(async () => ({})),
  writePersistedStructures: vi.fn(async () => undefined),
}));

const baseRule = (patch: Partial<StructureRule>): StructureRule => ({
  node_type: "CompositeRule",
  sibling_position: 0,
  require_min: null,
  require_max: null,
  all_mandatory: null,
  credits_min: null,
  credits_max: null,
  count_min: null,
  count_max: null,
  referenced_group_id: null,
  children: [],
  course: null,
  module: null,
  unresolved: null,
  ...patch,
});
const courseSummary = (id: string, code: string): CourseSummary => ({
  group_id: id,
  name_fi: null,
  name_en: `${code} course`,
  code,
  credits_min: "5",
  credits_max: null,
  is_active: true,
  official_url: `https://example.org/${id}`,
});
const moduleSummary = (
  id: string,
  code: string,
  moduleType = "Module",
): ModuleSummary => ({
  group_id: id,
  source_version_id: `sisu-${id}`,
  module_type: moduleType,
  code,
  name_fi: null,
  name_en: `${code} module`,
});
const structureModule = (
  summary: ModuleSummary,
  rule: StructureRule,
): StructureModule => ({
  module: summary,
  target_credits_min: null,
  target_credits_max: null,
  rule,
});
const report = (root: StructureModule): StudyStructureReport => ({
  academic_year_start: 2026,
  root,
});
const courseChild = (
  id: string,
  code: string,
  position: number,
): StructureRule =>
  baseRule({
    node_type: "CourseUnitRule",
    sibling_position: position,
    course: courseSummary(id, code),
  });
const moduleChild = (node: StructureModule, position: number): StructureRule =>
  baseRule({
    node_type: "ModuleRule",
    sibling_position: position,
    module: node,
  });
const mandatory = (children: StructureRule[]): StructureRule =>
  baseRule({ all_mandatory: true, children });

const fileOf = (text: string, name: string, type: string): File => {
  const file = new File([text], name, { type });
  Object.defineProperty(file, "text", {
    configurable: true,
    value: async () => text,
  });
  return file;
};

type MarkupModule = {
  name: string;
  status: string;
  code?: string;
  graduate?: boolean;
  grouping?: boolean;
  info?: boolean;
  children?: readonly string[];
};
const planModule = (options: MarkupModule): string => `<plan-module>
  <div class="header has-header-items">
    <button type="button" class="collapse non-button" aria-expanded="true">-</button>
    <div class="title clickable${options.grouping ? " grouping-module" : ""}">
      <span data-cy="plan-module-name">${options.name}</span>
      ${options.code ? `<span ng-bind=":: $ctrl.module.code">${options.code}</span>` : ""}
    </div>
    ${options.info ? `<button id="module-info-modal-open-${options.code}">i</button>` : ""}
    ${options.graduate ? `<div class="cell graduate">graduation</div>` : ""}
    <div class="cell status status-text"><span id="${options.status}">state</span></div>
  </div>
  ${(options.children ?? []).join("")}
</plan-module>`;
const courseBox = (code: string, name: string, completed: boolean): string =>
  `<course-unit-box>
  <div class="courseunit-top-row">
    <div class="title-box"><span class="courseunit-code">${code}</span> ${name}</div>
  </div>
  ${completed ? "<app-course-unit-attainment-notification></app-course-unit-attainment-notification>" : ""}
</course-unit-box>`;

const currentState = (store: ReturnType<typeof createBrowserStore>) => {
  const snapshot = store.getSnapshot();
  return snapshot.profiles[snapshot.currentProfileId].state;
};
const contentOf = (
  reports: StudyStructureReport[],
  extra: CourseSummary[] = [],
): PlacedContent => {
  const courses = new Map<string, CourseSummary>();
  const collect = (rule: StructureRule) => {
    if (rule.course) courses.set(rule.course.group_id, rule.course);
    if (rule.module) collect(rule.module.rule);
    for (const child of rule.children) collect(child);
  };
  for (const item of reports) collect(item.root.rule);
  for (const course of extra) courses.set(course.group_id, course);
  return {
    reports: Object.fromEntries(
      reports.map((item) => [item.root.module.group_id, item]),
    ),
    courses,
  };
};

describe("study-plan readers", () => {
  beforeEach(() => localStorage.clear());
  afterEach(() => vi.unstubAllGlobals());

  it("reads each platform's capture and saved page and applies the format order and refusals", async () => {
    const markup = planModule({
      name: "Education in Computer Sciences",
      status: "otm-education-statuscell",
      code: "TIEKO",
      children: [
        planModule({
          name: "Bachelor's Programme",
          status: "otm-bachelor-statuscell",
          code: "TIEK",
          graduate: true,
          info: true,
          children: [
            courseBox("CS.100", "Programming", true),
            courseBox("CS.101", "Data structures", false),
          ],
        }),
        planModule({
          name: "Master's Programme",
          status: "otm-master-statuscell",
          code: "TIM",
          graduate: true,
          info: true,
          children: [courseBox("CS.900", "Thesis", false)],
        }),
      ],
    });
    const capture = {
      type: "course-explorer/sisu/1",
      markup,
    };
    const page = fileOf(
      `<html><body>${markup}</body></html>`,
      "rakenne.html",
      "text/html",
    );
    const captured = readSisuCapture(capture);
    const saved = await readSisuPage(page, new AbortController().signal);
    expect(captured).toEqual(saved);
    expect(captured.programmes.map((programme) => programme.name)).toEqual([
      "Bachelor's Programme",
      "Master's Programme",
    ]);
    expect(captured.programmes[0]).toMatchObject({
      code: "TIEK",
      sourceVersionId: "otm-bachelor",
    });
    expect(captured.programmes[0].children[0]).toMatchObject({
      kind: "course",
      code: "CS.100",
      completed: true,
    });

    const structure = `<div id="structure">
      <section class="panel lu-container">
        <div class="panel-heading">
          <h4 class="study-header">
            <button onclick="openLearningUnitInfoModal('STUDY_MODULE', 'MOD-1', 1)">
              <span class="lu-name">Module One</span>
            </button>
          </h4>
        </div>
        <div class="panel-body">
          <table><tbody>
            <tr class="lu-container">
              <td class="table-cell-status"><span class="status-badge status-completed"></span></td>
              <td class="table-cell-name">
                <button onclick="openLearningUnitInfoModal('COURSE_UNIT', 'C-1', 1)">
                  <span class="lu-name">Course One</span>
                </button>
              </td>
            </tr>
          </tbody></table>
        </div>
      </section>
    </div>`;
    const programme = "PROG-1, Programme One";
    const peppiCapture = {
      type: "course-explorer/peppi-hops/1",
      programme,
      markup: structure,
    };
    const peppiPage = fileOf(
      `<html><body><div id="header-row-div"><h2>${programme}</h2></div>${structure}</body></html>`,
      "tarkastelu.html",
      "text/html",
    );
    const peppiCaptured = readPeppiCapture(peppiCapture);
    const peppiSaved = await readPeppiPage(
      peppiPage,
      new AbortController().signal,
    );
    expect(peppiCaptured).toEqual(peppiSaved);
    expect(peppiCaptured.programmes[0]).toMatchObject({
      code: "PROG-1",
      name: "Programme One",
    });
    expect(peppiCaptured.programmes[0].children[0]).toMatchObject({
      kind: "module",
      code: "MOD-1",
      name: "Module One",
    });
    expect(
      (
        peppiCaptured.programmes[0].children[0] as {
          children: readonly { kind: string; completed: boolean }[];
        }
      ).children[0],
    ).toMatchObject({ kind: "course", completed: true });

    const codeOnly = "PROG-2";
    const codeOnlyCaptured = readPeppiCapture({
      type: "course-explorer/peppi-hops/1",
      programme: codeOnly,
      markup: structure,
    });
    const codeOnlySaved = await readPeppiPage(
      fileOf(
        `<html><body><div id="header-row-div"><h2>${codeOnly}</h2></div>${structure}</body></html>`,
        "tarkastelu.html",
        "text/html",
      ),
      new AbortController().signal,
    );
    expect(codeOnlyCaptured.programmes[0]).toMatchObject({
      code: codeOnly,
      name: codeOnly,
    });
    expect(codeOnlySaved.programmes[0]).toMatchObject({
      code: codeOnly,
      name: codeOnly,
    });

    expect(
      formatsForStudyPlan(["peppi_capture", "peppi_page"]).map(
        (format) => format.id,
      ),
    ).toEqual(["peppi_page", "peppi_capture"]);
    expect(
      formatsForStudyPlan(["sisu_capture", "sisu_page", "sisu_pdf"]).map(
        (format) => format.id,
      ),
    ).toEqual(["sisu_page", "sisu_pdf", "sisu_capture"]);
    expect(() => formatsForStudyPlan(["unknown"])).toThrow();
    expect(() => scriptForStudyPlan("unknown")).toThrow();
    await expect(
      readSisuPage(
        fileOf("<html><body></body></html>", "empty.html", "text/html"),
        new AbortController().signal,
      ),
    ).rejects.toThrow();
    await expect(
      readPeppiPage(
        fileOf("<html><body></body></html>", "empty.html", "text/html"),
        new AbortController().signal,
      ),
    ).rejects.toThrow();
  });
});

describe("study-plan placement", () => {
  beforeEach(() => localStorage.clear());

  it("places captured choices in their programme scopes and clears pending atomically", () => {
    const inner = structureModule(
      { ...moduleSummary("inner", "INNER"), name_en: "Inner module" },
      mandatory([courseChild("c-inner", "C-INNER", 0)]),
    );
    const moduleX = structureModule(
      moduleSummary("mod-x", "MOD-X"),
      mandatory([
        baseRule({
          require_min: 1,
          require_max: 1,
          all_mandatory: false,
          children: [
            courseChild("c-shared", "C-SHARED", 0),
            courseChild("c-opt", "C-OPT", 1),
          ],
        }),
        moduleChild(inner, 1),
      ]),
    );
    const freeSlot = structureModule(
      moduleSummary("free-slot", "FREE-SLOT"),
      baseRule({
        require_min: 0,
        children: [baseRule({ node_type: "AnyModuleRule" })],
      }),
    );
    const elective = structureModule(
      moduleSummary("elective", "ELECTIVE"),
      mandatory([
        courseChild("c-elective", "C-ELECTIVE", 0),
        courseChild("c-extra", "C-EXTRA", 1),
      ]),
    );
    const programmeA = report(
      structureModule(
        moduleSummary("prog-a", "PROG-A", "DegreeProgramme"),
        mandatory([
          moduleChild(moduleX, 0),
          moduleChild(freeSlot, 1),
          courseChild("c-main", "C-MAIN", 2),
          courseChild("c-default", "C-DEFAULT", 3),
        ]),
      ),
    );
    const programmeB = report(
      structureModule(
        moduleSummary("prog-b", "PROG-B", "DegreeProgramme"),
        mandatory([
          courseChild("c-shared", "C-SHARED", 0),
          courseChild("c-b2", "C-B2", 1),
          moduleChild(elective, 2),
        ]),
      ),
    );
    const plan: ImportedPlan = {
      programmes: [
        {
          kind: "module",
          groupId: "prog-a",
          versionId: "v-prog-a",
          code: "PROG-A",
          name: "Programme A",
          children: [
            {
              kind: "module",
              groupId: "mod-x",
              versionId: "v-mod-x",
              code: "MOD-X",
              name: "Module X",
              children: [
                {
                  kind: "course",
                  groupId: "c-shared",
                  code: "C-SHARED",
                  name: "Shared course",
                  completed: true,
                },
                {
                  kind: "module",
                  groupId: "inner",
                  versionId: "v-inner",
                  code: null,
                  name: "Inner module",
                  children: [
                    {
                      kind: "course",
                      groupId: "c-inner",
                      code: "C-INNER",
                      name: "Inner course",
                      completed: false,
                    },
                  ],
                },
              ],
            },
            {
              kind: "module",
              groupId: "free-slot",
              versionId: "v-free",
              code: "FREE-SLOT",
              name: "Free slot",
              children: [
                {
                  kind: "module",
                  groupId: "elective",
                  versionId: "v-elective",
                  code: "ELECTIVE",
                  name: "Elective",
                  children: [
                    {
                      kind: "course",
                      groupId: "c-elective",
                      code: "C-ELECTIVE",
                      name: "Elective course",
                      completed: false,
                    },
                  ],
                },
              ],
            },
            {
              kind: "course",
              groupId: "c-main",
              code: "C-MAIN",
              name: "Main course",
              completed: false,
            },
          ],
        },
        {
          kind: "module",
          groupId: "prog-b",
          versionId: "v-prog-b",
          code: "PROG-B",
          name: "Programme B",
          children: [
            {
              kind: "course",
              groupId: "c-b2",
              code: "C-B2",
              name: "Second course",
              completed: false,
            },
          ],
        },
      ],
    };
    const content = contentOf([
      programmeA,
      programmeB,
      report(moduleX),
      report(inner),
      report(freeSlot),
      report(elective),
    ]);
    const store = createBrowserStore(localStorage);
    store.importProfile(prepareImportedProfile(plan, "Sisu plan", "en"));
    applyStructure(
      store,
      (state) =>
        placePendingImport(state, content, { courseIds: [], moduleIds: [] })
          ?.change ?? null,
    );
    const reloaded = currentState(createBrowserStore(localStorage));
    expect(reloaded.pendingPlanImport).toBeNull();
    expect(reloaded.structure.programmeGroupIds).toEqual(["prog-a", "prog-b"]);
    const forest = buildProgrammeForest([programmeA, programmeB]);
    const projection = resolveStructure(forest, reloaded.structure, content);
    const rendered = indexResolved(projection);
    const shared = rendered.courseAt.get("c-shared");
    expect(shared?.node.kind).toBe("choice");
    expect(reloaded.structure.coursePlacements["c-shared"]).toBe(
      shared?.node.key,
    );
    expect(shared?.node.key.startsWith(programmeKey("prog-a"))).toBe(true);
    expect(rendered.courseAt.has("c-opt")).toBe(false);
    expect(rendered.courseAt.has("c-default")).toBe(false);
    expect(rendered.courseAt.get("c-main")?.node.key).toBe(
      programmeKey("prog-a"),
    );
    expect(rendered.courseAt.get("c-b2")?.node.key).toBe(
      programmeKey("prog-b"),
    );
    const slot = rendered.moduleAt.get("free-slot");
    const added = rendered.moduleAt.get("elective");
    expect(slot).toBeDefined();
    expect(added?.node.key).toBe(`${slot?.node.key}/m:elective`);
    expect(reloaded.structure.modulePlacements["elective"]).toBe(
      slot?.node.key,
    );
    expect(rendered.courseAt.get("c-elective")?.node.key).toBe(added?.node.key);
    expect(rendered.courseAt.has("c-extra")).toBe(false);
    const innerNode = rendered.moduleAt.get("inner");
    const moduleXNode = rendered.moduleAt.get("mod-x");
    expect(innerNode?.node.key.startsWith(`${moduleXNode?.node.key}/`)).toBe(
      true,
    );
    expect(reloaded.structure.modulePlacements["inner"]).toBe(
      moduleXNode?.node.key,
    );
    expect(rendered.courseAt.get("c-inner")?.node.key).toBe(
      innerNode?.node.key,
    );
    expect(reloaded.structure.placedCourseIds).toEqual(
      projection.subtree.map((course) => course.group_id),
    );
    expect(reloaded.completedCourseIds).toEqual(["c-shared"]);
    expect(reloaded.selectedCourseIds).toContain("c-shared");
    expect(reloaded.selectedCourseIds).toContain("c-elective");
  });

  it("reports unplaceable plan items while retaining representable courses loose", () => {
    const modWait = structureModule(
      {
        ...moduleSummary("mod-wait", "MOD-WAIT"),
        name_en: "Official waiting module",
      },
      mandatory([]),
    );
    const programmeC = report(
      structureModule(
        moduleSummary("prog-c", "PROG-C", "DegreeProgramme"),
        mandatory([
          courseChild("c-official", "C-OFFICIAL", 0),
          moduleChild(modWait, 1),
        ]),
      ),
    );
    const plan: ImportedPlan = {
      programmes: [
        {
          kind: "module",
          groupId: "prog-c",
          versionId: "v-prog-c",
          code: "PROG-C",
          name: "Programme C",
          children: [
            {
              kind: "module",
              groupId: null,
              versionId: null,
              code: null,
              name: "Unknown module",
              children: [
                {
                  kind: "course",
                  groupId: "c-kept",
                  code: "C-KEPT",
                  name: "Kept course",
                  completed: true,
                },
              ],
            },
            {
              kind: "module",
              groupId: null,
              versionId: null,
              code: "MOD-WAIT",
              name: "Extracted waiting module",
              children: [],
            },
            {
              kind: "module",
              groupId: "mod-gone",
              versionId: null,
              code: "MOD-GONE",
              name: "Removed module",
              children: [
                {
                  kind: "course",
                  groupId: "c-absent",
                  code: "C-ABSENT",
                  name: "Absent course",
                  completed: false,
                },
              ],
            },
            {
              kind: "course",
              groupId: null,
              code: "C-NULL",
              name: "No identity",
              completed: false,
            },
          ],
        },
      ],
    };
    const content = contentOf(
      [programmeC],
      [courseSummary("c-kept", "C-KEPT")],
    );
    const store = createBrowserStore(localStorage);
    store.importProfile(prepareImportedProfile(plan, "Partial plan", "en"));
    const profileState = currentState(store);
    const resolved = placePendingImport(profileState, content, {
      courseIds: ["c-absent"],
      moduleIds: ["mod-gone"],
    });
    expect(resolved?.issues).toEqual([
      { name: "Unknown module", code: null, reason: "no_official_match" },
      { name: "Removed module", code: "MOD-GONE", reason: "module_not_placed" },
      {
        name: "Absent course",
        code: "C-ABSENT",
        reason: "course_not_represented",
      },
      { name: "No identity", code: "C-NULL", reason: "unresolved" },
    ]);
    applyStructure(
      store,
      (state) =>
        placePendingImport(state, content, {
          courseIds: ["c-absent"],
          moduleIds: ["mod-gone"],
        })?.change ?? null,
    );
    const reloaded = currentState(createBrowserStore(localStorage));
    expect(reloaded.pendingPlanImport).toBeNull();
    expect(reloaded.structure.programmeGroupIds).toEqual(["prog-c"]);
    expect(reloaded.completedCourseIds).toEqual(["c-kept"]);
    expect(reloaded.selectedCourseIds).toEqual(["c-kept"]);
    const projection = resolveStructure(
      buildProgrammeForest([programmeC]),
      reloaded.structure,
      content,
    );
    const rendered = indexResolved(projection);
    expect(rendered.courseAt.get("c-kept")?.node.kind).toBe("unplaced");
    expect(rendered.courseAt.has("c-absent")).toBe(false);
    expect(rendered.courseAt.has("c-official")).toBe(false);
    const placedWait = rendered.moduleAt.get("mod-wait");
    expect(placedWait).toBeDefined();
    expect(reloaded.structure.modulePlacements["mod-wait"]).toBe(
      programmeKey("prog-c"),
    );
  });

  it("preserves restored edits and course sets when a plan is waiting", () => {
    const programme = report(
      structureModule(
        moduleSummary("prog-p", "PROG-P", "DegreeProgramme"),
        mandatory([courseChild("c-official", "C-OFFICIAL", 0)]),
      ),
    );
    const plan: ImportedPlan = {
      programmes: [
        {
          kind: "module",
          groupId: "prog-p",
          versionId: "sisu-prog-p",
          code: "PROG-P",
          name: "Programme P",
          children: [],
        },
      ],
    };
    const prepared = prepareImportedProfile(plan, "Restored plan", "fi");
    const state = prepared.state;
    state.selectedCourseIds = ["c-unplaced", "c-completed"];
    state.completedCourseIds = ["c-completed"];
    state.search = { ...state.search, query: "restored query" };
    state.structure.programmeGroupIds = ["prog-p"];
    state.structure.choices = { "p:prog-p/1": ["p:prog-p/1/0"] };
    state.structure.coursePlacements = { "c-official": null };
    state.structure.modulePlacements = { "mod-removed": null };
    state.structure.placedCourseIds = ["c-unplaced"];
    const envelope: BrowserState = {
      schemaVersion: 3,
      currentProfileId: "default",
      profiles: { default: { name: "Restored plan", state } },
    };
    const imported = parseBrowserImport(
      JSON.stringify({
        type: "course-explorer/state/3",
        data: JSON.stringify(envelope),
      }),
    );
    if (imported.type !== "course-explorer/state/3")
      throw new Error("Expected a state import");
    const store = createBrowserStore(localStorage);
    store.restoreState(imported.state);
    const restored = store.getSnapshot();
    const raw = localStorage.getItem(BROWSER_STORAGE_KEY);
    const content = contentOf(
      [programme],
      [
        courseSummary("c-unplaced", "C-UNPLACED"),
        courseSummary("c-completed", "C-COMPLETED"),
      ],
    );
    for (const id of state.selectedCourseIds)
      expect(content.courses.has(id)).toBe(true);
    expect(content.reports["prog-p"]).toBe(programme);
    applyStructure(
      store,
      (current) =>
        placePendingImport(current, content, {
          courseIds: [],
          moduleIds: [],
        })?.change ?? null,
    );
    expect(store.getSnapshot()).toEqual(restored);
    expect(localStorage.getItem(BROWSER_STORAGE_KEY)).toBe(raw);
    const reloaded = createBrowserStore(localStorage);
    expect(reloaded.getSnapshot()).toEqual(restored);
    const reloadedState = currentState(reloaded);
    expect(reloadedState.pendingPlanImport).toEqual(plan);
    expect(reloadedState.selectedCourseIds).toContain("c-unplaced");
    const ordered = prepareImportedProfile(plan, "Ordered plan", "fi").state;
    ordered.structure.courseOrder = ["c-unplaced"];
    const orderedStore = createBrowserStore(localStorage);
    orderedStore.restoreState({
      schemaVersion: 3,
      currentProfileId: "default",
      profiles: { default: { name: "Ordered plan", state: ordered } },
    });
    const orderedRaw = localStorage.getItem(BROWSER_STORAGE_KEY);
    const orderedBefore = orderedStore.getSnapshot();
    applyStructure(
      orderedStore,
      (current) =>
        placePendingImport(current, content, {
          courseIds: [],
          moduleIds: [],
        })?.change ?? null,
    );
    expect(orderedStore.getSnapshot()).toEqual(orderedBefore);
    expect(localStorage.getItem(BROWSER_STORAGE_KEY)).toBe(orderedRaw);
  });
});

describe("study-plan PDF import", () => {
  const textItem = (
    str: string,
    x: number,
    y: number,
    height = 7.5,
  ): SisuPdfTextItem => ({
    str,
    transform: [1, 0, 0, 1, x, y],
    width: Math.max(1, str.length * 4),
    height,
  });
  const textPage = (
    page: number,
    items: readonly SisuPdfTextItem[],
  ): SisuPdfTextPage => ({ page, items });

  it("extracts supported bilingual plan rows from positioned text", () => {
    const pages = [
      textPage(1, [
        textItem("Sisu plan title", 40, 700, 15),
        textItem("PROG-A", 80, 500),
        textItem("Programme", 112, 500),
        textItem("A", 150, 500),
        textItem("Selections done", 300, 500),
        textItem("MOD-1", 100, 480),
        textItem("Module", 132, 480),
        textItem("one", 170, 480),
        textItem("Parts completed", 300, 480),
        textItem("CS.100", 120, 460),
        textItem("Programming", 160, 460),
        textItem("5 op 1.2.2026", 300, 460),
        textItem("basics", 120, 448),
        textItem("MOD-2", 100, 430),
        textItem("Informa-", 132, 430),
        textItem("Valinnat kesken", 300, 430),
        textItem("tiotekniikka", 100, 418),
        textItem("CS.200", 120, 400),
        textItem("Conﬁguration", 160, 400),
        textItem("3 op", 300, 400),
        textItem("footer", 40, 20),
      ]),
      textPage(2, [
        textItem("repeating column header", 100, 800),
        textItem("basics", 120, 760),
        textItem("CS.300", 120, 740),
        textItem("Data", 160, 740),
        textItem("5 op 2.3.2026", 300, 740),
        textItem("footer", 40, 20),
      ]),
    ];

    const rows = extractSisuPdfRows(pages);

    expect(
      rows.map((row) => ({
        kind: row.kind,
        code: row.code,
        name: row.name,
        completed: row.completed,
      })),
    ).toEqual([
      {
        kind: "module",
        code: "PROG-A",
        name: "Programme A",
        completed: false,
      },
      {
        kind: "module",
        code: "MOD-1",
        name: "Module one",
        completed: false,
      },
      {
        kind: "course",
        code: "CS.100",
        name: "Programming basics",
        completed: true,
      },
      {
        kind: "module",
        code: "MOD-2",
        name: "Informa-tiotekniikka",
        completed: false,
      },
      {
        kind: "course",
        code: "CS.200",
        name: "Configuration basics",
        completed: false,
      },
      {
        kind: "course",
        code: "CS.300",
        name: "Data",
        completed: true,
      },
    ]);
    expect(rows[0].x).toBe(80);
    expect(rows.some((row) => row.name.includes("footer"))).toBe(false);
  });

  it("matches plan rows only within served official identities and parent scopes", async () => {
    const catalogueModule = (
      sisuModuleId: string,
      moduleType: string,
      code: string,
      nameEn: string,
    ) => ({
      source_version_id: sisuModuleId,
      module_type: moduleType,
      code,
      name_fi: null,
      name_en: nameEn,
      validity_start: null,
      validity_end: null,
      curriculum_periods: [],
    });
    const catalogue = {
      dataset_identity: "pdf-test-dataset",
      courses: [
        courseSummary("c-1", "C-1"),
        courseSummary("c-group", "C-GROUP"),
        courseSummary("c-multi", "C-MULTI"),
      ],
      modules: [
        {
          group_id: "prog-1",
          candidates: [
            catalogueModule(
              "v-prog",
              "DegreeProgramme",
              "PROG-1",
              "Programme One",
            ),
          ],
        },
        {
          group_id: "mod-1",
          candidates: [
            catalogueModule("v-mod-1", "Module", "MOD-1", "Module one"),
          ],
        },
        {
          group_id: "mod-multi",
          candidates: [
            catalogueModule("v-multi-1", "Module", "MOD-MULTI", "Multi module"),
            catalogueModule("v-multi-2", "Module", "MOD-MULTI", "Multi module"),
          ],
        },
        {
          group_id: "group-a",
          candidates: [catalogueModule("v-a", "Module", "GRP-A", "Same name")],
        },
        {
          group_id: "group-b",
          candidates: [catalogueModule("v-b", "Module", "GRP-B", "Same name")],
        },
      ],
    };
    const groupA = structureModule(
      { ...moduleSummary("group-a", "GRP-A"), name_en: "Same name" },
      mandatory([courseChild("c-group", "C-GROUP", 0)]),
    );
    const groupB = structureModule(
      { ...moduleSummary("group-b", "GRP-B"), name_en: "Same name" },
      mandatory([]),
    );
    const mod1 = structureModule(
      moduleSummary("mod-1", "MOD-1"),
      mandatory([
        courseChild("c-1", "C-1", 0),
        moduleChild(groupA, 1),
        moduleChild(groupB, 2),
      ]),
    );
    const multi = structureModule(
      moduleSummary("mod-multi", "MOD-MULTI"),
      mandatory([courseChild("c-multi", "C-MULTI", 0)]),
    );
    const programme = report(
      structureModule(
        moduleSummary("prog-1", "PROG-1", "DegreeProgramme"),
        mandatory([moduleChild(mod1, 0), moduleChild(multi, 1)]),
      ),
    );
    const requested: string[][] = [];
    const fetchSpy = vi.fn(async (path: string, init?: RequestInit) => {
      if (path === "/api/catalogue")
        return new Response(JSON.stringify(catalogue), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      if (path === "/api/study-structure") {
        const body = JSON.parse(String(init?.body)) as {
          root_group_ids: string[];
        };
        requested.push(body.root_group_ids);
        return new Response(
          JSON.stringify({
            academic_year_start: 2026,
            roots: [programme.root],
            unavailable_root_group_ids: [],
            version_selection_required: [],
            dataset_identity: "pdf-test-dataset",
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      throw new Error(`Unexpected request ${path}`);
    });
    vi.stubGlobal("fetch", fetchSpy);
    const cache = new ReadCache("pdf-test-dataset", READ_CACHE_CAPACITY);
    const rows = [
      {
        kind: "module",
        code: "PROG-1",
        name: "Programme One",
        completed: false,
        x: 80,
      },
      {
        kind: "module",
        code: "MOD-1",
        name: "Module one",
        completed: false,
        x: 100,
      },
      {
        kind: "course",
        code: "C-1",
        name: "Course one",
        completed: true,
        x: 120,
      },
      {
        kind: "module",
        code: null,
        name: "Same name",
        completed: false,
        x: 120,
      },
      {
        kind: "course",
        code: "C-GROUP",
        name: "Group course",
        completed: false,
        x: 140,
      },
      {
        kind: "course",
        code: "C-UNKNOWN",
        name: "Unknown course",
        completed: true,
        x: 120,
      },
      {
        kind: "module",
        code: "MOD-MULTI",
        name: "Multi module",
        completed: false,
        x: 100,
      },
      {
        kind: "course",
        code: "C-MULTI",
        name: "Multi course",
        completed: false,
        x: 120,
      },
      {
        kind: "module",
        code: "MISSING",
        name: "Missing programme",
        completed: false,
        x: 80,
      },
    ] as const;

    const result = await resolveExtractedPlan(
      extractedPlanFromRows(rows),
      cache,
      new AbortController().signal,
    );

    const [prog] = result.plan.programmes;
    expect(prog.groupId).toBe("prog-1");
    expect(prog.versionId).toBe("v-prog");
    const matchedMod1 = prog.children.find(
      (item): item is ImportedModule =>
        item.kind === "module" && item.groupId === "mod-1",
    );
    expect(matchedMod1?.versionId).toBe("v-mod-1");
    const course = matchedMod1?.children.find(
      (item) => item.kind === "course" && item.code === "C-1",
    );
    expect(course).toMatchObject({ groupId: "c-1", completed: true });
    const ambiguousModule = matchedMod1?.children.find(
      (item): item is ImportedModule =>
        item.kind === "module" && item.name === "Same name",
    );
    expect(ambiguousModule).toMatchObject({
      groupId: null,
      versionId: null,
      code: null,
    });
    expect(ambiguousModule?.children[0]).toMatchObject({
      kind: "course",
      code: "C-GROUP",
      groupId: "c-group",
    });
    const multiModule = prog.children.find(
      (item): item is ImportedModule =>
        item.kind === "module" && item.groupId === "mod-multi",
    );
    expect(multiModule?.versionId).toBeNull();
    expect(
      multiModule?.children.find(
        (item) => item.kind === "course" && item.code === "C-MULTI",
      ),
    ).toMatchObject({ groupId: "c-multi" });
    const ambiguousIssue = result.issues.find(
      (issue) => issue.reason === "ambiguous_official_match",
    );
    expect(
      ambiguousIssue?.candidates?.map((candidate) => candidate.groupId).sort(),
    ).toEqual(["group-a", "group-b"]);
    const unresolvedProgramme = result.plan.programmes[1];
    expect(unresolvedProgramme).toMatchObject({
      groupId: null,
      code: "MISSING",
      name: "Missing programme",
    });
    expect(result.issues).toContainEqual({
      name: "Missing programme",
      code: "MISSING",
      reason: "no_official_match",
    });
    const unknown = result.issues.find(
      (issue) => issue.name === "Unknown course",
    );
    expect(unknown).toMatchObject({
      code: "C-UNKNOWN",
      reason: "no_official_match",
    });
    expect(unknown?.candidates).toBeUndefined();
    expect([...new Set(requested.flatMap((ids) => [...ids]))].sort()).toEqual([
      "prog-1",
    ]);
    expect(JSON.stringify(fetchSpy.mock.calls)).not.toContain("C-UNKNOWN");
    expect(JSON.stringify(fetchSpy.mock.calls)).not.toContain("Same name");
  });
});
