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
| `IN[3]` | **Boolean** | `True` (default) = hide everything except windows & doors while you pick (see below). `False` = pick in the normal view. |

### Door tag types by door name

Doors get a specific tag type depending on their **family or type name**
(not case-sensitive). The first match wins:

| Door name contains | Tag family : type | e.g. |
|---|---|---|
| `Robe` | GH-AN-Tag_Door : **Robe Door** | Robe Sliding Door : Smart - 2100H 2 x 520 |
| `Opening` | GH-AN-Tag_Door : **Bulkhead Height** | Door - Opening : Opening - 2200H |
| `Internal` | GH-AN-Tag_Door : **Internal** | Internal Timber Flush Door_Single : 2040 x 870 |
| anything else | the default door tag (Tag By Category) | |

Windows always get the default window tag.

- **Robe is checked first**, so a door named e.g. "Internal Robe…" gets the
  Robe tag.
- **To change the rules**, edit the `DOOR_TAG_RULES` list near the top of
  the script. Each row is `("text in door name", "tag family", "tag
  type")`.
- **If a tag type isn't loaded**, those doors get the default door tag, and
  the report lists the missing type.
- The report shows which types were used, e.g.
  `Door tag types used: Bulkhead Height x2, Internal x5, Robe Door x3`.

### Tag orientation follows the wall (plan views only)

In **plan views** (floor, ceiling, structural and area plans):

| Window / door is in a wall running... | Tag |
|---|---|
| **across** the view (horizontal) | **Vertical** |
| **up** the view (vertical) | **Horizontal** |
| at an angle | whichever of the two it's closer to |

- **Doors with `Robe` in their name are excluded.** They always get a
  horizontal tag.
- **In elevations, sections and other views, all tags stay horizontal.**
- "Across" and "up" are measured against the plan view's own axes, so it
  works on rotated plans too.
- To exclude more doors/windows, add words to `ORIENTATION_EXCLUDE` near
  the top of the script, e.g. `["robe", "cavity slider"]`.
- The report shows the split, e.g.
  `7 tag(s) vertical (in walls running across the view), 5 horizontal.`

---

## 2. Using it

1. Click **Run** (or run the `.dyn` from **Manage → Dynamo Player**).
2. The view switches to **windows and doors only**: everything else is
   temporarily hidden, with the cyan *Temporary Hide/Isolate* border.
   In Revit, **click each window and door** you want to tag. Selected ones
   highlight as you go. Click one again to deselect it.
3. Press **Enter** (or click **Finish** on the Options Bar). **Esc**
   cancels without tagging anything.
4. Tags appear on every window and door you clicked. The Watch node
   reports, e.g.:
   ```
   Tag Doors & Windows script version 2026-09-29 tag-6
   Tagged 6 door(s) and 9 window(s).
   2 already tagged in this view, skipped.
   1 clicked element(s) weren't windows or doors, ignored.
   ```

Details:
- **Only windows & doors shown while picking:** this uses Revit's
  **Temporary Hide/Isolate**, the same as *sunglasses icon → Isolate
  Category*. It's reset automatically when you press Enter, when you press
  Esc, and even if something goes wrong. No view settings, templates or
  element visibility are changed. If the view already has your own
  temporary hide/isolate on, it's left as it is.
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
| Everything stays hidden after a run | Click the **sunglasses icon → Reset Temporary Hide/Isolate**. The report says if the automatic reset failed. |
| Windows/doors in a **linked model** | Not supported. Only elements in this model can be picked. |
