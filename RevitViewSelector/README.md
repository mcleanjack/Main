# Revit View Selector — by View Template + Design Option

A small, single-purpose Dynamo tool for Revit: pick a View Template and/or a
Design Option from real dropdowns, and the tool selects every matching view
in the project so you can act on them (put on sheets, batch-print, tag,
review, etc.). It never edits the model — it only reads views and changes
the active Revit selection.

Contents of this folder:

- `ViewSelectorByTemplateAndOption.py` — the entire tool, meant to be pasted
  into one Dynamo **Python Script** node.
- `README.md` — this file.

---

## 1. Why a pop-up dialog instead of Dynamo dropdown nodes

Out-of-the-box Dynamo has no built-in node for "a dropdown populated with an
arbitrary list of strings" — that UX normally comes from a third-party
package (e.g. Data-Shapes). Since the brief asked to avoid third-party
package dependencies where possible, this tool instead pops up a small
native Windows dialog (built with `System.Windows.Forms`, which ships with
.NET/Revit — no package install required) containing:

- A **View Template** dropdown (all templates in the project + `<None / Any>`)
- A **Design Option** dropdown (all design options, labeled with their
  Design Option **Set** so identically-named options aren't ambiguous, +
  `<None / Any>`)
- Three radio buttons for match mode (AND / Template only / Option only)
- A **Select Matching Views** button and a **Cancel** button

This keeps the actual Dynamo graph trivial (2–3 nodes) while still giving a
real, professional-feeling picker UI — matching the requested workflow:
open Dynamo → run → dialog appears → pick options → click the button →
Revit selects the views.

---

## 2. Building the graph in Dynamo

This takes about a minute:

1. Open Revit, go to **Manage → Dynamo** (or **Automate → Dynamo** depending
   on your Revit version) to launch Dynamo.
2. **File → New** to start a blank graph.
3. In the node search box, add a **Boolean** node. Set it to `False`. This is
   your "Run" switch — rename it (right-click → Rename Node) to `Run` for
   clarity.
4. In the node search box, add a **Python Script** node.
5. Open the Python Script node, delete its default contents, and paste in
   the entire contents of `ViewSelectorByTemplateAndOption.py`.
6. Wire the **Boolean (Run)** node's output into **IN[0]** of the Python
   Script node. (If you don't wire anything to IN[0], the tool defaults to
   running anyway — but wiring an explicit Boolean gives you a clean
   on/off "Run" switch and stops it firing by accident when the graph
   loads.)
7. Add **Watch** nodes (or just leave the outputs unwired if you don't need
   to see them) for the outputs you care about. The Python node exposes 6
   outputs, in this order:
   - `OUT[0]` — list of matching View elements
   - `OUT[1]` — number of matching views
   - `OUT[2]` — names of matching views
   - `OUT[3]` — Element Ids of matching views (as plain integers)
   - `OUT[4]` — status message, e.g. `"23 views selected."`
   - `OUT[5]` — debug info (counts, exclusions, any per-view errors)

   A clean minimal setup: wire `OUT[4]` (status) to one Watch node, and
   `OUT[1]` (count) to another. Leave `OUT[5]` (debug) unwired unless you're
   troubleshooting.
8. Save the graph, e.g. as `RevitViewSelector.dyn`.

That's the whole graph: **Boolean → Python Script → (Watch nodes)**.

---

## 3. Running it in Revit

