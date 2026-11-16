/**
 * The one study-plan import dialog: the profile name, the plan's programmes and every item the
 * import left out or is waiting on.
 *
 * The dialog owns the name field and the overwrite decision. The profile and whole-state
 * confirmations stay in the profile transfer surface.
 */
import { useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { AnimatePresence } from "framer-motion";
import type { Profile } from "../../state/types";
import { ErrorNotice } from "../../shared/ErrorNotice";
import { useWording } from "../../shared/useWording";
import { ProfileNotice } from "../profiles/ProfileNotice";
import { ImportIssueItem, itemLabel } from "./ImportNotice";
import type { ImportIssue, ImportedPlan } from "./plan";

/** Render the import dialog for one resolved plan. */
export function ImportPlanDialog({
  plan,
  issues,
  profiles,
  confirmError,
  onImport,
  onCancel,
}: {
  plan: ImportedPlan;
  issues: readonly ImportIssue[];
  profiles: Record<string, Profile>;
  confirmError: Error | null;
  onImport(name: string, existingProfileId: string | null): void;
  onCancel(): void;
}) {
  const { language, t } = useWording();
  const [name, setName] = useState("");
  const trimmed = name.trim();
  const existingProfileId =
    trimmed === ""
      ? null
      : (Object.entries(profiles).find(
          ([, profile]) => profile.name === trimmed,
        )?.[0] ?? null);
  const overwrite = existingProfileId !== null;
  const waiting = issues.filter(
    (issue) => issue.reason === "version_choice_pending",
  );
  const omitted = issues.filter(
    (issue) => issue.reason !== "version_choice_pending",
  );
  return (
    <Dialog.Root
      open
      onOpenChange={(open) => {
        if (!open) onCancel();
      }}
    >
      <Dialog.Portal>
        <Dialog.Overlay className="dialog-scrim" />
        <Dialog.Content
          className="dialog-panel dialog-panel-compact"
          aria-describedby={undefined}
        >
          <Dialog.Title>{t("Import study plan")}</Dialog.Title>
          <label htmlFor="plan-profile-name">{t("Profile name")}</label>
          <input
            id="plan-profile-name"
            className="profile-rename-input"
            value={name}
            autoComplete="off"
            autoFocus
            onChange={(event) => setName(event.target.value)}
          />
          <p>
            {overwrite
              ? t(
                  "This replaces the saved data of the profile “{name}” with these programmes: {names}.",
                  {
                    name: trimmed,
                    names: plan.programmes
                      .map((programme) => programme.name)
                      .join(", "),
                  },
                )
              : t(
                  "This creates a new profile with these programmes: {names}.",
                  {
                    names: plan.programmes
                      .map((programme) => programme.name)
                      .join(", "),
                  },
                )}
          </p>
          {waiting.length > 0 && (
            <ul className="import-omitted">
              {waiting.map((issue, index) => (
                <li key={`waiting:${issue.code ?? ""}:${issue.name}:${index}`}>
                  <ImportIssueItem
                    label={itemLabel(issue)}
                    reason={issue.reason}
                  />
                </li>
              ))}
            </ul>
          )}
          {omitted.length > 0 && (
            <>
              <p>{t("Left out of the import:")}</p>
              <ul className="import-omitted">
                {omitted.map((issue, index) => (
                  <li key={`${issue.code ?? ""}:${issue.name}:${index}`}>
                    <ImportIssueItem
                      label={itemLabel(issue)}
                      reason={issue.reason}
                    >
                      {issue.candidates && issue.candidates.length > 0 && (
                        <div>
                          <p>{t("Matching official items:")}</p>
                          <ul>
                            {issue.candidates.map((candidate) => (
                              <li key={candidate.groupId}>
                                {candidate.code ? `${candidate.code} ` : ""}
                                {candidate.name} ({candidate.groupId})
                              </li>
                            ))}
                          </ul>
                        </div>
                      )}
                    </ImportIssueItem>
                  </li>
                ))}
              </ul>
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
              disabled={trimmed === ""}
              onClick={() => onImport(trimmed, existingProfileId)}
            >
              {overwrite ? t("Overwrite") : t("Import")}
            </button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
