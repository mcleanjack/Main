# Auto-Dimension with Room Names

A Dynamo/Revit tool that turns a line you sketch across a floor plan into a
full running dimension string. The string picks up every wall face the line
crosses, and each room's name is written underneath its dimension value.

```
   |<-200->|<------- 4200 ------->|<-100->|<----- 3100 ----->|<-200->|
           |        Kitchen       |       |     Bedroom 1    |
```

Wall thickness segments have no room in them, so they stay blank. The room
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
| Filter | Only faces **square to the line** (within 1°) are kept, because a linear dimension can only measure between faces perpendicular to it. Faces closer than about 1 mm (e.g. flush joined walls) are merged. |
| Dimension | `NewDimension(view, line, references)` creates one string along the exact line you drew. |
| Room names | For each segment, finds the room at the segment midpoint (1 ft above the view's level, in the **view's phase**) and sets `segment.Below = room name`. |

Everything runs in a single transaction, so one **Ctrl+Z** in Revit undoes it.

---

## 2. Building the graph in Dynamo

**Simplest: one node**

1. In Revit, open **Manage → Dynamo** and start a new graph.
2. Add a **Python Script** node and paste in all of
   `AutoDimensionWithRoomNames.py`.
3. Set Dynamo to **Manual** run mode (bottom left). This matters because the
   on-screen pick doesn't work well in Automatic mode.
4. Save it as `AutoDimension.dyn`.

When it runs, Revit asks you to click the line.

**Full: with options**

Add input ports to the Python node with the **+** button until it has four
(`IN[0]`–`IN[3]`), then wire:

| Port | Node | Purpose |
|---|---|---|
| `IN[0]` | **Select Model Elements** (or *Select Model Element*) | The line(s) you drew. Selecting several lines gives one dimension string per line. Leave it unwired to pick on screen instead. |
| `IN[1]` | **String** | Dimension type name, e.g. `Linear - 2.5mm Arial`. Leave blank for the default. |
| `IN[2]` | **Boolean** | `True` = "101 Kitchen", `False` = "Kitchen". |
| `IN[3]` | **Boolean** | `True` deletes the sketch line after dimensioning. |

Wire the output into a **Watch** node. `OUT[0]` is the list of new
dimensions and `OUT[1]` is a text report.

**Dynamo Player:** to make the inputs editable in Player, right-click each
input node and choose **Is Input**. With `IN[0]` unwired, Player runs become
"click Run → click the line → done".

---

## 3. Using it

1. Open a **floor plan** (or ceiling/area plan). The tool uses the active
   view.
2. Draw a **straight Detail Line** (Annotate → Detail Line) across the
   building, **perpendicular to the walls** you want to measure. Put it
   where you want the dimension string to sit. Make sure it starts and ends
   **outside** the first and last walls.
3. Run the graph and pick the line if prompted.
4. Check the Watch node, for example:
   `Line 452113: 12 wall faces dimensioned, 5 room label(s).`

Tips:
- For horizontal and vertical strings, draw two lines and select both. Each
  gets its own dimension.
- To make the room text bigger or smaller, change the dimension type's text
  settings. Below-text uses the same text style as the value.
- To tweak a label by hand, double-click the dimension value. The **Below**
  field is in the dialog.

---

## 4. What it won't do (and why)

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

## 5. Suggested test

1. A simple 3-room plan with 200 mm exterior and 100 mm interior walls.
   Draw one line through all rooms and check that the faces alternate as
   thickness, room, thickness, and so on, with names under the room segments.
2. The same, with two lines selected (one horizontal, one vertical).
3. A line that ends inside a room. Walls beyond its end should be ignored.
4. A diagonal line. The report should show fewer faces, or `FAILED`.
5. Run it, then press Ctrl+Z once. Everything created should be gone.
