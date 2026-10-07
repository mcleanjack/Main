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
| Room names | For each segment, finds the room at the segment's midpoint on your drawn line, on the view's level. It looks in **this model and any loaded linked models**, preferring rooms in the **view's phase**. It then sets `segment.Below = room name`. If no segment finds a room, the report says how many placed rooms it could see, to help track down why. |

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

### External walls the line doesn't cross: overall ends and facade steps

Picture yourself standing on the line and looking out from it. Every
external wall corner you can **see** is added to the string, even where
the line doesn't cross that wall:

- **Facade steps:** wherever the outer wall line steps in or out (the
  wall returns at each corner of the facade).
- **Overall ends:** the outermost external wall at each end of the string.

| That external wall is... | Snaps to |
|---|---|
| **Brick** | the **outer face of the brick** |
| **Not brick** | the **outer face of the 90 mm Structure layer** (frame) |

- **What counts as "seen":** an outer wall face only counts if it **faces
  back towards the line**. At each point along the string, only the face
  **nearest the line** counts, so walls hidden behind it don't. Its
  corners (where that outline steps in or out) are dimensioned.
- **Which way you're looking (pop-up):** the pop-up that asks for the
  pick-up height also asks which way you're looking from the line. It has
  a separate choice for **horizontal lines** (running across the view) and
  for **vertical lines** (running up the view), and remembers both:

  | Horizontal lines | Vertical lines | Adds |
  |---|---|---|
  | **Look up the view** | **Look left** | only external corners of the house on **that side** of the line, ahead of you. For a line through an **S-bend**, you get the corner ahead of you and not the one behind. |
  | **Look down the view** | **Look right** | the same, on the other side. |

  Each string uses the choice for its own direction, so a stepped path
  with horizontal and vertical runs uses both.

  With **Look up / down / left / right**, where the line is **outside**
  the house (not in a room, or in a Porch/Alfresco room), you only see
  outer wall faces that **face back towards you**, like standing outside
  looking at the house. The far side of the house never shows through,
  e.g. a wall at the back picked up through a gap at a corner. Where the
  line runs **through** the house (in a room), the nearest outer wall
  ahead counts whichever way it faces, so the S-bend rule still works.
- The report lists the walls these extra points came from, e.g.
  `external walls added beyond the line: 1234567, 1234890`. If a point
  you don't want appears, select that wall by ID (**Manage → Select by
  ID**) to see why.
- If the height is wired in (so there's no pop-up), the last choice made
  in the pop-up is used.
- Only **external** walls count (type Function = Exterior). Points the line
  already picks up aren't repeated.
- The report counts them, e.g. `5 external wall point(s) added beyond the line`.
- To turn these off, set `ADD_FACADE_STEPS = False` and/or
  `ADD_OVERALL_EXTERNAL = False` near the top of the script.
- The view's external walls are all considered. On a plan showing a
  detached garage or another building, crop or hide it if its walls
  shouldn't count.

### Porch and alfresco slabs

A porch or alfresco usually has no walls round it, so the string also
snaps to the **slab edges** under any room named **Porch** or
**Alfresco** (anywhere in the name, not case-sensitive, e.g.
"Front Porch", "ALFRESCO"). Those slab edges are treated like external
walls:

- **Crossed by the line:** e.g. a line running up through the alfresco
  picks up the alfresco slab's outer edge.
- **Overall ends:** if the porch/alfresco slab sticks out past the
  outermost external wall, its edge is added as the end of the string
  (the wall is still dimensioned too).
- **Visible corners:** porch/alfresco slab edges count in the "what you
  can see from the line" outline above, with the same pop-up look
  direction.

How a slab edge qualifies:
- It's a vertical edge face of a **Floor** or **Structural Foundation**
  slab visible in the view.
- A Porch/Alfresco room is found **300 mm inside** the edge. The rooms can
  be in this model or a linked model, using the same design-option rules
  as the room names.
- It's on the slab **outline**: an edge where another slab carries on just
  beyond it (e.g. where a set-down porch slab meets the house slab) is
  left out.
- A slab edge within **20 mm** of a wall point already on the string is
  left out, so you don't get tiny segments.

Notes:
- The report counts them, e.g. `2 Porch/Alfresco slab edge(s)`.
- If the porch room's boundary (room separation lines) sits more than
  300 mm in from the slab edge, the edge isn't found. Draw the separation
  lines on the slab edge, or raise `PORCH_PROBE_IN`.
- The room words are `PORCH_ROOM_WORDS = ["porch", "alfresco"]` near the
  top of the script (add e.g. `"patio"` or `"deck"`). Turn this off with
  `ADD_PORCH_SLABS = False`.

### External walls: outer face (brick walls only)

**Only external walls with brick** get the outer face. A wall counts as
brick if its type has **Function = Exterior** and either a **layer
material** or the **wall type name** contains "brick" (e.g.
*GH-Brick-Facebrick-86x240*). **External walls without brick** (e.g.
weatherboard or cladding over frame) are dimensioned **like internal
walls**: both faces of the 90 mm Structure layer. The word list is
`OUTER_FACE_WORDS` near the top of the script, e.g. `["brick", "block"]`.

For walls whose type has **Function = Exterior** (*Edit Type → Construction
→ Function*), the string runs from the wall's **outer face** (e.g. the
outside of the brick) to the **inner face of the Structure layer**, as one
segment. For this wall type:

