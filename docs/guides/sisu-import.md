# Importing a Sisu plan

A Sisu plan imports into course-explorer as one profile that holds the plan's programmes, choices and completions.

The **Import study plan** dialog, the outcomes it names and the modules that wait for a version choice are described in [Importing a study plan](/docs/guides/importing-a-study-plan.md).

## Saving the plan page

1. Open your plan in Sisu's **Structure of studies** view. In the Finnish interface the view is **Opintojen rakenne**.
2. Expand every module of the plan, including both degrees under its education.
3. Right-click the page and choose **Save as**. Keep the default type, **Webpage, Complete**, and save the file.
4. Open the course-explorer profile menu. On desktop select the profile control in the top bar. On a phone open **More**, then select the profile row.
5. Drop the saved `.html` file onto the **Import** area, or select **Choose file** and pick it.

A page that is not a fully expanded Sisu plan page is refused with **This file is not a saved Sisu plan page with every module expanded. Open the plan in Sisu's Structure of studies, expand every module, and save the page again.**

## Printing the plan

1. Open your plan in Sisu's **Structure of studies** view.
2. Choose **⋯** beside **New study plan**, choose **Print study plan**, and save the print as a PDF. In the Finnish interface the path is **Opintojen rakenne** → **⋯** beside **Uusi opintosuunnitelma** → **Tulosta opintosuunnitelma**, then save the print as a PDF.
3. Import the PDF the same way as a saved page.

The import accepts the Finnish and English prints of one plan, and it reads the print layout of the Sisu structure view. A scan and an image-only PDF hold no plan text to read, and an encrypted PDF cannot be decoded, so each of them is refused with **Invalid Sisu PDF plan**. The PDF is read in this browser, and its bytes, text and file details are not uploaded.

> [!WARNING]
> A Sisu plan PDF and a saved plan page contain personal identity data. Keep the file to yourself and do not share it casually.

## The script

1. Open your plan in Sisu's **Structure of studies** view.
2. Expand every module of the plan, including both degrees under its education. The script refuses the download while any module is collapsed.
3. Open the developer console of that browser tab.
4. Copy the script with **Copy import script** in the profile menu, or in the empty **Structure of studies** import area, or select the whole block under [The script](#the-script) and copy it.
5. Paste the script into the console and run it.

The script reads the open page and downloads one file named `sisu-plan-capture.json`. It makes no network request and names no university, so the same script works on every Sisu plan page. Import that file through the **Import** area, in the same way as a saved page.

> [!WARNING]
> A browser warns before the first paste into its console, because pasted code runs with the access of the open page. Read the whole script before you run it and check that it only reads the page and downloads one file.

```javascript
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
```

[Using the app on a phone](/docs/guides/phone.md) describes the **More** sheet that holds the profile control on a phone.
