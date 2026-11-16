/** The console's product and instance mark. */
import { wording } from "../shared/wording";

/** Render the product name and the site's configured instance display label beside it. */
export function BrandMark({ displayLabel }: { displayLabel: string }) {
  return (
    <span className="brand">
      <span className="brand-text">{wording.brand}</span>
      <span>/</span>
      <span className="brand-text">{displayLabel}</span>
    </span>
  );
}
