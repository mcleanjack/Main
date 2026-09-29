# Slab Auto-Dimension

A Dynamo/Revit tool for slab plans. Draw a line across the slab and it
creates a running dimension string that snaps to **every floor/slab edge
the line crosses**: outer slab edges, steps and rebates (e.g. the
`-172 STEP` edges), set-downs, recesses and openings.

```
   |<- 2670 ->|<120>|<------------------ 12430 ------------------>|<150>|
   porch edge   rebate                                          rebate  slab edge
```

It works like the wall tool (`RevitAutoDimension`): same line picking,
stepped paths, dimension types and re-running. It's a **separate script**,
so the two don't affect each other.

---

## 1. What it picks up

| Picks up | Doesn't pick up |
|---|---|
| Vertical edge faces of **Floors** | Top and bottom faces of slabs |
| Vertical edge faces of **Structural Foundation** slabs (turn off with `IN[3] = False`) | Isolated footings / pad families (they're families, not slabs) |
| Edges at **any height**: top edge, rebate step, bottom of a thickening | Sloped edge faces |
| Openings/shafts cut through the slab | Edges not square to the line (within 1°) |

- **Stepped edges:** a rebate is two edges at different plan positions,
  so you get both (e.g. the `120` and `150` above). Edges at the **same**
  plan position (e.g. the top and bottom of a straight edge, or two slabs
  meeting flush) count once.
- **Porch and residence as separate slabs:** both are picked up. Where
  they meet flush, you get one point.
- The dimension is attached to the slab edge faces, so it updates if the
  slab edges move.

### Overall slab edges

By default the string also snaps to the **outermost slab edges** in its
direction, across **all slabs visible in the view**. That way it
captures the whole length of the slab, even where your line doesn't
cross that edge. Example: a porch that sticks out past the part of the
residence slab the line runs through. Its outer edge is still added, as
the first point of the string.

- These points come from each slab's own edges, so they're attached like
  the rest.
- If the outermost edge is one the line already crosses, nothing extra is
  added.
- A jog in a stepped path never gets an overall string of its own.
- The report says when this happens, e.g.
  `1 overall slab edge(s) added beyond the line.`
- To turn it off, wire `False` into `IN[4]`.
- It uses **every slab visible in the view**. If the view shows more
  than one building, hide the others (or use a crop region) so their
  edges aren't treated as the overall extent.

---

## 2. Building the graph in Dynamo

1. In Revit, open **Manage → Dynamo** and start a new graph.
2. Add a **Python Script** node and paste in all of `SlabEdgeDimension.py`.
3. **Don't leave `IN[0]` unconnected.** Dynamo won't run a node while any
   of its input ports is empty. Either:
   - wire a **Boolean** node into `IN[0]` to pick lines on screen (its
     value is ignored); or
   - click **−** to remove `IN[0]`.
4. Set Dynamo to **Manual** run mode.
5. Save it (e.g. `SlabDimension.dyn`) in a **trusted location**, or
   Dynamo shows *"Run blocked."* (see the wall tool's README).

**Optional inputs:** add ports with **+** and wire them:

| Port | Node | Purpose |
|---|---|---|
| `IN[0]` | **Boolean**, or **Select Model Element(s)** | Boolean = pick the line(s) on screen. Or select the line(s) in advance. |
| `IN[1]` | **String** | Dimension type name, e.g. `Linear - 2.5mm Arial`. Blank = default. |
| `IN[2]` | **Boolean** | `True` deletes the drawn line(s) afterwards. |
| `IN[3]` | **Boolean** | `True` (default) also picks up Structural Foundation slabs. `False` = Floors only. |
| `IN[4]` | **Boolean** | `True` (default) also snaps to the **overall** slab edges (see below). `False` = only edges the line crosses. |

Wire `OUT` into a **Watch** node. The first line shows the script version,
e.g. `Slab Auto-Dimension script version 2026-09-29 slab-2`.

**Running it again:** just click **Run**. In Manual mode the script flags
itself to run again next time, so no toggling or re-wiring is needed. Or
run the saved graph from **Manage → Dynamo Player**.

---

## 3. Using it

1. Open the **slab plan**. The tool uses the active view.
2. Draw a straight **Detail Line** across the slab, **square to the edges**
   you want to dimension. Start and finish it **outside** the slab.
3. Run the graph, click the line(s), then click **Finish** on the Options
   Bar (or press Enter).
4. Check the Watch node, e.g.
   `Line 10254001: 6 slab edges dimensioned across 2 slab(s).`

**Stepped paths:** as with the wall tool, draw the path as connected lines
(ends snapped together). Runs in the same direction become **one** string
on the longest run. A jog that crosses no edges is ignored. Separate lines
each get their own string.

---

## 4. If something's missing

| Symptom | Likely cause |
|---|---|
| An edge isn't picked up | The edge isn't square to the line, the slab isn't visible in the view, or the line stops short of it. |
| Pad footings aren't dimensioned | They're families, not slabs. Only slab edges are picked up. |
| `No dimensions made` | Fewer than 2 edges were found. Check the line crosses the slab fully and square to its edges. |
| Extra points you don't want | Edges of openings or thickenings the line crosses. Move the line to avoid them. |