| # | Function | Material | Thickness | Snapped to |
|---|---|---|---|---|
| 1 | Finish 2 [5] | Facebrick | 110 | ◀ outer face |
| 2 | Thermal/Air Layer [3] | Air | 40 | |
| 3 | Membrane Layer | Sarking | 0 | |
| 4 | *Core Boundary* | | | |
| 5 | Structure [1] | Timber Frame 90 | 90 | |
| 6 | *Core Boundary* | | | ◀ inner face of frame |
| 7 | Finish 1 [4] | Plaster | 10 | |

the string reads **240** (110 + 40 + 90), then carries on into the room
from the inside of the frame. Both points attach to the wall: the outer
face is the wall's exterior face, and the inner frame face is its inner
core boundary.

| External wall modelled as... | String picks up |
|---|---|
| **One wall type**: brick + cavity + Structure frame + plasterboard (above) | outside of brick → inside of frame, e.g. `240` |
| **Two walls**: a brick skin wall (no Structure layer) + a framed wall | outside of brick (from the brick wall) → frame → frame (from the framed wall, set to Interior) |
| **A brick wall whose brick layer is Structure [1]** (e.g. a 150 mm brick wall type) | outside of brick → inside of brick (its Structure layer) |

Check these before you run it:
- **Internal wall types must have Function = Interior.** Any wall type set
  to *Exterior* is dimensioned outer face → inside of frame. The report counts them
  (`N external with outer face`), so a wrong type shows up quickly.
- **External walls must face the right way.** The outer face is the
  wall's *exterior* side. If a wall was drawn inside-out, the string picks
  up the plasterboard face instead. Select the wall and press the flip
  arrows (or spacebar) to fix it.

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

**Ready-made graph:** `AutoDimension.dyn` in this folder is the finished
graph (Boolean → Python Script (CPython3, with this script inside) →
Watch, Manual run mode). Save it in a **trusted folder**, open it from
**Manage → Dynamo → Open** (or Dynamo Player), and click **Run**. It
contains the same code as `AutoDimensionWithRoomNames.py` at the time it
was generated; the Watch's first line shows the version. To build it
yourself instead, follow the steps below.

**Simplest: one node**

1. In Revit, open **Manage → Dynamo** and start a new graph.
2. Add a **Python Script** node and paste in all of
   `AutoDimensionWithRoomNames.py`.
3. **Don't leave `IN[0]` unconnected.** Dynamo won't run a node while
   any of its input ports is empty (the port shows a red bar). Either:
   - wire a **Boolean** node into `IN[0]`. Its value is ignored and you
     still pick lines on screen; or
   - click the **−** button on the node to remove `IN[0]`.
4. Set Dynamo to **Manual** run mode (bottom left). This matters because the
   on-screen pick doesn't work well in Automatic mode.
5. Save it as `AutoDimension.dyn` in a **trusted location** (see
   *"Run blocked."* below).

When it runs, Revit asks you to click the line(s). Click as many as you
like, then click **Finish** on the Options Bar (or press Enter).

> **Running it more than once:** just click **Run** again. Dynamo only
> re-runs a node when one of its inputs has changed. So in **Manual**
> mode, at the end of each run the script **flips the Boolean wired into
> `IN[0]`** (True ↔ False). You'll see it change. Its value is ignored,
> but Dynamo counts it as a change, so the next click of Run always
> executes the node again. This needs a **Boolean node wired into
> `IN[0]`**. Keep Dynamo in **Manual** mode: in Automatic mode this is
> switched off, to avoid an endless loop of pick prompts.
>
> If Run ever does nothing (e.g. a Dynamo version where the script can't
> reach the node), run the saved graph from *Manage → Dynamo Player*
> instead. Player re-runs the whole graph each time you press Play.

> **"Run blocked."** next to the Run button is Dynamo's **file security
> check**, not the script. Dynamo blocks a graph opened from a folder that
> isn't in its trusted locations (e.g. Downloads, Desktop, a network
> drive) until you answer its *"Open external file? This file is stored in
> an untrusted location"* prompt. To fix it for good, do either of these:
> - Re-open the `.dyn`. When the prompt appears, tick **"Trust this
>   file's location in the future"** and click **Yes**.
> - In Dynamo, go to **Settings → Preferences → Security → Trusted File
>   Locations** and add the folder the `.dyn` is saved in.
>
> Then close and re-open the graph.

**Full: with options**

Add input ports to the Python node with the **+** button until it has six
(`IN[0]`–`IN[5]`), then wire:

