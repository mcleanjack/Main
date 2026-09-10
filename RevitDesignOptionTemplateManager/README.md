# Design Option <-> View Template Bulk Manager

A Dynamo/Revit tool to bulk-apply a Design Option setting across many views
at once by picking the View Template(s) those views use, instead of opening
each view's Visibility/Graphics Overrides one at a time.

**Read section 1 before using this.** The tool's actual mechanism is not
"set it on the View Template" — the Revit API doesn't support that. It's
explained in full below, and it's also written directly into the comment
block at the top of the Python file.

---

## 1. Verified API findings — read this first

The original ask was: select View Templates, pick a Design Option, and have
the *templates* force that option on every view they control. Before
building anything, the Revit API was checked against this ask. Here is what
is actually true, each point independently checked:

| Question | Finding |
|---|---|
| Can a View Template store/force a specific Design Option via the API? | **No.** The View Template dialog in the Revit UI does have a "V/G Overrides Design Options" row a human can include/lock, but there is no documented API property or method to read or set which option a *template* forces, or to toggle that row's included/excluded state. Autodesk's own developer community states plainly that "the VG settings for Design Options are not currently accessible for use in custom programming." |
| Is there a per-VIEW equivalent? | **Yes.** The Visibility/Graphics Overrides dialog's "Design Options" tab (one row per Design Option Set, each a dropdown of "Automatic" or a specific option) is a genuine per-view override, exposed as `BuiltInParameter.VIEWER_OPTION_VISIBILITY` ("Visible in Option") on the `View` object. It is a normal, gettable/settable `Parameter` with `StorageType.ElementId`. |
| Is that per-view parameter always present? | **No.** Independently confirmed to work reliably on plan-type views; known to be absent/non-functional specifically on true Elevation-type views (Sections work fine); never present on schedules, legends, or sheets. This tool checks for it on every view and skips cleanly wherever it's missing — it never assumes. |
| Is `VIEWER_OPTION_VISIBILITY` an officially documented, guaranteed API? | **Not fully.** There is a long-standing, still-open Autodesk "Revit Idea" (feature request) titled "API to get/set View Overrides for Design Options," asking Autodesk to formalize exactly this. That confirms the parameter-level access works in practice, but it isn't a guaranteed, first-class API — which is why this tool treats every read/write of it defensively (try/except, read-only checks, presence checks) rather than assuming success. |
| Does `Element.DesignOption` (used in the companion View Selector tool) help here? | **No** — that property tells you which option an *element* (or, rarely, a view) was itself created inside. It is not the mechanism that controls what a view *displays*, and doesn't correspond to the VG "Design Options" tab at all. |
| If a template locks "Design Options" (included in its Include list), can the API unlock just that one row? | **Yes.** `View.GetTemplateParameterIds()` / `GetNonControlledTemplateParameterIds()` / `SetNonControlledTemplateParameterIds()` are a real, documented API surface for toggling which rows a template controls — called on the template element itself. Adding the "Visible in Option" parameter's Id to the non-controlled set is the exact API equivalent of a human unchecking "Design Options" in Manage View Templates. This does NOT let you set *which option the template's row shows* (that part is still UI-only) — it only unlocks the row so the views underneath become independently editable. |

### Conclusion and what this tool actually does

Because the Revit API cannot store or apply *which option* a View Template
forces, **this tool always applies the Design Option setting directly to
every ordinary view that uses the selected template(s)**, which produces
exactly the end result the user wants: every one of those views ends up
showing the chosen Design Option. It never touches any other View Template
or view setting (Visibility/Graphics, Filters, Object Styles, Detail Level,
Discipline, Phase, Phase Filter, Scale, Annotation, Crop, Worksets, View
Range, linked model settings — all untouched).

If a view's own copy of this parameter is locked (`IsReadOnly == True`)
because its View Template *does* have "Design Options" included/checked in
the Revit UI, there's a checkbox in the dialog — **"Unlock 'Design Options'
on templates that currently lock it"**, on by default — that makes the
tool unlock just that one Include row on the template (via
`SetNonControlledTemplateParameterIds`, see the table above) before setting
the views' values. Turn the checkbox off to keep the tool from touching
templates at all; locked views are then reported as skipped, with the
reason, instead.

