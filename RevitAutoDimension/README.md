# Auto-Dimension with Room Names

A Dynamo/Revit tool that turns a line you sketch across a floor plan into a
full running dimension string. The string snaps **only to the Structure
layers** of the walls the line crosses (the layers set to *Structure [1]* in
the wall type). Each room's name is written underneath its dimension value.

For a wall type built like this:

| # | Function | Material | Thickness |
|---|---|---|---|
| 1 | Finish 2 [5] | Plasterboard | 10 |
| 2 | *Core Boundary* | | |
| 3 | **Structure [1]** | **Timber Frame 90** | **90** |
| 4 | *Core Boundary* | | |
| 5 | Finish 2 [5] | Plasterboard | 10 |

the string measures the **90 mm frame**, and the room dimensions run from
framing face to framing face:

```
   |<-90->|<------- 4220 ------->|<-90->|<----- 3120 ----->|<-90->|
          |        Kitchen       |      |     Bedroom 1    |
```

It works the same for any framing size (70, 90, 140 mm, ...), since it goes
by the layer's Function, not its thickness.

Wall segments have no room in them, so they stay blank. The room segments
get the room name in the dimension's **Below** text field. That means the
label belongs to the dimension itself: it moves with it and deletes with it,
and you don't have loose text notes to manage.

---

## 1. How it works

| Step | What happens |
|---|---|
| Get the line | Uses the Detail/Model Line(s) wired into `IN[0]`. If nothing is wired, it asks you to click one on screen. |
| Find walls | Collects walls visible in the **active plan view**. A quick bounding-box check skips walls nowhere near the line. |
| Find faces | For each wall, gets both side faces (`HostObjectUtils.GetSideFaces`, Exterior + Interior) and intersects them with the line at the view's cut-plane height. `Face.Project` rejects hits that fall outside the face's real edges. |
| Pick the Structure layer | Reads the wall type's layers and works out where the **Structure [1]** layer's two faces are. It then snaps to them (see section 2). Walls with no Structure layer are **skipped** unless `IN[4] = True`. |
| Filter | Only faces **square to the line** (within 1°) are kept, because a linear dimension can only measure between faces perpendicular to it. Faces closer than about 1 mm (e.g. flush joined walls) are merged. |
| Dimension | `NewDimension(view, line, references)` creates one string along the exact line you drew. |
| Room names | For each segment, finds the room at the segment midpoint (1 ft above the view's level, in the **view's phase**) and sets `segment.Below = room name`. |

Everything runs in a single transaction, so one **Ctrl+Z** in Revit undoes it.

---

## 2. How the Structure layer faces are found (read this if a wall is skipped)

### Which layers

The script reads each wall type's layers (*Edit Type → Structure → Edit*)
and takes the layer(s) whose **Function** is **Structure [1]**:
- One Structure layer (the usual case) gives its two faces.
- Several Structure layers **next to each other** count as one block and
  give its outer two faces.
- Separate Structure layers (e.g. a double stud wall with a gap) can't
  all sit on the two core boundaries, so those walls are skipped. Model
  them as two walls instead, one per frame.

### Wall type setup (required)

Revit only lets a dimension attach to a wall's **finished faces** or its
**core faces**. It has no reference for faces between other layers. So
each face of the Structure layer must be one of those:

| The Structure layer's faces are... | Result |
|---|---|
| the core boundaries: *Core Boundary* rows directly either side of the Structure layer, **as in the table above** | Dimensioned, attached to the wall |
| a finished face (e.g. the unlined side of a wall) | Dimensioned, attached to the wall |
| anywhere else (e.g. core drawn around the plasterboard too) | **Skipped**, with the reason in the report |

So in each wall type, put the two *Core Boundary* rows **directly either
side of the Structure layer**. The dimensions then attach to the wall and
update if it moves or changes type.

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
script doesn't trust it blindly**. For each Structure face, it makes a
throwaway dimension from the exterior finished face to each candidate core
reference. It keeps the one that measures exactly the right distance
(within 0.6 mm) and rolls the throwaway dimension back straight away.

### Skipped walls

The report names each skipped wall and why, e.g.
`Wall 351208 skipped (no Structure layer)`:
- **no Structure layer**: no layer in the wall type has Function
  *Structure [1]*, e.g. a plasterboard lining wall. Set `IN[4] = True` to
  dimension these to their core (or finished) faces instead.
- **Structure layer isn't between the Core Boundary rows in its wall
  type**: fix the wall type as described above.
- **no layer structure**: stacked walls, which have no single layer list.
- **line doesn't cross it cleanly**: the line ends inside the wall or
  crosses a face more than once.

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
| `IN[4]` | **Boolean** | Walls with **no** Structure layer: `False` (default) = skip them, `True` = dimension to their core (or finished) faces. |

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
   `Line 452113: 12 faces dimensioned (walls: 6 on Structure layer, 0 skipped), 5 room label(s).`

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
| Wall with no Structure layer | Skipped and named in the report, or dimensioned to its core/finished faces if `IN[4] = True`. |
| Structure layer not bounded by the core | Skipped and named in the report. Fix the wall type's Core Boundary rows (see section 2). |
| Rooms on a different phase | Labels come from the view's phase, so a demolition-phase view shows existing rooms. |

---

## 6. Suggested test

1. A simple 3-room plan using your 10 + 90 + 10 wall (set up as in the
   table at the top). Draw one line
   through all rooms and check that every wall segment reads **90** and
   the names sit under the room segments.
2. Add one asymmetric wall (e.g. 13 mm plasterboard one side, 10 mm the
   other) and a flipped copy of it. Both should still read the framing
   size. This proves the exterior/interior core faces were matched
   correctly.
3. Add an exterior wall (cladding + cavity + 90 mm Structure frame +
   plasterboard), a 140 mm framed wall, and a plasterboard-only lining wall
   with no Structure layer. They should read 90 and 140. The lining wall
   should be skipped and named in the report.
4. On a wall type whose core boundary is drawn around the plasterboard as
   well, check it is skipped and the report says the Structure layer
   isn't between the Core Boundary rows.
5. The same, with two lines selected (one horizontal, one vertical).
6. A line that ends inside a room. Walls beyond its end should be ignored.
7. A diagonal line. The report should show fewer faces, or `FAILED`.
8. Run it, then press Ctrl+Z once. Everything created should be gone.
