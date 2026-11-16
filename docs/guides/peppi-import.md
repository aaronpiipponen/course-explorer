# Importing a Peppi plan

A Peppi HOPS plan imports into course-explorer as one profile that holds the plan's programme, choices and completions.

The **Import study plan** dialog, the outcomes it names and the modules that wait for a version choice are described in [Importing a study plan](/docs/guides/importing-a-study-plan.md).

## Saving the HOPS page

1. Open your HOPS page in Peppi's **Tarkastelu** view.
2. Expand every part of the plan.
3. Right-click the page and choose **Save as**. Keep the default type, **Webpage, Complete**, and save the file.
4. Open the course-explorer profile menu. On desktop select the profile control in the top bar. On a phone open **More**, then select the profile row.
5. Drop the saved `.html` file onto the **Import** area, or select **Choose file** and pick it.

The saved page must show the programme heading above the plan structure. The heading reads the programme code and name separated by a comma, or the programme code alone, and the structure below it holds at least one module or course of the plan. A course that the page shows as completed is imported as completed. A page without that heading, or whose structure holds no module or course of the plan, is refused with **This file is not a saved Peppi HOPS page. Open the HOPS page (Tarkastelu) and save the page again.**

> [!WARNING]
> A saved HOPS page contains personal study data. Keep the file to yourself and do not share it casually.

## The script

1. Open your HOPS page in Peppi's **Tarkastelu** view.
2. Open the developer console of that browser tab.
3. Copy the script with **Copy import script** in the profile menu, or in the empty **Structure of studies** import area, or select the whole block under [The script](#the-script) and copy it.
4. Paste the script into the console and run it.

The script reads the open page and downloads one file named `peppi-hops-capture.json`. It makes no network request and names no university, so the same script works on every Peppi HOPS page. Import that file through the **Import** area, in the same way as a saved page.

> [!WARNING]
> A browser warns before the first paste into its console, because pasted code runs with the access of the open page. Read the whole script before you run it and check that it only reads the page and downloads one file.

```javascript
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
```

[Using the app on a phone](/docs/guides/phone.md) describes the **More** sheet that holds the profile control on a phone.
