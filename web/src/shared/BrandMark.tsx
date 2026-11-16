/** The product mark, with the configured instance display label on the app surface. */
import { useWebFeatures } from "../app/useWebFeatures";
import { useWording } from "./useWording";

/** Render the product name and the site's configured instance display label beside it. */
function SiteBrandMark() {
  const { t } = useWording();
  const { display_label } = useWebFeatures();
  return (
    <span className="brand">
      <span className="brand-text">{t("course-explorer")}</span>
      <span>/</span>
      <span className="brand-text">{display_label}</span>
    </span>
  );
}

/** Render the product name and, when requested, the configured instance display label beside it. */
export function BrandMark({ site = false }: { site?: boolean }) {
  const { t } = useWording();
  if (site) return <SiteBrandMark />;
  return (
    <span className="brand">
      <span className="brand-text">{t("course-explorer")}</span>
    </span>
  );
}
