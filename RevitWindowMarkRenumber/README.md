# Window Mark Renumber — click in order

A Dynamo/Revit tool for renumbering window tags by clicking them in the
order you want. Each window's **Mark** (Identity Data) is rewritten as
`W.01`, `W.02`, … in click order, so the tags update right away.

## What it does

1. A dialog asks for the **prefix** (`W.`), **start number** (`1`) and
   **digits** (`2` → `W.01`). The defaults are worked out from the Marks
   already used by windows in the active view.
2. You click **window tags or the windows themselves** in the active view,
   one at a time, in the order you want.
   - Windows you've clicked stay highlighted blue until the script finishes.
   - Only windows and window tags can be picked.
   - Click them one at a time. Dragging a selection box loses the click
     order.
   - Click **Finish** (the green tick on the Options Bar) when you're done,
     or **Cancel** to stop without changing anything.
3. If you ticked **Show a preview before applying** in the dialog, a preview
   lists every change (`W.06 -> W.01`, …) and any duplicate Marks that
   would result. Click **Apply** or **Cancel**. The preview is off by
   default, so the Marks change as soon as you click Finish.
4. All Marks are written in one transaction, so **one Ctrl+Z** in Revit
   undoes the whole renumber.

### Windows you don't click

The dialog has a choice for windows that are visible in the active view but
that you don't click:

- **Leave them alone** (default): they keep their Marks. If one of them
  already uses a new number (e.g. you renumbered three windows to
  W.01–W.03 but an unclicked window is still W.02), the report (and the
  preview, if it's on) warns you about the duplicate.
- **Number them after the clicked ones**: they get the next numbers, in the
  same order as their current Marks. Click the few windows whose position
  you care about, and the rest follow on.

### Design Options

The same Mark in different Design Options is intentional, so the tool
never changes it or flags it:

- Only windows **visible in the active view** are looked at, so windows in
  options the view isn't showing are never touched.
- When checking for duplicates, two windows with the same Mark only count
  if they are in the **same** Design Option, or one of them is in the main
  model. `W.06` in Option 1 and `W.06` in Option 2 never show up as a
  duplicate, even if the view shows both options.
- The duplicate check only ever **reports**. It never renumbers a window
  you didn't click.

## Building the graph in Dynamo

1. In Revit: **Manage → Dynamo**, then start a new graph.
2. **Set the run mode to Manual** (bottom-left of Dynamo). In Automatic
   mode, the clicking session starts again every time the graph
   re-evaluates.
3. Add a **Python Script** node (Script → Editors → Python Script).
   Double-click it, delete the template code, paste in the whole of
   `WindowMarkRenumberByClick.py` and click **Save Changes**. CPython3 or
   PythonNet3 both work, and IronPython2 does too.
4. (Optional) Add a **Boolean** node, set it to `True` and connect it to
   `IN[0]`. If nothing is connected, the script runs anyway.
5. (Optional) Add **Watch** nodes on the outputs:
   - `OUT[0]`: the window elements that were renumbered
   - `OUT[1]`: how many Marks changed
   - `OUT[2]`: report lines (`W.06 -> W.01`, duplicates, skipped windows)
   - `OUT[3]`: status message
   - `OUT[4]`: debug info (shows the traceback if something goes wrong)
6. Save it, e.g. as `WindowMarkRenumber.dyn`.

It also works from **Dynamo Player**, which is the quickest way to run it
again and again.

## Running it

1. Open the floor plan that has the window tags.
2. Click **Run** in Dynamo (or in Dynamo Player).
3. Check the settings dialog and click **Start Picking**. If the dialog
   isn't on screen, it may be behind the Dynamo window, so check Alt-Tab.
4. Switch to the Revit view and click the tags in order, then click
   **Finish** (green tick) on the Options Bar.
5. The status output reads `Completed: …`. To renumber again, just click
   **Run** again: the script flags the graph as changed when it finishes,
   so Dynamo runs it again even though nothing in the graph changed. If
   Run ever does nothing, toggle the Boolean node or use Dynamo Player,
   which always runs the whole graph.

## Notes

- The tool always writes the **Mark** instance parameter
  (`BuiltInParameter.ALL_MODEL_MARK`), which is the "Mark" row under
  Identity Data.
- Clicking a tag renumbers the window **the tag points at**. Tags on
  windows in linked models are ignored, because a linked model's windows
  can't be edited from the host model.
- Revit itself shows its normal "duplicate Mark" warning if duplicates are
  left behind. The renumbering itself never leaves temporary duplicates,
  because every change is made inside one transaction.
- Windows inside model groups whose Mark is read-only are skipped and listed
  in the report.
