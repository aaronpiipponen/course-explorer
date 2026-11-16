/**
 * Profile, whole-state and study-plan transfer inside the profile menu.
 *
 * The picker and the drop area share one read through the site's configured import formats. A
 * profile or whole-state file waits in its confirmation, and a plan file resolves its official
 * identities before the generic plan dialog asks for a profile name. The bottom row holds the help
 * drawer, the optional script copy and the export disclosure. The component also renders its
 * import area alone, with its own heading and the same export list, for the empty structure tab.
 */
import {
  useEffect,
  useRef,
  useState,
  type ChangeEvent,
  type DragEvent,
} from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { AnimatePresence } from "framer-motion";
import type { BrowserImport } from "../../state/types";
import type { BrowserStore } from "../../state/store";
import { useCurrentProfile } from "../../state/useCurrentProfile";
import { useReadCache } from "../../app/useReadCache";
import { useWebFeatures } from "../../app/useWebFeatures";
import {
  formatsForStudyPlan,
  readImportFile,
  scriptForStudyPlan,
} from "../imports/formats";
import { resolveExtractedPlan } from "../imports/resolve";
import {
  prepareImportedProfile,
  type ImportIssue,
  type ImportedPlan,
} from "../imports/plan";
import { ImportPlanDialog } from "../imports/ImportPlanDialog";
import { Disclosure } from "../../shared/Disclosure";
import { ErrorNotice } from "../../shared/ErrorNotice";
import { HelpLink, HelpTip, type HelpSection } from "../../shared/HelpTip";
import { useAction } from "../../shared/useAction";
import { useWording } from "../../shared/useWording";
import { usePhoneTabsOnClose } from "../../shared/usePhoneTabsOnClose";
import { downloadBrowserFile } from "./files";
import { PROFILE_NOTICE_MS, ProfileNotice } from "./ProfileNotice";

function asError(caught: unknown): Error {
  return caught instanceof Error ? caught : new Error(String(caught));
}

/** One resolved plan waiting in this component: the plan and its transient issues. */
type PendingPlan = {
  kind: "plan";
  plan: ImportedPlan;
  issues: readonly ImportIssue[];
};

