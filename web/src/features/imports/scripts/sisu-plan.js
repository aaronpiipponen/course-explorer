// Download one capture of the open Sisu plan page.
//
// Run this script in the browser console on the Structure of studies page with every module
// expanded. It writes sisu-plan-capture.json with the plan tree markup. It reads the open page and
// makes no network request.
(() => {
  if (!document.querySelector("plan-module"))
    throw new Error("Open the Structure of studies page first.");
  if (document.querySelector('plan-module .collapse[aria-expanded="false"]'))
    throw new Error("Expand every module of the plan first.");

  const markup = [...document.querySelectorAll("plan-module")]
    .filter((module) => module.parentElement.closest("plan-module") === null)
    .map((module) => module.outerHTML)
    .join("");

  const file = { type: "course-explorer/sisu/1", markup };
  const link = document.createElement("a");
  link.href = URL.createObjectURL(
    new Blob([JSON.stringify(file)], { type: "application/json" }),
  );
  link.download = "sisu-plan-capture.json";
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 0);
})();