| Port | Node | Purpose |
|---|---|---|
| `IN[0]` | **Select Model Elements** (or *Select Model Element*) | The line(s) you drew. Connected lines make one string per direction; separate lines make one string each (see *Stepped lines* below). Wire a **Boolean** instead to pick on screen. |
| `IN[1]` | **String** | Dimension type name, e.g. `Linear - 2.5mm Arial`. Leave blank for the default. |
| `IN[2]` | **Boolean** | `True` = "101 Kitchen", `False` = "Kitchen". |
| `IN[3]` | **Boolean** | `True` deletes the sketch line after dimensioning. |
| `IN[4]` | **Boolean** | Walls with **no** Structure layer: `False` (default) = skip them, `True` = dimension to their core (or finished) faces. |
| `IN[5]` | **Number** | **Wall pick-up height** in mm above the view's level, e.g. `1200`. Walls are only picked up where your line crosses them at this height. For automatic, remove this port (**−** button) or wire an empty **String** node, because an unconnected port stops the node running. See below. |

**Wall pick-up height: pop-up**

If no height is wired into the node, a small **Auto-Dimension** window pops
up each time you run it, **before** you pick the lines:

```
 ┌ Auto-Dimension ────────────────────────────────────┐
 │ Wall pick-up height above the view's level (mm):   │
 │ [ 1200      ]                                      │
 │ [ ] Automatic (cut plane, then up the whole wall)  │
 │                      [OK - pick lines]  [Cancel]   │
 └────────────────────────────────────────────────────┘
```

- Type a height (e.g. `1200`) or tick **Automatic**, then press **OK**
  (or Enter). You're then asked to pick the lines.
- It **remembers your last setting**, so usually you just press Enter.
- **Cancel** stops the run without doing anything.
- The window stays on top of Revit/Dynamo. If you don't see it, check the
  taskbar.
- To skip the pop-up, wire the height in instead (below). A wired height
  always wins over the pop-up.

**Wall pick-up height: wired in (`IN[5]`, or a Number on `IN[0]`)**

Quickest setup: wire a **Number** node straight into `IN[0]`, e.g.
`1200`. A number on `IN[0]` means "pick the lines on screen" *and* "use
this pick-up height", so you don't need six ports. If both are wired,
`IN[5]` wins. A **Boolean** on `IN[0]` (with `IN[5]` removed or blank) means
"no height wired", so the **pop-up** asks for it.

- **Blank (automatic)**, i.e. port removed or an empty String wired in: walls are picked up at the view's cut-plane
  height. If the line misses a wall there (e.g. it passes through a
  window or door), the script tries heights up the whole wall, so the
  wall above the head or below the sill is still found.
- **A number, e.g. `1200`:** walls are picked up **only** where the line
  crosses them at 1200 mm above the view's level. This is useful for
  leaving out low walls (e.g. a 900 mm half-height wall: set 1200) or
  dimensioning at a specific height. A wall with a window or door at that
  height is **not** picked up there, because at that height it's an
  opening.
- Pick a height inside the walls you want, not 0. Right at the floor
  line, the wall faces start exactly at the level and may be missed.
- The report confirms the height used, e.g.
  `Picking up walls at 1200 mm above the view's level.`

If `IN[0]` is wired to *Select Model Element(s)*, click **Select** again
for each new line. Otherwise the run re-dimensions the same line. If
`IN[3]` deleted the line, the report says it no longer exists.

A Boolean (e.g. a "Run" toggle) wired into `IN[0]` is ignored, and you're
asked to pick lines on screen. You don't
need a Run toggle at all.

Wire the output into a **Watch** node. `OUT[0]` is the list of new
dimensions and `OUT[1]` is a text report.

**Dynamo Player:** to make the inputs editable in Player, right-click each
input node and choose **Is Input**. With a Boolean on `IN[0]`, Player runs become
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
| Rooms in a linked model | Found, as long as the link is loaded. Only main model and **primary** design option rooms in the link are used. |
| **Design options** | Only rooms in the main model or in a design option **shown in this view** are used. Where two options overlap, names come from the option you see. The view's shown options are worked out from the walls, floors, doors etc. Revit displays in it. The report says how many rooms were ignored, e.g. `Ignored 12 room(s) in design options not shown in this view.` |
| No room labels at all | The report says `No room found under any segment (placed rooms: N in this model, M in linked models)`. If both are 0, the rooms aren't placed/enclosed or the link isn't loaded. |
| Wall with no Structure layer | External (Function = Exterior): its outer face only. Otherwise skipped and named in the report, or dimensioned to its core/finished faces if `IN[4] = True`. |
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
6. A line across an external brick wall. The string should start at the
   **outside of the brick**, then pick up the frame. Check an internal
   wall doesn't get an extra point; if it does, its type's Function is
   set to Exterior.
7. A line through a window and an internal door. Both host walls should
   read their framing size, e.g. 90.
8. A line that ends inside a room. Walls beyond its end should be ignored.
9. A diagonal line. The report should show fewer faces, or `FAILED`.
10. Run it, then press Ctrl+Z once. Everything created should be gone.