/** Render the profile export, import, configured format and script controls. */
export function ProfileTransfer({
  store,
  importOnly = false,
}: {
  store: BrowserStore;
  /** Render the empty Structure import area with its own heading, import-only help and the same export list. */
  importOnly?: boolean;
}) {
  const { profiles } = useCurrentProfile();
  const { language, t } = useWording();
  const cache = useReadCache();
  const { study_plan_script, study_plan_formats } = useWebFeatures();
  const formats = formatsForStudyPlan(study_plan_formats);
  const script = scriptForStudyPlan(study_plan_script);
  const { runAction, error: exportError } = useAction();
  const [exportOpen, setExportOpen] = useState(false);
  const [copyStatus, setCopyStatus] = useState(false);
  const [copyError, setCopyError] = useState<Error | null>(null);
  const copyTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const copyOpener = useRef<HTMLElement | null>(null);
  const copyButton = useRef<HTMLButtonElement>(null);
  const [fileError, setFileError] = useState<Error | null>(null);
  const [confirmError, setConfirmError] = useState<Error | null>(null);
  // The profile panel and the empty Structure area can be mounted together, so each export list
  // keeps its own element id.
  const exportListId = importOnly
    ? "structure-profile-export-list"
    : "profile-export-list";
  const [pendingImport, setPendingImport] = useState<
    BrowserImport | PendingPlan | null
  >(null);
  const [reading, setReading] = useState(false);
  const operation = useRef<AbortController | null>(null);
  usePhoneTabsOnClose(pendingImport !== null);
  const importDisabled = reading || pendingImport !== null;

  // A pending notice timer and a running file read never outlive the panel.
  useEffect(() => {
    return () => {
      operation.current?.abort();
      if (copyTimer.current !== null) clearTimeout(copyTimer.current);
    };
  }, []);

  const cancel = () => {
    setPendingImport(null);
    setConfirmError(null);
  };

  const acceptFile = async (file: File) => {
    const controller = new AbortController();
    operation.current = controller;
    setReading(true);
    setFileError(null);
    try {
      const result = await readImportFile(file, formats, controller.signal);
      if (controller.signal.aborted) return;
      if ("programmes" in result) {
        const resolved = await resolveExtractedPlan(
          result,
          cache,
          controller.signal,
        );
        if (controller.signal.aborted) return;
        setConfirmError(null);
        setPendingImport({
          kind: "plan",
          plan: resolved.plan,
          issues: resolved.issues,
        });
        return;
      }
      setConfirmError(null);
      setPendingImport(result);
    } catch (caught) {
      if (controller.signal.aborted) return;
      setFileError(asError(caught));
    } finally {
      operation.current = null;
      if (!controller.signal.aborted) setReading(false);
    }
  };

  const pick = (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    // An emptied value lets the same file be picked again.
    event.target.value = "";
    if (file) void acceptFile(file);
  };

  const drop = (event: DragEvent<HTMLLabelElement>) => {
    event.preventDefault();
    if (importDisabled) return;
    const files = event.dataTransfer.files;
    if (files.length !== 1) {
      setFileError(new Error("Choose one file"));
      return;
    }
    void acceptFile(files[0]);
  };

  /**
   * Copy the committed script through one path that works on a plain HTTP origin: select it in a
   * temporary textarea and copy the selection during the click. The element sits inside the open
   * panel, so the menu does not read its focus as a dismissal, and the element, the focus and the
   * previous selection are restored before the status is reported. A successful copy restarts the
   * notice lifetime.
   */
  const copySnippet = () => {
    if (!script) return;
    copyOpener.current =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    const selection = document.getSelection();
    const previous = selection
      ? Array.from({ length: selection.rangeCount }, (_, index) =>
          selection.getRangeAt(index),
        )
      : [];
    const hold = document.createElement("textarea");
    hold.className = "profile-snippet-hold";
    hold.setAttribute("readonly", "");
    hold.value = script.text;
    (copyButton.current?.parentElement ?? document.body).append(hold);
    hold.select();
    let copied = false;
    try {
      copied = document.execCommand("copy");
    } catch {
      copied = false;
    }
    hold.remove();
    selection?.removeAllRanges();
    previous.forEach((range) => selection?.addRange(range));
    copyOpener.current?.focus();
    setCopyStatus(copied);
    setCopyError(copied ? null : new Error("Could not copy import script"));
    if (copyTimer.current !== null) clearTimeout(copyTimer.current);
    copyTimer.current = copied
      ? setTimeout(() => setCopyStatus(false), PROFILE_NOTICE_MS)
      : null;
  };

  const confirm = () => {
    if (!pendingImport || "kind" in pendingImport) return;
    try {
      if (pendingImport.type === "course-explorer/state/3") {
        store.restoreState(pendingImport.state);
        window.location.reload();
        return;
      }
      if (overwrite && existingProfileId !== null)
        store.overwriteProfile(existingProfileId, pendingImport.profile.state);
      else store.importProfile(pendingImport.profile);
    } catch (caught) {
      setConfirmError(asError(caught));
      return;
    }
    cancel();
  };

  const importPlan = (name: string, existingProfileId: string | null) => {
    if (!pendingImport || !("kind" in pendingImport)) return;
    try {
      const target = prepareImportedProfile(pendingImport.plan, name, language);
      if (existingProfileId !== null)
        store.overwriteProfile(existingProfileId, target.state);
      else store.importProfile(target);
    } catch (caught) {
      setConfirmError(asError(caught));
      return;
    }
    cancel();
  };

  const pendingPlan =
    pendingImport !== null && "kind" in pendingImport ? pendingImport : null;
  const pendingProfile =
    pendingImport !== null &&
    "type" in pendingImport &&
    pendingImport.type === "course-explorer/profile/3"
      ? pendingImport.profile
      : null;
  const pendingState =
    pendingImport !== null &&
    "type" in pendingImport &&
    pendingImport.type === "course-explorer/state/3"
      ? pendingImport
      : null;
  const pendingName = pendingProfile ? pendingProfile.name.trim() : "";
  const existingProfileId = pendingName
    ? (Object.entries(profiles).find(
        ([, profile]) => profile.name === pendingName,
      )?.[0] ?? null)
    : null;
  const overwrite = existingProfileId !== null && pendingProfile !== null;

  const importSection: HelpSection = {
    heading: t("Import"),
    items: [
      {
        term: (
          <HelpLink href="/docs#guide-profiles/importing-a-file">
            {t("Import")}
          </HelpLink>
        ),
        text: t(
          "Choose or drop one file. A profile file creates a separate profile after confirmation, or overwrites the profile that already has its name. All saved data replaces every profile in this browser after confirmation. A plan file asks for a profile name, then places its choices and completions when Structure of studies loads its programmes. Cancel leaves saved data unchanged.",
        ),
      },
    ],
  };
  const formatSections: HelpSection[] = formats.map((format) => ({
    heading: t(format.label),
    items: [
      {
        term: <HelpLink href={format.guide}>{t(format.label)}</HelpLink>,
        text: t(format.help),
      },
    ],
  }));
  const scriptSection: HelpSection | null = script
    ? {
        heading: t(script.label),
        items: [
          {
            term: <HelpLink href={script.guide}>{t(script.label)}</HelpLink>,
            text: t(script.help),
          },
        ],
      }
    : null;
  const importHelp: readonly HelpSection[] = [
    importSection,
    ...formatSections,
    ...(scriptSection ? [scriptSection] : []),
  ];
  const help: readonly HelpSection[] = importOnly
    ? importHelp
    : [
        {
          heading: t("Profiles"),
          items: [
            {
              term: (
                <HelpLink href="/docs#guide-profiles/the-profile-control">
                  {t("Profiles")}
                </HelpLink>
              ),
              text: t(
                "A profile holds your language, selected and completed courses, and tab settings in this browser. A new profile starts as a copy of the open profile. Switching profiles leaves the open tab in place. Export a file to move your plan to another browser or device.",
              ),
            },
          ],
        },
        {
          heading: t("Export"),
          items: [
            {
              term: (
                <HelpLink href="/docs#guide-profiles/exporting-a-profile-or-the-saved-data">
                  {t("Export")}
                </HelpLink>
              ),
              text: t(
                "Open Export and select a profile to download it as a .json file. All saved data downloads every profile and setting together. Exporting changes no profile.",
              ),
            },
          ],
        },
        ...importHelp,
      ];
  const fileLabels = [
    t("a JSON profile export"),
    ...formats.map((format) => t(format.label)),
  ];
  const files =
    fileLabels.length === 1
      ? fileLabels[0]
      : `${fileLabels.slice(0, -1).join(", ")} ${t("or")} ${
          fileLabels[fileLabels.length - 1]
        }`;
  const accept = [
    ...new Set([".json", ...formats.flatMap((f) => f.suffixes)]),
  ].join(",");

  return (
    <div className="profile-transfer">
      {importOnly && (
        <h3 className="profile-transfer-heading">{t("Import")}</h3>
      )}
      <label
        className="profile-drop"
        onDrop={drop}
        onDragOver={(event) => event.preventDefault()}
      >
        <span className="profile-drop-text">
          <span className="profile-choose">{t("Choose file")}</span>{" "}
          <span className="profile-drop-note">
            {t("or drop {files}", { files })}
          </span>
        </span>
        <input
          className="profile-file"
          type="file"
          accept={accept}
          aria-label={t("Import")}
          disabled={importDisabled}
          onChange={pick}
        />
      </label>
      {exportError && <ErrorNotice error={exportError} language={language} />}
      {fileError && <ErrorNotice error={fileError} language={language} />}
      {importOnly ? (
        <div className="profile-transfer-actions profile-transfer-actions-import">
          {script && (
            <button
              className="profile-transfer-toggle"
              type="button"
              ref={copyButton}
              onClick={copySnippet}
            >
              {t("Copy import script")}
            </button>
          )}
          <button
            className="profile-transfer-toggle"
            type="button"
            aria-expanded={exportOpen}
            aria-controls={exportListId}
            onClick={() => setExportOpen((open) => !open)}
          >
            {t("Export")}
          </button>
          <HelpTip
            title={t("Import")}
            explain="Explain import"
            sections={help}
          />
        </div>
      ) : (
        <div className="profile-transfer-actions">
          <HelpTip
            title={t("Profiles")}
            explain="Explain profiles and import"
            sections={help}
          />
          <div className="profile-transfer-controls">
            {script && (
              <button
                className="profile-transfer-toggle"
                type="button"
                ref={copyButton}
                onClick={copySnippet}
              >
                {t("Copy import script")}
              </button>
            )}
            <button
              className="profile-transfer-toggle"
              type="button"
              aria-expanded={exportOpen}
              aria-controls={exportListId}
              onClick={() => setExportOpen((open) => !open)}
            >
              {t("Export")}
            </button>
          </div>
        </div>
      )}
      <AnimatePresence initial={false}>
        {copyStatus && (
          <ProfileNotice key="profile-copy-status">
            <p className="error-notice profile-copy-notice" role="status">
              {t("Import script copied")}
            </p>
          </ProfileNotice>
        )}
      </AnimatePresence>
      {copyError && <ErrorNotice error={copyError} language={language} />}
      <Disclosure open={exportOpen}>
        <div className="profile-export" id={exportListId}>
          <p className="profile-export-hint">
            {t("Select which file to export")}
          </p>
          {Object.entries(profiles).map(([id, profile]) => (
            <button
              className="profile-export-item"
              key={id}
              type="button"
              onClick={() =>
                runAction(() => downloadBrowserFile(store.exportProfile(id)))
              }
            >
              {profile.name}
            </button>
          ))}
          <button
            className="profile-export-item"
            type="button"
            onClick={() =>
              runAction(() => downloadBrowserFile(store.exportState()))
            }
          >
            {t("All saved data")}
          </button>
        </div>
      </Disclosure>
      <Dialog.Root
        open={pendingProfile !== null || pendingState !== null}
        onOpenChange={(open) => {
          if (open) return;
          cancel();
        }}
      >
        <Dialog.Portal>
          <Dialog.Overlay className="dialog-scrim" />
          <Dialog.Content
            className="dialog-panel dialog-panel-compact"
            aria-describedby={undefined}
          >
            {pendingProfile ? (
              <>
                <Dialog.Title>
                  {t("Import profile “{name}”?", {
                    name: pendingProfile.name,
                  })}
                </Dialog.Title>
                <p>
                  {overwrite
                    ? t(
                        "This replaces the saved data of the profile “{name}” and makes it current.",
                        { name: pendingProfile.name },
                      )
                    : t(
                        "This creates a new profile named “{name}” and makes it current.",
                        { name: pendingProfile.name },
                      )}
                </p>
              </>
            ) : (
              <>
                <Dialog.Title>{t("Replace all saved data?")}</Dialog.Title>
                <p>
                  {t(
                    "This replaces every profile in this browser with the imported saved data.",
                  )}
                </p>
              </>
            )}
            <AnimatePresence initial={false}>
              {overwrite && (
                <ProfileNotice key="profile-existing-name">
                  <ErrorNotice
                    error={new Error("A profile with that name already exists")}
                    language={language}
                  />
                </ProfileNotice>
              )}
              {confirmError && (
                <ProfileNotice key="profile-confirm-error">
                  <ErrorNotice error={confirmError} language={language} />
                </ProfileNotice>
              )}
            </AnimatePresence>
            <div className="dialog-foot">
              <Dialog.Close asChild>
                <button className="dialog-cancel" type="button">
                  {t("Cancel")}
                </button>
              </Dialog.Close>
              <button
                className="dialog-confirm"
                type="button"
                onClick={confirm}
              >
                {overwrite
                  ? t("Overwrite")
                  : pendingProfile
                    ? t("Import")
                    : t("Replace")}
              </button>
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
      {pendingPlan && (
        <ImportPlanDialog
          plan={pendingPlan.plan}
          issues={pendingPlan.issues}
          profiles={profiles}
          confirmError={confirmError}
          onImport={importPlan}
          onCancel={cancel}
        />
      )}
    </div>
  );
}
