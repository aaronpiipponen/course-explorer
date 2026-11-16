// Download one capture of the open Peppi HOPS page.
//
// Run this script in the browser console on the HOPS page (Tarkastelu). It writes
// peppi-hops-capture.json with the programme heading and the plan structure markup. It reads the
// open page and makes no network request.
(() => {
  const structure = document.querySelector("#structure");
  const heading = document.querySelector("#header-row-div h2");
  if (!structure || !heading)
    throw new Error("Open the HOPS page (Tarkastelu) first.");

  const file = {
    type: "course-explorer/peppi-hops/1",
    programme: heading.textContent.trim(),
    markup: structure.outerHTML,
  };
  const link = document.createElement("a");
  link.href = URL.createObjectURL(
    new Blob([JSON.stringify(file)], { type: "application/json" }),
  );
  link.download = "peppi-hops-capture.json";
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 0);
})();
