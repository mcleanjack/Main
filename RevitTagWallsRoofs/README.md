# Material Tag Walls & Roofs

A Dynamo/Revit tool: open an **elevation** (or section), click **Run**, and
every wall and roof in the view gets a material tag,
**GH-AN-Tag_Material : Material Tag**. No clicking on elements.

---

## 1. What it does

- Finds every **wall** and **roof** visible in the active elevation or
  section.
- For each, it picks the face that **faces you** in that view:
  - **walls:** the exterior face;
  - **roofs:** the top face (roof plane) pointing towards the view. On a
    pitched roof, that's the front slope.
- Places the material tag at the **middle of that face**. Material tags
  show the material of the face they point at, so you get the outside
  material (brick, cladding, roof sheeting), not an inner layer.

Skipped, and counted in the report:
- walls/roofs that already have a **material tag** in this view, so
  re-running only tags new ones (other tags, e.g. keynotes, don't count);
- walls seen **edge-on** or **facing away** (e.g. the back of the house);
- **curtain walls**.

**One Ctrl+Z** undoes all tags from a run.

---

## 2. Building the graph in Dynamo

1. In Revit, open an **elevation**, then **Manage → Dynamo → New**.
2. Set the run mode (bottom left) to **Manual**.
3. Add a **Python Script** node, double-click it, check the engine says
   **CPython3**, paste in all of `TagWallsRoofsMaterial.py`, and save.
4. Add a **Boolean** node and wire it into **IN[0]**. Keep it wired: the
   script flips it after each run so the next click of **Run** executes
   again.
5. Add a **Watch** node on **OUT**.
6. Save it (e.g. `TagWallsRoofs.dyn`) in a **trusted folder**, or Dynamo
   shows *"Run blocked."*.

**Optional inputs** (add ports with **+**):

| Port | Node | Purpose |
|---|---|---|
| `IN[1]` | **Boolean** | `True` = tags with a leader. Default `False`. |
| `IN[2]` | **Boolean** | `True` (default) = skip walls/roofs already tagged in this view. `False` = tag them again. |

---

## 3. Using it

1. Open an elevation or section.
2. Click **Run** (or run the `.dyn` from **Manage → Dynamo Player**).
3. The tags appear. The Watch node reports, e.g.:
   ```
   Material Tag Walls & Roofs script version 2026-09-29 material-2
   Tagged 8 wall(s) and 2 roof(s) with GH-AN-Tag_Material : Material Tag.
   Skipped 5 wall(s) and 0 roof(s) with no face towards this view (edge-on, facing away, or curtain walls).
   ```
4. Drag the tags into position as needed.

---

## 4. If something goes wrong

| Symptom | Fix |
|---|---|
| Report says walls were tagged but none show | Material Tags are turned off in the view. The report warns about this. Turn on **Visibility/Graphics → Annotation Categories → Material Tags** (or in the view template). |
| `Open an elevation or section view` | It only runs in elevations and sections. |
| `GH-AN-Tag_Material : Material Tag isn't loaded` | Load the tag family. Until then the default material tag is used. The names are `MATERIAL_TAG_FAMILY` / `MATERIAL_TAG_TYPE` near the top of the script. |
| A wall shows the wrong material (e.g. plasterboard) | The wall was drawn inside-out, so its "exterior" faces into the house. Flip the wall and re-tag it. |
| Walls hidden behind others get tags | Revit counts them as visible in the view. Delete those tags, or hide those walls in the view first. |
| Run does nothing the second time | Keep the **Boolean wired into IN[0]**, and stay in **Manual** mode. |
