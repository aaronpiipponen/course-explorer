/** Independent-project notice and curriculum source line pinned to the bottom of the application shell. */
import { useLayoutEffect, useRef } from "react";
import { useWebFeatures } from "../app/useWebFeatures";
import { useWording } from "../shared/useWording";

/** Render the application notice and curriculum source line. */
export function AppFooter({
  measures = true,
}: {
  /** Whether this copy reports its height to the document root. Only one copy may. */
  measures?: boolean;
}) {
  const { language, t } = useWording();
  const { source_attribution, university_names } = useWebFeatures();
  const footer = useRef<HTMLElement>(null);
  // The notice stack sits above the footer, so its measured height is a root variable. A hidden
  // footer measures zero and the phone rule uses its own bottom offset.
  useLayoutEffect(() => {
    const element = footer.current;
    if (!element || !measures) return;
    const root = document.documentElement;
    const measure = () =>
      root.style.setProperty(
        "--app-footer-height",
        `${element.offsetHeight}px`,
      );
    measure();
    if (typeof ResizeObserver === "undefined")
      return () => root.style.removeProperty("--app-footer-height");
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => {
      observer.disconnect();
      root.style.removeProperty("--app-footer-height");
    };
  }, [measures]);
  return (
    <footer className="app-footer" ref={footer}>
      <span className="app-footer-notice">
        {t(
          "This is an independent project that is not affiliated with {university}.",
          {
            university: university_names[language],
          },
        )}
      </span>
      <div className="app-footer-source">{source_attribution[language]}</div>
    </footer>
  );
}
