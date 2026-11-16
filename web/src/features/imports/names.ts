/**
 * Shared exact official-name comparison for a study-plan reader and its resolve step.
 *
 * The readers emit the same ligatures and whitespace runs the official print and page markup carry,
 * so both normalize a name here before comparing it with the exact Finnish or English official
 * name.
 */

/** Normalize extraction whitespace and ligatures for an exact official name comparison. */
export function normalizeImportedName(name: string): string {
  return name
    .replaceAll("ﬁ", "fi")
    .replaceAll("ﬂ", "fl")
    .replace(/\s+/g, " ")
    .trim();
}

/** Return whether one official name equals the extracted name after extraction normalization. */
export function sameOfficialName(
  module: { name_fi: string | null; name_en: string | null },
  name: string,
): boolean {
  const wanted = normalizeImportedName(name);
  return [module.name_fi, module.name_en].some(
    (candidate) =>
      candidate !== null && normalizeImportedName(candidate) === wanted,
  );
}
