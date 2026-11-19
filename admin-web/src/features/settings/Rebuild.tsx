/**
 * The Rebuild panel: the site rebuild action, the steps the command reported, and the
 * administration's separate build.
 *
 * The panel confirms the site rebuild once and shows pending for it as a whole rather than
 * inventing per-step progress. Only a successful rebuild renders an ordered step list, from the
 * steps the command itself reported, beside the bind it returned, and a refused one shows that
 * command's own failure. Directly underneath, the administration action confirms separately, warns
 * that it signs the operators out, and hands a successful build to the shell, which discards the
 * protected session in memory. One pending operation disables both actions. No step pulls code, and
 * the administration action returns with the backend's restart attached as a background task that
 * runs after the response body is sent.
 */
import { useState } from "react";
import { failureText, handleAdmissionFailure } from "../../api/http";
import { rebuildAdminPanel, rebuildSite } from "../../api/settings";
import type { RebuildReport } from "../../api/types";
import { ConfirmDialog } from "../../shared/ConfirmDialog";
import { FailureNotice, Panel, Row, Rows } from "../../shared/Panel";
import type { SessionAccess } from "../../shared/StagePanel";
import { wording } from "../../shared/wording";

/** One confirmed maintenance action of this panel. */
type Confirming = "app" | "admin";

/** Return the readable text of one step token the rebuild command reported. */
function stepText(step: string): string {
  const steps: Record<string, string> = wording.settings.rebuildSteps;
  return steps[step] ?? step;
}

/** Render the rebuild panel. */
export function Rebuild({
  session,
  onRefusal,
  onAdminRebuild,
}: {
  session: SessionAccess;
  onRefusal: (status: number) => void;
  onAdminRebuild: () => void;
}) {
  const detail = wording.settings;
  const [confirming, setConfirming] = useState<Confirming | null>(null);
  const [pending, setPending] = useState(false);
  const [report, setReport] = useState<RebuildReport | null>(null);
  const [failure, setFailure] = useState<string | null>(null);

  const onRebuild = async (): Promise<void> => {
    setPending(true);
    setFailure(null);
    setReport(null);
    try {
      setReport(await rebuildSite(session.csrfToken));
    } catch (error) {
      if (handleAdmissionFailure(error, onRefusal)) {
        return;
      }
      setFailure(failureText(error, wording.writeFailed));
    } finally {
      setPending(false);
    }
  };

  const onRebuildAdmin = async (): Promise<void> => {
    setPending(true);
    setFailure(null);
    try {
      await rebuildAdminPanel(session.csrfToken);
    } catch (error) {
      if (handleAdmissionFailure(error, onRefusal)) {
        return;
      }
      setFailure(failureText(error, wording.writeFailed));
      setPending(false);
      return;
    }
    // The answered build is complete, so the shell leaves this screen before the backend restarts.
    onAdminRebuild();
  };

  return (
    <Panel title={detail.rebuild}>
      <div className="acts">
        <button
          className="btn p"
          type="button"
          disabled={pending}
          onClick={() => setConfirming("app")}
        >
          {detail.rebuildApp}
        </button>
        <button
          className="btn"
          type="button"
          disabled={pending}
          onClick={() => setConfirming("admin")}
        >
          {detail.rebuildAdmin}
        </button>
      </div>
      {pending ? <p className="note">{detail.pending}</p> : null}
      {report === null ? null : (
        <>
          <ol className="steps">
            {report.steps.map((step) => (
              <li key={step}>
                <span className="dot" />
                <div>{stepText(step)}</div>
              </li>
            ))}
          </ol>
          <Rows>
            <Row label={detail.bind} value={`${report.host}:${report.port}`} />
          </Rows>
        </>
      )}
      <FailureNotice text={failure} />
      <ConfirmDialog
        open={confirming === "app"}
        title={detail.rebuildApp}
        message={detail.rebuildConfirm}
        pending={pending}
        onCancel={() => setConfirming(null)}
        onConfirm={() => {
          setConfirming(null);
          void onRebuild();
        }}
      />
      <ConfirmDialog
        open={confirming === "admin"}
        title={detail.rebuildAdmin}
        message={detail.rebuildAdminConfirm}
        pending={pending}
        onCancel={() => setConfirming(null)}
        onConfirm={() => {
          setConfirming(null);
          void onRebuildAdmin();
        }}
      />
    </Panel>
  );
}
