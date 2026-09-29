# Revit Window Auto-Dimension

A Dynamo tool for Revit that dimensions between windows in plan views. Click
the windows in a wall and it dimensions the solid wall **between** the
windows, lined up outside the wall. The window widths themselves are not
dimensioned:

```
corner ├──950──┤ [window] ├──1610──┤ [window] ├──1550──┤ corner
```

Revit can't leave a gap in a single dimension string, so each gap is its own
dimension. They all sit on the same line, so they read as one string.

The string ends on the **outer face of the walls that run perpendicular to
the wall hosting the windows**. For end walls this is the external corner of
the building.

Contents of this folder:

- `WindowAutoDimension.dyn`: a ready-to-open Dynamo graph (Python node plus
  its inputs, set to **Manual** run).
- `WindowAutoDimension.py`: the same script as plain text, if you'd rather
  paste it into your own Python Script node.
- `README.md`: this file.

No third-party Dynamo packages are required.

---

## 1. Using it

1. Open a **floor plan** view in Revit.
2. Open `WindowAutoDimension.dyn` in Dynamo (Manage/Automate → Dynamo →
   Open), or run it from **Dynamo Player**.
3. Set the inputs if needed:
   - **Run**: `True`.
   - **Offset from wall (mm)**: the distance from the wall's exterior face to
     the dimension line, in model millimetres. The default is `1000`.
   - **Dimension Type**: the exact name of a linear dimension type, for
     example `Arrow - 2.5mm Arial`. Leave it blank to use the project default.
4. Click **Run**. Revit asks you to pick elements:
   - Click each window you want dimensioned. Doors are accepted too.
   - Press **Finish** on the Options Bar, or press Enter. Esc cancels.
5. The gap dimensions are created for each host wall. The **Result** Watch node
   lists the new dimensions and a short report.

The graph is set to **Manual**. Each click of Run starts a new pick, even
if you haven't changed any inputs, because the script flags the graph as
changed at the end of every run so Dynamo doesn't reuse the last result.
Press Ctrl+Z in Revit to undo a run.

You can pick windows in several different walls at once. Each wall gets its
own line of dimensions.

---

## 2. What it snaps to

| Part of the string | Reference used |
|---|---|
| Window sides | The **jamb faces of the opening cut in the host wall**, i.e. the masonry/rough opening. These are real wall faces, so the dimensions stay attached and update when windows move or resize. |
| Start / end | The **exterior face** (the Finish 2 / outside layer, taken from the wall's orientation) of the **external wall** at each end of the host wall. At an external corner this is the building corner. Where the façade steps, it's the brick face of the step, not the internal plaster. Interior walls such as robes and partitions are never used. |
| Fallback end | If no perpendicular wall is found on a side, the host wall's own end face is used. |
| Fallback window | If a window family doesn't cut the wall, its centre (Left/Right centre) reference is used and a note is added to the report. |

How the perpendicular wall is chosen:

- It must be visible in the current view, straight, and within about 1° of
  perpendicular to the host wall.
- It must touch the host wall, on either the interior or exterior side.
- Of those walls, the one **furthest out** on each side is used, which is
  the external corner wall. Interior walls that butt into the host wall
  part-way along, such as the ROBE 2 wall or the BED 3/BED 2 partition, are
  skipped. The string always runs corner to corner, even if you pick only
  one window.

The dimension line is placed parallel to the host wall on its **exterior**
side, and the end walls are measured to their **exterior** face. Both come
from each wall's orientation. If a string lands on the inside, or ends on
the plaster instead of the brick, that wall is flipped. Select it, use the
flip arrows (or Spacebar), then re-run.

---

## 3. Limitations

- If the external wall is modelled as several separate wall segments, the
  string only runs to the ends of the segment hosting the picked windows.
- Straight host walls only; curved walls are skipped and reported.
- Windows in curtain walls aren't hosted in a basic wall and are skipped.
- Walls in linked models are not used as end walls.
- For walls with several jamb faces (e.g. cavity walls with returns), the
  innermost/tightest opening faces are used.

---

## 4. Building the graph manually (instead of opening the .dyn)

1. In a blank Dynamo graph, add a **Boolean** node (Run), a **Number** node
   (Offset, `1000`), and a **String** node (Dimension Type, blank).
2. Add a **Python Script** node. Click **+** twice so it has `IN[0]`, `IN[1]`,
   `IN[2]`, then paste in the contents of `WindowAutoDimension.py`.
3. Wire Run → `IN[0]`, Offset → `IN[1]`, and Dimension Type → `IN[2]`. Add a
   **Watch** node on the output.
4. Set the graph's run mode to **Manual** (bottom-left of the Dynamo window).

The script targets the **CPython3** engine, the default in Dynamo 2.13+ and
Revit 2022+. It also runs under IronPython2.
