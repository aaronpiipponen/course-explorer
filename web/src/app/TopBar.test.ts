/** Rendered proof that the header follows the served tab set and resolves a stored hidden tab. */
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { setActiveTab } from "./sessionTab";
import { TABS } from "./tabs";
import { TopBar } from "./TopBar";
import { WebFeaturesContext } from "./webFeaturesContext";
import { StoreContext } from "../state/context";
import { createBrowserStore, type BrowserStore } from "../state/store";
import { uiText } from "../shared/wording";

(
  globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }
).IS_REACT_ACT_ENVIRONMENT = true;

const originalMedia = globalThis.matchMedia;
const originalObserver = globalThis.ResizeObserver;
const REPOSITORY_URL = "https://github.com/owner/repo";

function restore(target: object, name: string, value: unknown) {
  if (value === undefined) Reflect.deleteProperty(target, name);
  else Reflect.set(target, name, value);
}

describe("served header tabs", () => {
  let container: HTMLDivElement;
  let root: Root;
  let store: BrowserStore;

  beforeEach(() => {
    // jsdom has no layout observer or media queries, and the header measures its own height.
    globalThis.matchMedia = ((query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener() {},
      removeListener() {},
      addEventListener() {},
      removeEventListener() {},
      dispatchEvent: () => false,
    })) as unknown as typeof window.matchMedia;
    globalThis.ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver;
    localStorage.clear();
    sessionStorage.clear();
    setActiveTab("compare");
    container = document.createElement("div");
    document.body.appendChild(container);
    store = createBrowserStore(localStorage);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    sessionStorage.clear();
    setActiveTab("search");
    restore(globalThis, "matchMedia", originalMedia);
    restore(globalThis, "ResizeObserver", originalObserver);
  });

  function render(embeddings: boolean, prerequisites: boolean): void {
    act(() =>
      root.render(
        createElement(
          StoreContext.Provider,
          { value: store },
          createElement(
            WebFeaturesContext.Provider,
            {
              value: {
                embeddings_enabled: embeddings,
                inference_enabled: true,
                prerequisites_enabled: prerequisites,
                dataset_identity: "synthetic-dataset",
                display_label: "example-uni",
                university_names: {
                  en: "Example University",
                  fi: "Esimerkkiyliopisto",
                },
                source_attribution: {
                  en: "Source attribution",
                  fi: "Lähdemerkintä",
                },
                assumed_course_credits: "5",
                study_plan_script: null,
                study_plan_formats: [],
              },
            },
            createElement(TopBar, {
              repositoryUrl: REPOSITORY_URL,
              openDocs: () => {},
              openFeedback: () => {},
            }),
          ),
        ),
      ),
    );
  }

  function servedTabLabels(): (string | null)[] {
    const navigation = container.querySelector<HTMLElement>(
      'nav[aria-label="' + uiText("en", "Main navigation") + '"]',
    );
    return [
      ...(navigation?.querySelectorAll<HTMLButtonElement>(".nav-tab") ?? []),
    ].map((tab) => tab.textContent);
  }

  it("offers the served tabs, resolves a stored hidden tab and carries the GitHub link", () => {
    render(false, true);
    const served = TABS.filter((tab) => tab.id !== "compare");
    expect(servedTabLabels()).toEqual(
      served.map((tab) => uiText("en", tab.labelKey)),
    );
    expect(servedTabLabels()).not.toContain(uiText("en", "Compare"));
    const tabs = [
      ...(container.querySelectorAll<HTMLButtonElement>(".nav-tab") ?? []),
    ];
    expect(tabs.filter((tab) => tab.classList.contains("active"))).toHaveLength(
      1,
    );
    expect(tabs[0].classList.contains("active")).toBe(true);
    // The feedback entry is a standalone page link beside Docs, not another course tab.
    expect(container.querySelector('a[href="/feedback"]')?.textContent).toBe(
      uiText("en", "Feedback"),
    );
    // The top bar carries the GitHub mark, label and configured repository address.
    const github = container.querySelector<HTMLAnchorElement>(".github-link");
    expect(github?.getAttribute("href")).toBe(REPOSITORY_URL);
    expect(github?.textContent).toContain(uiText("en", "GitHub"));
    expect(github?.querySelector("svg")).toBeTruthy();

    // Prerequisites off removes the graph tab and leaves Discover while embeddings are on.
    render(true, false);
    expect(servedTabLabels()).toEqual(
      TABS.filter((tab) => tab.id !== "prerequisites").map((tab) =>
        uiText("en", tab.labelKey),
      ),
    );

    // Both switches off hide Compare, Prerequisites and Discover.
    render(false, false);
    expect(servedTabLabels()).toEqual(
      TABS.filter((tab) => ["search", "structure"].includes(tab.id)).map(
        (tab) => uiText("en", tab.labelKey),
      ),
    );

    // A stored hidden tab renders Search and keeps its stored value.
    setActiveTab("prerequisites");
    render(false, false);
    const active = container.querySelector(".nav-tab.active");
    expect(active?.textContent).toBe(uiText("en", "Search"));
  });
});
