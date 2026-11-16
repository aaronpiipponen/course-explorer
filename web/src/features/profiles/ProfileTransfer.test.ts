/** Rendered proof that profile, whole-state and plan imports wait for their confirmation. */
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ProfileTransfer } from "./ProfileTransfer";
import type { WebFeatures } from "../../api/types";
import { WebFeaturesContext } from "../../app/webFeaturesContext";
import { uiText } from "../../shared/wording";
import { StoreContext } from "../../state/context";
import { READ_CACHE_CAPACITY, ReadCache } from "../../app/readCache";
import { ReadCacheContext } from "../../app/readCacheContext";
import {
  BROWSER_STORAGE_KEY,
  createBrowserStore,
  type BrowserStore,
} from "../../state/store";
import { waitFor } from "../../test/waitFor";
import type { Profile } from "../../state/types";

const { readSisuPdfMock } = vi.hoisted(() => ({
  readSisuPdfMock: vi.fn(),
}));

vi.mock("../imports/pdf", () => ({ readSisuPdf: readSisuPdfMock }));
vi.mock("../structure/structureStorage", () => ({
  readPersistedStructures: vi.fn(async () => ({})),
  writePersistedStructures: vi.fn(async () => undefined),
}));

(
  globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }
).IS_REACT_ACT_ENVIRONMENT = true;

const originalMedia = globalThis.matchMedia;

const featuresWithoutFormats: WebFeatures = {
  embeddings_enabled: true,
  inference_enabled: false,
  prerequisites_enabled: true,
  dataset_identity: "test-dataset",
  display_label: "example-uni",
  university_names: { en: "Example University", fi: "Esimerkkiyliopisto" },
  source_attribution: { en: "Source", fi: "Lähde" },
  assumed_course_credits: "5",
  study_plan_script: null,
  study_plan_formats: [],
};
const sisuFeatures: WebFeatures = {
  ...featuresWithoutFormats,
  study_plan_script: "sisu",
  study_plan_formats: ["sisu_page", "sisu_pdf", "sisu_capture"],
};
const peppiFeatures: WebFeatures = {
  ...featuresWithoutFormats,
  study_plan_script: "peppi",
  study_plan_formats: ["peppi_page", "peppi_capture"],
};

