# Auto-Dimension with Room Names

A Dynamo/Revit tool that turns a line you sketch across a floor plan into a
full running dimension string. By default the string picks up the **core
boundary** of every wall the line crosses, and each room's name is written
underneath its dimension value.

For a wall of 10 mm plasterboard + 90 mm framing + 10 mm plasterboard, the
string measures the **90 mm framing**. The room dimensions run stud to stud
(framing face to framing face):

```
   |<-90->|<------- 4220 ------->|<-90->|<----- 3120 ----->|<-90->|
          |        Kitchen       |      |     Bedroom 1    |
```

Wall core segments have no room in them, so they stay blank. The room
segments get the room name in the dimension's **Below** text field. That
means the label belongs to the dimension itself: it moves with it and
deletes with it, and you don't have loose text notes to manage.

---

## 1. How it works

| Step | What happens |
|---|---|
| Get the line | Uses the Detail/Model Line(s) wired into `IN[0]`. If nothing is wired, it asks you to click one on screen. |
| Find walls | Collects walls visible in the **active plan view**. A quick bounding-box check skips walls nowhere near the line. |
| Find faces | For each wall, gets both side faces (`HostObjectUtils.GetSideFaces`, Exterior + Interior) and intersects them with the line at the view's cut-plane height. `Face.Project` rejects hits that fall outside the face's real edges. |
| Snap to core | Swaps each wall's finished faces for its core faces (see section 2). Turn this off with `IN[4] = False`. |
| Filter | Only faces **square to the line** (within 1°) are kept, because a linear dimension can only measure between faces perpendicular to it. Faces closer than about 1 mm (e.g. flush joined walls) are merged. |
| Dimension | `NewDimension(view, line, references)` creates one string along the exact line you drew. |
| Room names | For each segment, finds the room at the segment midpoint (1 ft above the view's level, in the **view's phase**) and sets `segment.Below = room name`. |

Everything runs in a single transaction, so one **Ctrl+Z** in Revit undoes it.

---

## 2. How the core faces are found (read this if a wall falls back)

The Revit API has **no documented method** for getting a reference to a
wall's core face. `HostObjectUtils.GetSideFaces` only returns the finished
faces. The approach the Revit API community uses (it came out of
Autodesk's Revit API forum and The Building Coder) is to build the
reference from the wall's UniqueId:

```
<wall UniqueId>:-9999:<n>     n = 1 wall centre, 2 / 3 core faces, 4 core centre
```

This is the same reference Revit uses when you snap a dimension to "Core
Face" by hand. The mapping of `n` isn't officially documented, so **the
script doesn't trust it blindly**:

1. It reads the wall type's layers and adds up the finish thickness on
   each side of the core. In the example that's 10 mm exterior and 10 mm
   interior.
2. For each side, it makes a throwaway dimension from the finished face to
   each candidate core reference and keeps the one that measures exactly
   that finish thickness (within 0.6 mm). The throwaway dimension is
   rolled back straight away.
3. A side with no finish layers already has its finished face on the
   core, so that face is used directly.

If a wall's core can't be verified, that wall is dimensioned to its
**finished faces** and the report names it, e.g.
`Wall 351208: core faces not found, used finish faces`. Causes:
- The wall type has no core boundaries defined. In Edit Assembly, put
  the core boundaries around the framing layer.
- Stacked walls, which have no single compound structure.
- The line crosses a wall face more than once, or ends inside the wall.
- A future Revit version changes the internal reference format.

**Make sure the core is set up in the wall type.** In *Edit Type →
Structure → Edit*, the 90 mm framing layer must sit **between** the two
*Core Boundary* rows, with the plasterboard layers outside them. That's
what the script reads.

---

## 3. Building the graph in Dynamo

**Simplest: one node**

1. In Revit, open **Manage → Dynamo** and start a new graph.
2. Add a **Python Script** node and paste in all of
   `AutoDimensionWithRoomNames.py`.
3. Set Dynamo to **Manual** run mode (bottom left). This matters because the
   on-screen pick doesn't work well in Automatic mode.
4. Save it as `AutoDimension.dyn`.

When it runs, Revit asks you to click the line.

**Full: with options**

Add input ports to the Python node with the **+** button until it has five
(`IN[0]`–`IN[4]`), then wire:

| Port | Node | Purpose |
|---|---|---|
| `IN[0]` | **Select Model Elements** (or *Select Model Element*) | The line(s) you drew. Selecting several lines gives one dimension string per line. Leave it unwired to pick on screen instead. |
| `IN[1]` | **String** | Dimension type name, e.g. `Linear - 2.5mm Arial`. Leave blank for the default. |
| `IN[2]` | **Boolean** | `True` = "101 Kitchen", `False` = "Kitchen". |
| `IN[3]` | **Boolean** | `True` deletes the sketch line after dimensioning. |
| `IN[4]` | **Boolean** | `True` (default) = core faces only. `False` = finished faces. |

Wire the output into a **Watch** node. `OUT[0]` is the list of new
dimensions and `OUT[1]` is a text report.

**Dynamo Player:** to make the inputs editable in Player, right-click each
input node and choose **Is Input**. With `IN[0]` unwired, Player runs become
"click Run → click the line → done".

---

## 4. Using it

1. Open a **floor plan** (or ceiling/area plan). The tool uses the active
   view.
2. Draw a **straight Detail Line** (Annotate → Detail Line) across the
   building, **perpendicular to the walls** you want to measure. Put it
   where you want the dimension string to sit. Make sure it starts and ends
   **outside** the first and last walls.
3. Run the graph and pick the line if prompted.
4. Check the Watch node, for example:
   `Line 452113: 12 core faces dimensioned, 5 room label(s).`

Tips:
- For horizontal and vertical strings, draw two lines and select both. Each
  gets its own dimension.
- To make the room text bigger or smaller, change the dimension type's text
  settings. Below-text uses the same text style as the value.
- To tweak a label by hand, double-click the dimension value. The **Below**
  field is in the dialog.

---

## 5. What it won't do (and why)

| Case | Behaviour |
|---|---|
| Diagonal line through orthogonal walls | Those faces are skipped. Revit can't measure a linear dimension between faces that aren't square to the dimension line. |
| Curved walls | Skipped, because a linear dimension can't reference a cylindrical face. |
| Curtain walls | Skipped and listed in the report. They have no side faces. |
| Walls/rooms in **linked models** | Not included. The tool only reads the host model. |
| Fewer than 2 faces hit | That line is reported as `FAILED` and no dimension is made. |
| Line not straight / not a line | `FAILED` with a message saying why. |
| Room not placed or unbounded | That segment has no label. |
| Rooms on a different phase | Labels come from the view's phase, so a demolition-phase view shows existing rooms. |

---

## 6. Suggested test

1. A simple 3-room plan using your 10 + 90 + 10 wall. Draw one line
   through all rooms and check that every wall segment reads **90** and
   the names sit under the room segments.
2. Add one asymmetric wall (e.g. 13 mm plasterboard one side, 10 mm the
   other) and a flipped copy of it. Both should still read the framing
   size. This proves the exterior/interior core faces were matched
   correctly.
3. Set `IN[4]` to `False` and check you get 110 mm (finished) walls.
4. The same, with two lines selected (one horizontal, one vertical).
5. A line that ends inside a room. Walls beyond its end should be ignored.
6. A diagonal line. The report should show fewer faces, or `FAILED`.
7. Run it, then press Ctrl+Z once. Everything created should be gone.