If a view has active overrides for **more than one** Design Option Set at
once, the API gives no documented way to tell which override belongs to
which set. The tool disambiguates by matching a parameter whose *current*
value already belongs to the target set; if it can't confidently identify
one, it skips that view and says so, rather than risk changing the wrong
Design Option Set's visibility.

---

## 2. Building the graph in Dynamo

1. Open Revit → `Manage` (or `Automate`) → `Dynamo`.
2. `File → New`.
3. Add a **Boolean** node, set it to `False`, rename it `Run`.
4. Add a **Python Script** node.
5. Paste the entire contents of `DesignOptionTemplateManager.py` into it.
6. Wire `Run`'s output into the Python node's `IN[0]`.
7. Optionally add **Watch** nodes on `OUT[1]` (modified count), `OUT[3]`
   (per-template report), and `OUT[4]` (status message).
8. Save as `DesignOptionTemplateManager.dyn`.

Graph shape: **Boolean → Python Script → (Watch nodes)** — same minimal
pattern as the companion View Selector tool.

---

## 3. Running it in Revit

1. Open the target project.
2. Open the saved graph in Dynamo.
3. Set `Run` to `True` and run the graph.
4. A dialog titled **"Design Option <-> View Template Manager"** appears
   (check behind the Dynamo window if you don't see it):
   - Check the View Templates to modify (Select All / Clear All available).
   - Choose a Design Option Set.
   - Choose a Design Option (or "Main Model" to clear back to Automatic) —
     this list repopulates automatically when you change the Set.
   - Leave **"Unlock 'Design Options' on templates that currently lock
     it"** checked (default) if you want templates that currently lock the
     setting to be unlocked automatically so their views update too.
     Uncheck it if you'd rather the tool never touch a template and just
     skip any view it locks.
   - Click **Preview Changes >>**.
5. A confirmation screen shows exactly what will happen — the templates,
   the target Design Option, whether locked templates will be unlocked,
   and a plain-language note that no other view/template setting is being
   touched. Click **APPLY CHANGES**
   to proceed, **<< Back** to change your selections, or **Cancel** to
   abort with nothing modified.
6. Revit applies the change in a single Transaction (one Undo step) and the
   dialog closes. Check the Watch node(s) for the results.

Re-run any time by toggling `Run` and running again.

---

## 4. Result reporting

`OUT[3]` (per-template report) gives one line per selected template, e.g.:

```
Floor Plan - Construction: 4 of 4 views updated.
RCP - Construction: 2 of 2 views updated.
Section - Construction: unlocked 'Design Options' (was locked by this template's Include list).
Section - Construction: 1 of 3 views updated (partial).
    skipped 'Section - Elevation Check': Design Option visibility does not apply to this view (e.g. schedule, legend, or unsupported view type).
    skipped 'Section - Detail Callout': Design Option visibility does not apply to this view (e.g. schedule, legend, or unsupported view type).
3D - Presentation: 0 of 1 views updated. Skipped.
    skipped '3D - Presentation View': Design Option visibility does not apply to this view (e.g. schedule, legend, or unsupported view type).
```

(The "unlocked 'Design Options'..." line only appears when the unlock
checkbox was on and a selected template actually was locked. With the
checkbox off, a locked view is instead reported with the reason `Locked
by this view's View Template (Design Options is an included/controlled
parameter)...`.)

`OUT[4]` (status message) gives the one-line summary, e.g.:

```
"3 View Template(s) fully updated, 1 skipped. Target: Facade Options -> Option 2."
```

`OUT[0]`/`OUT[1]`/`OUT[2]` give the raw template list, fully-updated count,
and skipped count respectively, for anything downstream you want to wire
into further Dynamo logic.

---

## 5. Error handling built in

- No View Templates in the project → clear status message, tool exits
  before showing the dialog.
- No Design Option Sets in the project → same.
- No View Templates selected, or no Design Option Set/Option chosen →
  caught in the dialog itself (`MessageBox` prompt), Preview cannot proceed.
- A view whose `VIEWER_OPTION_VISIBILITY` parameter doesn't exist → skipped
  with an explicit reason, never an exception.
- A view whose parameter is read-only (locked by its template's own
  Design Options include) → if the unlock checkbox is on, the tool
  unlocks that one Include row on the template first and proceeds; if
  it's off, or the unlock attempt itself fails, the view is skipped with
  an explicit reason instead of throwing.
- A view with ambiguous multi-Design-Option-Set overrides → skipped with an
  explicit reason rather than guessed.
- A deleted/invalid view or template encountered mid-scan → caught per-item
  and logged; the loop continues.
- Any unexpected Revit API exception during the transaction → the
  transaction is rolled back (`Transaction.RollBack()`), nothing is left
  half-applied, and the tool reports a safe status message with the full
  traceback in the debug output.
- User clicks Cancel at either dialog stage → no Transaction is ever
  started, nothing is modified.

---

## 6. Test cases

### Critical test case (from the brief)

Project has View Templates `Floor Plan - Construction`, `RCP -
Construction`, `Section - Construction`, `Elevation - Construction`, `3D -
Construction`; Design Option Set `Facade Options` with options `Main
Model`, `Option 1`, `Option 2`.

- Select `Floor Plan - Construction`, `RCP - Construction`, `Section -
  Construction`; pick `Facade Options -> Option 2`; Apply.
- **Expected:** every ordinary view using those three templates gets its
  `VIEWER_OPTION_VISIBILITY` parameter set to `Option 2`'s Id. `Elevation -
  Construction` and `3D - Construction` are untouched (not selected). No
  other setting on any of the five templates or any view changes.
- **Caveat to test for real:** if any of the views under `Section -
  Construction` are true `Elevation` ViewType (unlikely under a Section
  template, but confirm), verify whether the parameter is present — this
  is the one known view-type gap in the underlying API mechanism.

### Additional scenarios

| # | Scenario | Expected result |
|---|----------|------------------|
| 1 | Single View Template, single option | All views on that template updated; report shows 1 template, N views |
| 2 | All View Templates selected via "Select All" | Every template with at least one controlled view is processed; empty templates reported as "no views currently use this template" |
| 3 | "Main Model" chosen instead of an option | Matching views' override is cleared (`InvalidElementId`); already-cleared views reported as "unchanged" (still counted as success) |
| 4 | A selected template controls zero views | Reported per-template as "nothing to modify", not an error |
| 5 | A selected template has "Design Options" locked/included in the Revit UI, unlock checkbox ON (default) | The template's "Design Options" Include row is unchecked (reported explicitly), then its views update normally; no other Include row changes |
| 5b | Same, but unlock checkbox OFF | Its views are skipped individually with the "Locked by this view's View Template" reason; the template's Include list is never touched; other selected templates still proceed normally |
| 6 | A view under a selected template is a Schedule/Legend/Sheet-adjacent type with no such parameter | Skipped with "does not apply to this view" reason, never an exception |
| 7 | Cancel at the first dialog | No dialog 2 shown, no transaction started, status = "Cancelled by user." |
| 8 | Cancel at the confirmation dialog | Same as above — nothing applied |
| 9 | Click "<< Back" from confirmation | Returns to the selection dialog with the same templates/Set/Option still selected |
| 10 | Project with multiple Design Option Sets, and a view already overridden for a *different* set than the one being changed | That view's unrelated Design Option Set override is left completely alone; only the parameter matching the target set (if identifiable) is touched |

---

## 7. Future expansion

The Python file is written with one clearly marked section per "control"
(currently just Design Option visibility). To add Phase, Phase Filter,
Discipline, Detail Level, View Range, etc. later:

1. Write parallel `resolve_xxx_parameter(view, target)` and
   `apply_xxx_to_view(view, target)` functions following the same shape as
   `resolve_design_option_parameter` / `apply_design_option_to_view`.
2. Reuse `get_views_using_template()` unchanged — it already returns every
   ordinary view controlled by a given template.
3. Add a corresponding tab or control to `SelectionDialog`, and a matching
   branch in the main execution loop's per-template processing.

Nothing about the template discovery, the Preview/Confirm flow, the
Transaction handling, or the reporting format needs to change.