describe("profile transfer confirmation", () => {
  let container: HTMLDivElement;
  let root: Root;
  let store: BrowserStore;
  let features: WebFeatures;
  let input!: HTMLInputElement;
  let originalFilesDescriptor: PropertyDescriptor | undefined;
  const selectedFiles: File[] = [];
  let payload: string;
  let imported: ReturnType<BrowserStore["getSnapshot"]>;
  let initialEnvelope: string;
  let reload!: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.clearAllMocks();
    readSisuPdfMock.mockReset();
    localStorage.clear();
    sessionStorage.clear();
    // jsdom has no media queries. The panel presents its desktop import area.
    globalThis.matchMedia = ((media: string) => ({
      matches: false,
      media,
      onchange: null,
      addListener() {},
      removeListener() {},
      addEventListener() {},
      removeEventListener() {},
      dispatchEvent: () => false,
    })) as unknown as typeof window.matchMedia;
    const realWindow = window;
    reload = vi.fn();
    vi.stubGlobal(
      "window",
      new Proxy(realWindow, {
        get(target, key) {
          if (key === "location") return { reload };
          return Reflect.get(target, key, target);
        },
      }),
    );
    container = document.createElement("div");
    document.body.appendChild(container);

    // A separate storage builds a distinct whole-state file through the real export owner.
    const source = createBrowserStore(sessionStorage);
    source.updateCurrent((state) => ({
      ...state,
      language: "fi",
      selectedCourseIds: ["course-imported"],
      completedCourseIds: ["course-imported"],
      search: { ...state.search, query: "imported query" },
    }));
    payload = JSON.stringify(source.exportState());
    imported = source.getSnapshot();

    store = createBrowserStore(localStorage);
    store.saveAsProfile("Second");
    const envelope = localStorage.getItem(BROWSER_STORAGE_KEY);
    if (envelope === null) throw new Error("The saved envelope is missing");
    initialEnvelope = envelope;
    root = createRoot(container);
    features = featuresWithoutFormats;
  });

  function render(importOnly = false): void {
    const cache = new ReadCache("test-dataset", READ_CACHE_CAPACITY);
    act(() =>
      root.render(
        createElement(
          ReadCacheContext.Provider,
          { value: cache },
          createElement(
            WebFeaturesContext.Provider,
            { value: features },
            createElement(
              StoreContext.Provider,
              { value: store },
              createElement(ProfileTransfer, { store, importOnly }),
            ),
          ),
        ),
      ),
    );
    const found =
      container.querySelector<HTMLInputElement>('input[type="file"]');
    if (!found) throw new Error("The file input is missing");
    input = found;
    originalFilesDescriptor = Object.getOwnPropertyDescriptor(input, "files");
  }

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    if (originalFilesDescriptor === undefined)
      Reflect.deleteProperty(input, "files");
    else Object.defineProperty(input, "files", originalFilesDescriptor);
    for (const file of selectedFiles) Reflect.deleteProperty(file, "text");
    selectedFiles.length = 0;
    globalThis.matchMedia = originalMedia;
    vi.unstubAllGlobals();
  });

  async function chooseFile(): Promise<void> {
    const file = new File([payload], "saved-data.json", {
      type: "application/json",
    });
    Object.defineProperty(file, "text", {
      configurable: true,
      value: async () => payload,
    });
    selectedFiles.push(file);
    Object.defineProperty(input, "files", {
      configurable: true,
      value: [file],
    });
    await act(async () => {
      input.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await waitFor(() =>
      expect(document.body.querySelector('[role="dialog"]')).toBeTruthy(),
    );
  }

  async function chooseUnsupported(file: File): Promise<void> {
    selectedFiles.push(file);
    Object.defineProperty(input, "files", {
      configurable: true,
      value: [file],
    });
    await act(async () => {
      input.dispatchEvent(new Event("change", { bubbles: true }));
    });
  }

  function dialogButton(name: string): HTMLButtonElement {
    const found = [
      ...document.body.querySelectorAll<HTMLButtonElement>(
        '[role="dialog"] button',
      ),
    ].find((button) => button.textContent?.trim() === name);
    if (!found) throw new Error(`The dialog button ${name} is missing`);
    return found;
  }

  it("refuses an unconfigured file and replaces every profile after confirmation", async () => {
    render(true);
    expect(
      container.querySelector("h3.profile-transfer-heading")?.textContent,
    ).toBe(uiText("en", "Import"));
    expect(container.querySelector(".profile-choose")?.textContent).toBe(
      uiText("en", "Choose file"),
    );
    const exportButton = [...container.querySelectorAll("button")].find(
      (button) => button.textContent?.trim() === uiText("en", "Export"),
    );
    if (!exportButton) throw new Error("The Export button is missing");
    expect(exportButton.getAttribute("aria-controls")).toBe(
      "structure-profile-export-list",
    );
    const exportList = container.querySelector(
      "#structure-profile-export-list",
    );
    if (!exportList) throw new Error("The export list is missing");
    expect(
      [...exportList.querySelectorAll("button")].map((button) =>
        button.textContent?.trim(),
      ),
    ).toEqual(["My plan", "Second", uiText("en", "All saved data")]);
    expect(container.querySelector("#profile-export-list")).toBeNull();

    act(() => root.unmount());
    root = createRoot(container);

    render();
    const note = container.querySelector(".profile-drop-note")?.textContent;
    expect(note).toContain("or drop a JSON profile export");
    expect(
      [...container.querySelectorAll("button")].some(
        (button) =>
          button.textContent?.trim() === uiText("en", "Copy import script"),
      ),
    ).toBe(false);

    await chooseUnsupported(
      new File(["%PDF-1.7"], "plan.pdf", { type: "application/pdf" }),
    );
    await waitFor(() =>
      expect(document.body.textContent).toContain(
        uiText(
          "en",
          "This file is not one of this site's import formats. Use a file type listed in the import area.",
        ),
      ),
    );
    expect(document.body.querySelector('[role="dialog"]')).toBeNull();
    await chooseUnsupported(
      new File(
        [JSON.stringify({ type: "course-explorer/sisu/1", markup: "<p></p>" })],
        "capture.json",
        { type: "application/json" },
      ),
    );
    await waitFor(() =>
      expect(document.body.textContent).toContain(
        uiText(
          "en",
          "This file is not one of this site's import formats. Use a file type listed in the import area.",
        ),
      ),
    );
    expect(document.body.querySelector('[role="dialog"]')).toBeNull();

    await chooseFile();
    expect(reload).not.toHaveBeenCalled();
    expect(document.body.querySelector('[role="dialog"]')).toBeTruthy();
    expect(store.getSnapshot().profiles.default).toBeTruthy();
    expect(localStorage.getItem(BROWSER_STORAGE_KEY)).toBe(initialEnvelope);

    act(() => dialogButton(uiText("en", "Cancel")).click());
    await waitFor(() =>
      expect(document.body.querySelector('[role="dialog"]')).toBeNull(),
    );
    expect(reload).not.toHaveBeenCalled();
    expect(localStorage.getItem(BROWSER_STORAGE_KEY)).toBe(initialEnvelope);
    expect(Object.keys(store.getSnapshot().profiles)).toHaveLength(2);

    await chooseFile();
    expect(reload).not.toHaveBeenCalled();
    expect(localStorage.getItem(BROWSER_STORAGE_KEY)).toBe(initialEnvelope);
    act(() => dialogButton(uiText("en", "Replace")).click());
    await waitFor(() => expect(reload).toHaveBeenCalledTimes(1));

    const restored = createBrowserStore(localStorage).getSnapshot();
    expect(restored).toEqual(imported);
    expect(Object.keys(restored.profiles)).toEqual(
      Object.keys(imported.profiles),
    );
    expect(restored.currentProfileId).toBe(imported.currentProfileId);
  });

  it("overwrites the profile that holds the imported name after its confirmation", async () => {
    render();
    // The imported file is one profile of a separate storage, named like the second saved profile.
    const source = createBrowserStore(sessionStorage);
    source.saveAsProfile("Second");
    const sourceId = source.getSnapshot().currentProfileId;
    source.updateCurrent((state) => ({
      ...state,
      selectedCourseIds: ["course-second"],
      completedCourseIds: [],
    }));
    payload = JSON.stringify(source.exportProfile(sourceId));
    const importedState = source.getSnapshot().profiles[sourceId].state;

    await chooseFile();
    act(() => dialogButton(uiText("en", "Overwrite")).click());
    await waitFor(() =>
      expect(document.body.querySelector('[role="dialog"]')).toBeNull(),
    );

    const saved = store.getSnapshot();
    const ids = Object.keys(saved.profiles);
    expect(ids).toHaveLength(2);
    const overwritten = ids.find((id) => saved.profiles[id].name === "Second");
    if (overwritten === undefined)
      throw new Error("The overwritten profile is missing");
    expect(saved.profiles[overwritten].name).toBe("Second");
    expect(saved.profiles[overwritten].state).toEqual(importedState);
    expect(saved.currentProfileId).toBe(overwritten);
  });

  it.each(["pdf", "capture", "peppi"] as const)(
    "keeps a study plan transient until its named confirmation",
    async (source) => {
      features = source === "peppi" ? peppiFeatures : sisuFeatures;
      render();
      let selectedFile: File;
      let pdfRows: readonly unknown[] = [];
      let settle: () => Promise<void>;
      let expectedText: readonly string[];
      let absentText: readonly string[];
      let assertCreated: (created: Profile) => void;
      const profileName = `${source} plan`;

      const catalogue = {
        dataset_identity: "pdf-test-dataset",
        courses: [
          {
            group_id: "c-1",
            name_fi: null,
            name_en: "Course one",
            code: "C-1",
          },
        ],
        modules: [
          {
            group_id: "prog-1",
            candidates: [
              {
                source_version_id: "v-prog",
                module_type: "DegreeProgramme",
                code: "PROG-1",
                name_fi: null,
                name_en: "Programme One",
              },
            ],
          },
          {
            group_id: "mod-1",
            candidates: [
              {
                source_version_id: "v-mod-1",
                module_type: "Module",
                code: "MOD-1",
                name_fi: null,
                name_en: "Module one",
              },
            ],
          },
          {
            group_id: "mod-uncoded",
            candidates: [
              {
                source_version_id: "v-uncoded",
                module_type: "Module",
                code: null,
                name_fi: null,
                name_en: "Uncoded module",
              },
            ],
          },
          {
            group_id: "group-a",
            candidates: [
              {
                source_version_id: "v-a",
                module_type: "Module",
                code: "GRP-A",
                name_fi: null,
                name_en: "Same name",
              },
            ],
          },
          {
            group_id: "group-b",
            candidates: [
              {
                source_version_id: "v-b",
                module_type: "Module",
                code: "GRP-B",
                name_fi: null,
                name_en: "Same name",
              },
            ],
          },
        ],
      };
      const rule = (patch: Record<string, unknown>) => ({
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
      const moduleNode = (
        groupId: string,
        name: string,
        children: unknown[],
      ) => ({
        module: {
          group_id: groupId,
          source_version_id: `v-${groupId}`,
          module_type: groupId === "prog-1" ? "DegreeProgramme" : "Module",
          code: groupId.toUpperCase(),
          name_fi: null,
          name_en: name,
        },
        target_credits_min: null,
        target_credits_max: null,
        rule: rule({ all_mandatory: true, children }),
      });
      const groupA = moduleNode("group-a", "Same name", []);
      const groupB = moduleNode("group-b", "Same name", []);
      const mod1 = moduleNode("mod-1", "Module one", [
        rule({
          node_type: "CourseUnitRule",
          course: { group_id: "c-1", name_en: "Course one", code: "C-1" },
        }),
        rule({ node_type: "ModuleRule", module: groupA }),
        rule({ node_type: "ModuleRule", module: groupB }),
      ]);
      const programme = moduleNode("prog-1", "Programme One", [
        rule({ node_type: "ModuleRule", module: mod1 }),
      ]);
      const fetchSpy = vi.fn(async (path: string) => {
        if (path === "/api/catalogue")
          return new Response(JSON.stringify(catalogue), {
            status: 200,
            headers: { "content-type": "application/json" },
          });
        if (path === "/api/study-structure")
          return new Response(
            JSON.stringify({
              academic_year_start: 2026,
              roots: [programme],
              unavailable_root_group_ids: [],
              version_selection_required: [],
              dataset_identity: "pdf-test-dataset",
            }),
            { status: 200, headers: { "content-type": "application/json" } },
          );
        throw new Error(`Unexpected request ${path}`);
      });
      vi.stubGlobal("fetch", fetchSpy);

      if (source === "pdf") {
        const rows = [
          {
            kind: "module",
            code: "PROG-1",
            name: "Programme One",
            completed: false,
            page: 1,
            x: 80,
          },
          {
            kind: "module",
            code: "MOD-1",
            name: "Module one",
            completed: false,
            page: 1,
            x: 100,
          },
          {
            kind: "course",
            code: "C-1",
            name: "Course one",
            completed: true,
            page: 1,
            x: 120,
          },
          {
            kind: "module",
            code: null,
            name: "Same name",
            completed: false,
            page: 1,
            x: 120,
          },
        ] as const;
        let resolveRows!: (value: typeof rows) => void;
        readSisuPdfMock.mockReturnValueOnce(
          new Promise<typeof rows>((resolve) => {
            resolveRows = resolve;
          }),
        );
        pdfRows = rows;
        selectedFile = new File(["%PDF-1.7"], "plan.pdf", {
          type: "application/pdf",
        });
        settle = async () => {
          await act(async () => {
            resolveRows(rows);
            await Promise.resolve();
          });
        };
        expectedText = [
          uiText(
            "en",
            "Several official items match, so this item was left out.",
          ),
          uiText("en", "Matching official items:"),
          "group-a",
          "group-b",
        ];
        absentText = ["Choose module version"];
        assertCreated = (created) => {
          expect(created.state.pendingPlanImport?.programmes[0]).toMatchObject({
            groupId: "prog-1",
            versionId: "v-prog",
          });
          expect(created.state.completedCourseIds).toEqual(["c-1"]);
          expect(localStorage.getItem(BROWSER_STORAGE_KEY)).not.toContain(
            "candidates",
          );
          expect(localStorage.getItem(BROWSER_STORAGE_KEY)).not.toContain(
            "ambiguous_official_match",
          );
        };
      } else if (source === "peppi") {
        const page = `<html><body>
          <div id="header-row-div"><h2>PROG-1, Programme One</h2></div>
          <div id="structure">
            <section class="panel lu-container">
              <div class="panel-heading">
                <h4 class="study-header">
                  <button onclick="openLearningUnitInfoModal('STUDY_MODULE', 'MOD-1', 1)">
                    <span class="lu-name">Module one</span>
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
                  <tr class="lu-container">
                    <td class="table-cell-status"></td>
                    <td class="table-cell-name">
                      <button onclick="openLearningUnitInfoModal('COURSE_UNIT', 'C-UNKNOWN', 1)">
                        <span class="lu-name">Unknown course</span>
                      </button>
                    </td>
                  </tr>
                </tbody></table>
              </div>
            </section>
          </div>
        </body></html>`;
        selectedFile = new File([page], "tarkastelu.html", {
          type: "text/html",
        });
        Object.defineProperty(selectedFile, "text", {
          configurable: true,
          value: async () => page,
        });
        settle = async () => {};
        expectedText = [
          uiText(
            "en",
            "No official match was found, so this item was left out.",
          ),
          "C-UNKNOWN Unknown course",
        ];
        absentText = ["Choose module version"];
        assertCreated = (created) => {
          expect(created.state.pendingPlanImport?.programmes[0]).toMatchObject({
            groupId: "prog-1",
            versionId: "v-prog",
          });
          expect(created.state.completedCourseIds).toEqual(["c-1"]);
          expect(created.state.selectedCourseIds).not.toContain("c-unknown");
        };
      } else {
        const markup = `
          <plan-module>
            <span class="graduate"></span>
            <div class="status"><span id="v-prog-statuscell"></span></div>
            <span ng-bind=":: $ctrl.module.code">PROG-1</span>
            <span data-cy="plan-module-name">Programme One</span>
            <plan-module>
              <div class="status"><span id="v-uncoded-statuscell"></span></div>
              <span data-cy="plan-module-name">Uncoded module</span>
              <course-unit-box>
                <span class="courseunit-code">C-UNKNOWN</span>
                <span class="title-box">C-UNKNOWN Unknown course</span>
              </course-unit-box>
            </plan-module>
          </plan-module>`;
        const capture = JSON.stringify({
          type: "course-explorer/sisu/1",
          markup,
        });
        selectedFile = new File([capture], "sisu-plan-capture.json", {
          type: "application/json",
        });
        Object.defineProperty(selectedFile, "text", {
          configurable: true,
          value: async () => capture,
        });
        settle = async () => {};
        expectedText = [
          uiText(
            "en",
            "No official match was found, so this item was left out.",
          ),
          "C-UNKNOWN Unknown course",
        ];
        absentText = ["Uncoded module", "Choose module version"];
        assertCreated = (created) => {
          expect(created.state.pendingPlanImport?.programmes[0]).toMatchObject({
            groupId: "prog-1",
            versionId: "v-prog",
          });
          expect(created.state.selectedCourseIds).toEqual([]);
        };
      }

      if (source === "peppi") {
        const note =
          container.querySelector(".profile-drop-note")?.textContent ?? "";
        expect(note).toContain(uiText("en", "Peppi saved page"));
        expect(note.indexOf(uiText("en", "Peppi capture"))).toBeGreaterThan(
          note.indexOf(uiText("en", "Peppi saved page")),
        );
        expect(
          [...container.querySelectorAll("button")].some(
            (button) =>
              button.textContent?.trim() === uiText("en", "Copy import script"),
          ),
        ).toBe(true);
        const help = container.querySelector<HTMLButtonElement>(
          'button.help-tip[aria-label="Explain profiles and import"]',
        );
        if (!help) throw new Error("The help trigger is missing");
        act(() => help.click());
        const drawer =
          document.body.querySelector(".help-drawer")?.textContent ?? "";
        expect(drawer).toContain(uiText("en", "Peppi saved page"));
        expect(drawer).toContain(uiText("en", "Peppi capture"));
        expect(drawer).toContain(uiText("en", "Peppi import script"));
        const close = document.body.querySelector<HTMLButtonElement>(
          ".help-drawer .pill-btn",
        );
        if (!close) throw new Error("The help close button is missing");
        act(() => close.click());
        await waitFor(() =>
          expect(document.body.querySelector(".help-drawer")).toBeNull(),
        );
      }

      const chooseSelected = async () => {
        Object.defineProperty(input, "files", {
          configurable: true,
          value: [selectedFile],
        });
        await act(async () => {
          input.dispatchEvent(new Event("change", { bubbles: true }));
        });
      };

      await chooseSelected();
      if (source === "pdf") {
        await waitFor(() => expect(readSisuPdfMock).toHaveBeenCalledTimes(1));
        expect(document.body.querySelector('[role="dialog"]')).toBeNull();
        expect(localStorage.getItem(BROWSER_STORAGE_KEY)).toBe(initialEnvelope);
        await settle();
      }
      await waitFor(() =>
        expect(document.body.querySelector('[role="dialog"]')).toBeTruthy(),
      );
      expect(localStorage.getItem(BROWSER_STORAGE_KEY)).toBe(initialEnvelope);
      const dialogText = document.body.textContent ?? "";
      for (const text of expectedText) expect(dialogText).toContain(text);
      for (const text of absentText) expect(dialogText).not.toContain(text);

      act(() => dialogButton(uiText("en", "Cancel")).click());
      await waitFor(() =>
        expect(document.body.querySelector('[role="dialog"]')).toBeNull(),
      );
      expect(localStorage.getItem(BROWSER_STORAGE_KEY)).toBe(initialEnvelope);
      expect(Object.keys(store.getSnapshot().profiles)).toHaveLength(2);

      if (source === "pdf")
        readSisuPdfMock.mockReturnValueOnce(Promise.resolve(pdfRows));
      await chooseSelected();
      await waitFor(() =>
        expect(document.body.querySelector('[role="dialog"]')).toBeTruthy(),
      );
      const nameInput =
        document.body.querySelector<HTMLInputElement>("#plan-profile-name");
      if (!nameInput) throw new Error("The profile name input is missing");
      const setValue = Object.getOwnPropertyDescriptor(
        HTMLInputElement.prototype,
        "value",
      )?.set;
      act(() => {
        setValue?.call(nameInput, profileName);
        nameInput.dispatchEvent(new Event("input", { bubbles: true }));
      });
      act(() => dialogButton(uiText("en", "Import")).click());
      await waitFor(() =>
        expect(document.body.querySelector('[role="dialog"]')).toBeNull(),
      );

      const snapshot = store.getSnapshot();
      const created = Object.values(snapshot.profiles).find(
        (profile) => profile.name === profileName,
      );
      expect(created).toBeTruthy();
      if (created) assertCreated(created);
    },
  );
});
