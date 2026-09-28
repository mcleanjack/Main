# Auto-Dimension with Room Names

A Dynamo/Revit tool that turns a line you sketch across a floor plan into a
full running dimension string. By default the string picks up the **90 mm
layer** (the framing) of every wall the line crosses, whatever else is built
up either side of it. Each room's name is written underneath its dimension
value.

For a wall of 10 mm plasterboard + 90 mm framing + 10 mm plasterboard, or
cladding + cavity + 90 mm framing + plasterboard, the string measures the
**90 mm framing**. The room dimensions run stud to stud
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
| Pick the layer | For each wall, uses the faces of its **90 mm layer** (`IN[5]`). If it has none, it uses its core faces (`IN[4]`), and failing that its finished faces. See section 2. |
| Filter | Only faces **square to the line** (within 1°) are kept, because a linear dimension can only measure between faces perpendicular to it. Faces closer than about 1 mm (e.g. flush joined walls) are merged. |
| Dimension | `NewDimension(view, line, references)` creates one string along the exact line you drew. |
| Room names | For each segment, finds the room at the segment midpoint (1 ft above the view's level, in the **view's phase**) and sets `segment.Below = room name`. |

Everything runs in a single transaction, so one **Ctrl+Z** in Revit undoes it,
including any anchor lines.

---

## 2. How the layer and core faces are found (read this if a wall falls back)

### Which layer

The script reads each wall type's layers (*Edit Type → Structure → Edit*)
and looks for one that is **90 mm thick** (within 0.5 mm). If a wall has
more than one, e.g. a 90 mm brick veneer and 90 mm framing, it prefers:
1. a layer whose Function is **Structure [1]**, then
2. a layer inside the core boundaries, then
3. the one nearest the exterior.

To use a different size (e.g. 70 or 140 mm framing), wire a number into
`IN[5]`. Set it to `0` to turn layer matching off and go back to core
faces.

### Attached vs anchored

Revit only lets a dimension attach to a wall's **finished faces** or its
**core faces**. It has no reference for faces between other layers. So:

| The 90 mm layer's faces are... | Dimension is attached via | Follows the wall if it moves? |
|---|---|---|
| the core boundaries (core drawn tightly around the framing) | the core faces | **Yes** |
| a finished face (e.g. unlined side of a wall) | the finished face | **Yes** |
| anywhere else (e.g. core drawn around framing *and* plasterboard, or no core set) | a 100 mm **invisible detail line** placed exactly on that face | **No** |

The invisible anchors keep the string correct right now, but they won't
move with the wall. The report counts them, and each one has
**Comments = `AutoDim anchor`** so you can find them later (e.g. a view
filter on Lines where Comments equals that value). To avoid anchors, set
the wall type's core boundaries tightly around the 90 mm layer. The
dimension is then fully attached.

### Core faces

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
(`IN[0]`–`IN[5]`), then wire:

| Port | Node | Purpose |
|---|---|---|
| `IN[0]` | **Select Model Elements** (or *Select Model Element*) | The line(s) you drew. Selecting several lines gives one dimension string per line. Leave it unwired to pick on screen instead. |
| `IN[1]` | **String** | Dimension type name, e.g. `Linear - 2.5mm Arial`. Leave blank for the default. |
| `IN[2]` | **Boolean** | `True` = "101 Kitchen", `False` = "Kitchen". |
| `IN[3]` | **Boolean** | `True` deletes the sketch line after dimensioning. |
| `IN[4]` | **Boolean** | For walls **without** the `IN[5]` layer: `True` (default) = core faces, `False` = finished faces. |
| `IN[5]` | **Number** | Layer thickness in mm to dimension to. Default `90`; `0` = off. |

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
   `Line 452113: 12 faces dimensioned (walls: 6 on 90mm layer, 0 on core, 0 on finish faces), 5 room label(s).`

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
| Wall with no 90 mm layer | Falls back to core, then finished faces. Counted in the report. |
| 90 mm layer not bounded by the core | Dimensioned via invisible anchor lines, which don't follow the wall (see section 2). |
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
3. Add an exterior wall (cladding + cavity + 90 mm framing + plasterboard)
   and a wall with **no** 90 mm layer (e.g. 140 mm). The first should
   read 90. The second should fall back to its core, and the report shows
   `1 on core`.
4. On a wall type whose core boundary is drawn around the plasterboard as
   well, check it still reads 90. The report should mention anchor lines.
5. The same, with two lines selected (one horizontal, one vertical).
6. A line that ends inside a room. Walls beyond its end should be ignored.
7. A diagonal line. The report should show fewer faces, or `FAILED`.
8. Run it, then press Ctrl+Z once. Everything created should be gone.
