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
| Get the line | Uses the Detail/Model Line(s) wired into `IN[0]`. If nothing is wired, it asks you to click one or more on screen, then **Finish**. |
| Find walls | Collects walls visible in the **active plan view**. A quick bounding-box check skips walls nowhere near the line. |
| Find faces | For each wall, gets both side faces (`HostObjectUtils.GetSideFaces`, Exterior + Interior) and intersects them with the line. It tries the view's cut-plane height first, then heights spread up the full face. That way a line drawn **through a window or door** still finds the host wall above the head or below the sill. `Face.Project` rejects hits that fall outside the face's real edges or inside an opening. |
| Pick the Structure layer | Reads the wall type's layers and works out where the **Structure [1]** layer's two faces are. It then snaps to them (see section 2). Walls with no Structure layer are **skipped** unless `IN[4] = True`. |
| Filter | Only faces **square to the line** (within 1°) are kept, because a linear dimension can only measure between faces perpendicular to it. Faces closer than about 1 mm (e.g. flush joined walls) are merged. |
| Dimension | `NewDimension(view, line, references)` creates one string along the line you drew. For a stepped path of connected lines, it's one string per direction, on the longest run (see *Stepped lines* in section 4). |
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

When it runs, Revit asks you to click the line(s). Click as many as you
like, then click **Finish** on the Options Bar (or press Enter).

> **Running it more than once:** Dynamo only re-runs a node when one of
> its inputs has changed. With nothing wired in, a second click of
> **Run** does nothing: no pick prompt, no dimension. Use one of these:
> - **Dynamo Player (recommended):** run the saved graph from
>   *Manage → Dynamo Player*. Player re-runs the whole graph each time
>   you press Play, so you get a pick prompt every time.
> - **Refresh toggle in Dynamo:** add a 6th input (`IN[5]`), wire a
>   **Boolean** node into it, and flip it (True ↔ False) before each run.
>   The script ignores its value. Changing it just makes Dynamo re-run the
>   node.

**Full: with options**

Add input ports to the Python node with the **+** button until it has six
(`IN[0]`–`IN[5]`), then wire:

| Port | Node | Purpose |
|---|---|---|
| `IN[0]` | **Select Model Elements** (or *Select Model Element*) | The line(s) you drew. Connected lines make one string per direction; separate lines make one string each (see *Stepped lines* below). Leave it unwired to pick on screen instead. |
| `IN[1]` | **String** | Dimension type name, e.g. `Linear - 2.5mm Arial`. Leave blank for the default. |
| `IN[2]` | **Boolean** | `True` = "101 Kitchen", `False` = "Kitchen". |
| `IN[3]` | **Boolean** | `True` deletes the sketch line after dimensioning. |
| `IN[4]` | **Boolean** | Walls with **no** Structure layer: `False` (default) = skip them, `True` = dimension to their core (or finished) faces. |
| `IN[5]` | **Boolean** | **Refresh.** Flip it before each run in the Dynamo window to force a re-run (see the note above). Not needed in Dynamo Player. |

If `IN[0]` is wired to *Select Model Element(s)*, re-running with the
**same** line selected does nothing, because the input hasn't changed.
Click **Select** again for the new line. If `IN[3]` deleted the line, the
report says the line no longer exists.

Wire the output into a **Watch** node. `OUT[0]` is the list of new
dimensions and `OUT[1]` is a text report.

**Dynamo Player:** to make the inputs editable in Player, right-click each
input node and choose **Is Input**. With `IN[0]` unwired, Player runs become
"click Run → click the line(s) → Finish → done".

---

## 4. Using it

1. Open a **floor plan** (or ceiling/area plan). The tool uses the active
   view.
2. Draw a **straight Detail Line** (Annotate → Detail Line) across the
   building, **perpendicular to the walls** you want to measure. Put it
   where you want the dimension string to sit. Make sure it starts and ends
   **outside** the first and last walls.
3. Run the graph. If prompted, click the line(s), then click **Finish**
   on the Options Bar (or press Enter).
4. Check the Watch node, for example:
   `Line 452113: 12 faces dimensioned (walls: 6 on Structure layer, 0 skipped), 5 room label(s).`

### Stepped lines (one string through several runs)

To get around something, like the porch and garage below, draw the path
as several **connected** lines with their ends touching (snap them
end-to-end), then select them all:

```
                  ┌───────────────────────────────  run 2 (longest)
                  │ jog
 ─────────────────┘  run 1
```

- All runs going the **same direction** are merged into **one**
  continuous string. It picks up every wall that any run crosses and
  sits on the longest run.
- Each room name is taken from whichever run passes through that stretch,
  so run 1's rooms are labelled from run 1.
- The **jog** crosses no walls, so it's ignored. The report shows it as
  `0 wall face(s) found, no dimension made`. If a jog does cross walls,
  it gets its own short string.
- Lines that **don't touch** each other always get separate strings. So
  a horizontal and a vertical line drawn apart give two dimensions.

Tips:
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
| Line through a window or door | Dimensioned to the **host wall**, using the wall above the head or below the sill. The window/door itself isn't dimensioned. |
| Opening that runs the full wall height | No wall at that point, so nothing to dimension there. |
| Curtain walls | Skipped and listed in the report. They have no side faces. |
| Walls/rooms in **linked models** | Not included. The tool only reads the host model. |
| Fewer than 2 faces hit | No dimension is made for that line/run, and the report says so. That's normal for a jog between runs. |
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
5. A stepped path (two horizontal runs joined by a short vertical jog).
   You should get **one** string with every wall either run crosses, and
   room names from the run that passes through each room. Also try two
   separate lines (one horizontal, one vertical): you get two strings.
6. A line through a window and an internal door. Both host walls should
   read their framing size, e.g. 90.
7. A line that ends inside a room. Walls beyond its end should be ignored.
8. A diagonal line. The report should show fewer faces, or `FAILED`.
9. Run it, then press Ctrl+Z once. Everything created should be gone.
