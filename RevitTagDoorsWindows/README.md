# Tag Doors & Windows

A Dynamo/Revit tool: run it, **click the windows and doors** you want to
tag, going round the model, then press **Enter**. Each one gets a tag using
Revit's **Tag By Category**, the same as the ribbon button: the tag type
loaded as the default for the Doors / Windows category.

---

## 1. Building the graph in Dynamo

1. In Revit, open the view you want to tag in (a plan, elevation or
   section), then go to **Manage → Dynamo** and click **New**.
2. Set the run mode (bottom left) to **Manual**.
3. Add a **Python Script** node, double-click it, check the engine says
   **CPython3**, paste in all of `TagDoorsWindows.py`, and save.
4. Add a **Boolean** node and wire it into **IN[0]**. Its value doesn't
   matter; it just gives the node an input. (Or remove `IN[0]` with **−**.
   Don't leave a port unconnected, or Dynamo won't run the node.)
5. Add a **Watch** node on **OUT**.
6. Save it (e.g. `TagDoorsWindows.dyn`) in a **trusted folder**, or
   Dynamo shows *"Run blocked."*.

**Optional inputs** (add ports with **+**):

| Port | Node | Purpose |
|---|---|---|
| `IN[1]` | **Boolean** | `True` = tags with a leader. Default `False`. |
| `IN[2]` | **Boolean** | `True` (default) = skip windows/doors already tagged in this view. `False` = tag them again. |

---

## 2. Using it

1. Click **Run** (or run the `.dyn` from **Manage → Dynamo Player**).
2. In Revit, **click each window and door** you want to tag. Selected ones
   highlight as you go. Click one again to deselect it.
3. Press **Enter** (or click **Finish** on the Options Bar). **Esc**
   cancels without tagging anything.
4. Tags appear on every window and door you clicked. The Watch node
   reports, e.g.:
   ```
   Tag Doors & Windows script version 2026-09-29 tag-1
   Tagged 6 door(s) and 9 window(s).
   2 already tagged in this view, skipped.
   1 clicked element(s) weren't windows or doors, ignored.
   ```

Details:
- **Anything else you click** (walls, rooms, furniture) is ignored, so a
  stray click is harmless.
- **Already tagged in this view** means skipped, so re-running over the same
  area won't double up.
- Tags are placed at the window/door's insertion point, the same as
  Tag By Category. Move them afterwards if needed.
- **One Ctrl+Z** undoes all the tags from a run.
- **Running again:** just click **Run**. In Manual mode the script flags
  itself to run again next time.

---

## 3. If something goes wrong

| Symptom | Fix |
|---|---|
| `Couldn't tag ...: no tag loaded` | Load a Door / Window tag family, and check it's the default in **Annotate → Tag panel ▾ → Loaded Tags and Symbols**. |
| `Open a plan, elevation or section view` | Tags can't go in 3D views. Switch views and run again. |
| Clicked items not highlighting | Make sure the Revit window is active after clicking Run, then click on the windows/doors themselves (not their tags). |
| Windows/doors in a **linked model** | Not supported. Only elements in this model can be picked. |