1. Open the target Revit project (the one whose views you want to select).
2. **Manage → Dynamo**, then open `RevitViewSelector.dyn`.
3. Set the **Run** Boolean node to `True` (click it).
4. Run the graph (Dynamo's Run button, or Automatic mode).
5. A dialog titled **"Select Views by Template + Design Option"** appears
   (it can occasionally open behind the Dynamo window — check your
   taskbar/Alt-Tab if you don't see it immediately).
6. Choose a View Template, a Design Option, and a match mode.
7. Click **Select Matching Views**.
8. The dialog closes, the graph finishes, and Revit's active selection now
   contains every matching view. Switch to the Revit window — the views are
   selected (e.g. visible as highlighted rows if you have the Project
   Browser open, or ready for whatever you do next with a selection, such
   as **Modify | Views → Create → Sheet**, tagging, or exporting).
9. Check the Watch node(s) for the status message and count.

To run again with different criteria, just toggle the Boolean node (e.g.
False then True) and re-run — no need to rebuild anything.

---

## 4. Revit API notes: Design Options and Views

A few things about the API that shaped how this tool works, worth knowing
if you extend it:

- **Design Options apply to elements first, views only incidentally.**
  `Element.DesignOption` is a property on the base `Element` class (which
  `View` inherits from), so *every* element — including every view — has
  it available. But it is only populated when the element/view was created
  while a specific Design Option was active. In practice, the overwhelming
  majority of views in a project (floor plans, sections, elevations, 3D
  views created the normal way) will have `view.DesignOption == null`,
  because views themselves aren't "placed" the way walls or furniture are.
  This tool never assumes the value is present — it always checks for
  `null` first, and views with no Design Option simply won't match a
  filter that requires one.
- **Views don't expose Design Option as a Parameter you can read with
  `get_Parameter(...)`** the way `BuiltInParameter.DESIGN_OPTION_ID` works
  for ordinary model elements. `Element.DesignOption` is the reliable,
  documented way to read it generically across element types, so that's
  what this tool uses.
- **Design Option → Design Option Set** is not a direct property on
  `DesignOption` in older API versions; it's read via the
  `BuiltInParameter.OPTION_SET_ID` parameter on the `DesignOption` element,
  resolved back to the `DesignOptionSet` element with
  `Document.GetElement`. The tool wraps this in a try/except and falls
  back to `"Unknown Option Set"` if that parameter isn't available in a
  given Revit version, rather than failing.
- **View Templates are compared by `ElementId`, not name.** `View.
  ViewTemplateId` returns `ElementId.InvalidElementId` when no template is
  assigned — the tool treats that as "no template," never as an error.
  Comparing by Id (rather than by template name) means two templates that
  happen to share a name are never confused, and a renamed template still
  matches correctly.
- **Not all "views" are selectable, normal project views.** `View
  Templates` themselves are `View` elements with `IsTemplate == True` and
  are excluded. Schedules, sheets, and internal/system browser views
  (`ViewType.Schedule`, `PanelSchedule`, `ColumnSchedule`, `DrawingSheet`,
  `Internal`, `ProjectBrowser`, `SystemBrowser`, `Undefined`) are excluded
  because they either don't behave like normal graphical views for
  selection purposes, don't carry a meaningful Design Option/Template
  relationship, or aren't valid targets for this workflow. Floor plans,
  ceiling plans, sections, elevations, 3D views, detail views, drafting
  views, legends, area plans, engineering plans, walkthroughs, and
  renderings are all included.
- **`ElementId.IntegerValue` vs `ElementId.Value`.** Revit 2024 introduced
  `ElementId.Value` (an `int64`) and marked `IntegerValue` obsolete (and it
  is expected to eventually disappear). The tool's `eid_to_int()` helper
  tries `.Value` first and falls back to `.IntegerValue`, so the same
  script works across old and new Revit/API versions without editing.
- **Selecting views never requires a Transaction.** `UIDocument.Selection.
  SetElementIds()` changes the active UI selection, not the document, so
  this tool never opens a Revit `Transaction` — consistent with "select
  only, never modify."

---

## 5. Error handling built in

- No View Templates in the project → the dropdown just shows
  `<None / Any>` and nothing else; the tool still runs.
- No Design Options in the project → same as above for that dropdown.
- A view with no assigned template, or no Design Option → treated as
  "None," never throws — it simply won't satisfy a filter that requires a
  specific template/option.
- A deleted/invalid element encountered while scanning → caught per-view
  and logged to the debug output; the loop continues with the next view
  instead of aborting.
- No views match → status is exactly `"No views match the selected
  criteria."` and the active selection is cleared (set to an empty list),
  never left stale.
- Any unexpected Revit API exception anywhere in the run → caught by a
  top-level `try/except`; the tool reports a safe status message and puts
  the full traceback into the debug output rather than crashing the
  Dynamo graph.
- User clicks **Cancel** in the dialog → reported as cancelled, and the
  current Revit selection is left untouched.

---

## 6. Suggested test matrix

Run these against a real project with a mix of templated/non-templated
views and at least one populated Design Option Set:

| # | Scenario | Setup | Expected result |
|---|----------|-------|------------------|
| 1 | View Template only | Pick a real template, Design Option = `<None/Any>`, mode = Template only | All views using that template are selected, regardless of design option |
| 2 | Design Option only | Pick a real option, Template = `<None/Any>`, mode = Option only | All views belonging to that Design Option are selected (likely a small or empty set — see note below) |
| 3 | Template + Option (AND) | Pick both a template and an option, mode = AND | Only views satisfying both are selected |
| 4 | No matches | Pick a template/option combination nothing satisfies | Status = `"No views match the selected criteria."`, selection cleared, count = 0 |
| 5 | Views without a View Template | Include ordinary views with no template assigned | These views are correctly excluded whenever the Template filter is active, and correctly included when it isn't |
| 6 | Views without an applicable Design Option | Most views in the project | These are correctly excluded whenever the Design Option filter is active (Option only / AND modes), and never cause an error |

Note on test #2: because most views are never actually "in" a design
option (see API notes above), it's normal for a Design-Option-only search
to return very few or zero views in many real projects — that is correct
behavior, not a bug. If you need to select views by which design option
their *contents* belong to (e.g. "views that show elements from Option A"),
that is a materially different, heavier query (checking every visible
element in every view) and is a good candidate for a future filter rather
than part of this lightweight selection tool.

---

## 7. Adding future filters (View Type, Discipline, Phase, Sheet, etc.)

The filtering logic is deliberately modular so it can grow without a
rewrite:

1. In `build_view_record(view)`, add whatever new field you need (e.g.
   `"phase_id": get_view_phase_id(view)`).
2. Write a small `filter_by_xxx(record, criteria)` function following the
   existing pattern — return `True` when `criteria` doesn't ask for that
   filter (keeps it a no-op by default), and compare Ids/values otherwise.
3. Register it in `FILTER_REGISTRY` under a new criteria key.
4. Add it to whichever modes in `build_active_filters(mode)` should use it
   (or add a new mode).
5. Add the corresponding UI control to `ViewSelectorDialog` (a new
   `ComboBox`, `CheckBox`, etc.) and populate `criteria[...]` from it in
   `on_ok`.

No other part of the tool — the collection logic, the selection call, the
error handling, or the outputs — needs to change.
