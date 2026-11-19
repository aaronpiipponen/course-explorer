/** The Settings tab's fixed configuration, backup and maintenance requests. */
import { runCommand } from "./http";
import type {
  AdminRebuildReport,
  BackupDeletionReport,
  BackupListReport,
  ConfigurationFileReport,
  ConfigurationFilesReport,
  ConfigurationWriteReport,
  ProvisioningReport,
  RebuildReport,
} from "./types";

/** The one file the feature switches live in. */
export const WEB_CONFIGURATION_FILE = "config/web.toml";

/** List the editable configuration files. */
export function readConfigurationFiles(
  signal?: AbortSignal,
  automatic = false,
): Promise<ConfigurationFilesReport> {
  return runCommand<ConfigurationFilesReport>("/api/settings/config/files", {
    signal,
    automatic,
  });
}

/** Read every key of one listed configuration file as the file holds it. */
export function readConfigurationFile(
  file: string,
  csrfToken: string,
  signal?: AbortSignal,
  automatic = false,
): Promise<ConfigurationFileReport> {
  return runCommand<ConfigurationFileReport>("/api/settings/config/read", {
    body: { file },
    csrfToken,
    signal,
    automatic,
  });
}

/** Save one key of one listed configuration file from its JSON literal value. */
export function setConfigurationValue(
  file: string,
  key: string,
  value: string,
  csrfToken: string,
  signal?: AbortSignal,
): Promise<ConfigurationWriteReport> {
  return runCommand<ConfigurationWriteReport>("/api/settings/config/set", {
    body: { file, key, value },
    csrfToken,
    signal,
  });
}

/** List the regular files under the backup root. */
export function readBackups(
  signal?: AbortSignal,
  automatic = false,
): Promise<BackupListReport> {
  return runCommand<BackupListReport>("/api/settings/backups", {
    signal,
    automatic,
  });
}

/** Delete one listed backup file. */
export function deleteBackup(
  name: string,
  csrfToken: string,
  signal?: AbortSignal,
): Promise<BackupDeletionReport> {
  return runCommand<BackupDeletionReport>("/api/settings/backups/delete", {
    body: { name },
    csrfToken,
    signal,
  });
}

/**
 * Build the site image, re-create the database and embedding containers, re-create the site
 * container and prove its loopback bind.
 */
export function rebuildSite(
  csrfToken: string,
  signal?: AbortSignal,
): Promise<RebuildReport> {
  return runCommand<RebuildReport>("/api/settings/rebuild", {
    body: {},
    csrfToken,
    signal,
  });
}

/**
 * Build the administration frontend, whose successful response queues the backend's own restart.
 *
 * The request is an explicit action, so it carries no automatic-read marker. The final ASGI body
 * send completes before the restart is queued, and no restart follows a failed build.
 */
export function rebuildAdminPanel(
  csrfToken: string,
  signal?: AbortSignal,
): Promise<AdminRebuildReport> {
  return runCommand<AdminRebuildReport>("/api/settings/rebuild-admin", {
    body: {},
    csrfToken,
    signal,
  });
}

/** Update the worker code over one origin branch. */
export function setupWorker(
  branch: string,
  csrfToken: string,
  signal?: AbortSignal,
): Promise<ProvisioningReport> {
  return runCommand<ProvisioningReport>("/api/settings/worker/setup", {
    body: { branch },
    csrfToken,
    signal,
  });
}

/** Read the worker's prepared code and resource readiness. */
export function preflightWorker(
  csrfToken: string,
  signal?: AbortSignal,
): Promise<ProvisioningReport> {
  return runCommand<ProvisioningReport>("/api/settings/worker/preflight", {
    body: {},
    csrfToken,
    signal,
  });
}
