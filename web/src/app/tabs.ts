/** Ordered shell tab identities, their wording-resource label keys and the visible-tab policy. */
import type { UiTextKey } from "../shared/wording";
import type { TabId } from "../state/types";

/** The five tabs in canonical navigation order. */
export const TABS: readonly {
  id: TabId;
  labelKey: UiTextKey;
  phoneLabelKey: UiTextKey;
}[] = [
  { id: "search", labelKey: "Search", phoneLabelKey: "Search" },
  { id: "compare", labelKey: "Compare", phoneLabelKey: "Compare" },
  {
    id: "prerequisites",
    labelKey: "Prerequisites",
    phoneLabelKey: "Prereqs",
  },
  { id: "discover", labelKey: "Discover", phoneLabelKey: "Discover (phone)" },
  {
    id: "structure",
    labelKey: "Structure of studies",
    phoneLabelKey: "Structure",
  },
];

/** The served feature flags that decide which tabs this site shows. */
export type ServedFeatures = {
  embeddings_enabled: boolean;
  prerequisites_enabled: boolean;
};

/** Return the tabs a site serves, in canonical order, under its feature switches. */
export function visibleTabs(features: ServedFeatures): typeof TABS {
  return TABS.filter(
    (tab) =>
      (tab.id !== "compare" || features.embeddings_enabled) &&
      (tab.id !== "prerequisites" || features.prerequisites_enabled) &&
      (tab.id !== "discover" ||
        features.embeddings_enabled ||
        features.prerequisites_enabled),
  );
}

/** Return the served tab for one stored identity, resolving a hidden tab to Search. */
export function effectiveTab(tab: TabId, features: ServedFeatures): TabId {
  return visibleTabs(features).some((served) => served.id === tab)
    ? tab
    : "search";
}
