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

Doors get specific tag types depending on their **family or type name**
(not case-sensitive). The first match wins:

| Door name contains | Tag(s) | e.g. |
|---|---|---|
| `Entry` | **two tags**: GH-AN-Tag_Door : **Door Mark (H x W, Construction Type)**, running along the wall and offset 600 mm to the door's facing side; plus GH-AN-Tag_Door : **Internal**, square to the wall, at the door | Entry Door ... |
| `Robe` | GH-AN-Tag_Door : **Robe Door** | Robe Sliding Door : Smart - 2100H 2 x 520 |
| `Opening` | GH-AN-Tag_Door : **Bulkhead Height** | Door - Opening : Opening - 2200H |
| `Internal` | GH-AN-Tag_Door : **Internal** | Internal Timber Flush Door_Single : 2040 x 870 |
| anything else | the default door tag (Tag By Category) | |

Windows always get the default window tag.

- **Entry and Robe are checked before Internal**, so e.g. "Internal Robe…"
  gets the Robe tag.
- **Entry doors' two tags:** in a wall running across the plan, Door Mark
  is horizontal and Internal is vertical (90°). In a wall running up the
  plan it's the other way round. The Door Mark tag is moved 600 mm off the
  door so the two don't overlap; drag it wherever suits.
- **To change the rules**, edit `DOOR_TAG_RULES` near the top of the
  script. Each row is `("text in door name", [tags])`, and each tag is
  `("tag family", "tag type", orientation, offset mm)`. Orientation is
  `None` (usual rule), `"along"` (runs with the wall) or `"across"`
  (square to the wall).
- **If a tag type isn't loaded:** if it's a door's first tag, the default
  door tag is used instead. If it's the second, it's skipped. Either way
  the report lists the missing type.
- The report shows which types were used, e.g.
  `Door tag types used: Door Mark (H x W, Construction Type) x2, Internal x5, Robe Door x3`.

### Tag orientation follows the wall (plan views only)

In **plan views** (floor, ceiling, structural and area plans):

| Element | Wall runs **across** the plan | Wall runs **up** the plan |
|---|---|---|
| **Windows**, **sliding doors**, **Robe** doors, **Opening** doors: tag matches the wall | Horizontal tag | Vertical tag |
| **All other doors** (e.g. hinged internal doors): tag is square to the wall | Vertical tag | Horizontal tag |

- **Sliding doors** are doors with "slid" in their family or type name
  (Sliding, Slider), e.g. *Robe Sliding Door*.
- Angled walls use whichever axis they're closer to.
- **In elevations, sections and other views, all tags stay horizontal.**
- "Across" and "up" are measured against the plan view's own axes, so it
  works on rotated plans too.
- To change which doors match the wall, edit `MATCH_WALL_DOORS` near the
  top of the script, e.g. `["slid", "robe", "opening", "cavity"]`.
- The report shows the split, e.g. `7 tag(s) vertical, 5 horizontal.`

### "For Storage" doors: tag follows the door leaf (plan views only)

For doors with the **For Storage** parameter ticked (e.g. storage doors
drawn part-open at 30°), each tag is **rotated to line up with the open
door leaf**, like the `820` tag on a robe door.

- The leaf angle is **measured from the door itself**. It first reads the
  door's **plan linework in the current view** (the longest straight
  line at an angle to the wall, i.e. the drawn leaf). Door families
  usually draw the open leaf this way while the 3D panel stays shut. If
  that finds nothing, it falls back to the 3D panel. It's correct whichever
  way the door is flipped or handed, and still correct if the swing angle
  changes.
- The angle is kept between −90° and +90°, so the text never reads upside
  down.
- It applies to all of that door's tags (e.g. both tags on an Entry
  door) and overrides the horizontal/vertical rules below.
- It needs **Revit 2023 or later** (free tag rotation). On older versions
  the report says the tag couldn't be rotated.
- If no angled leaf is found (e.g. the door is drawn closed), the door is
  tagged normally and listed in the report.
- The parameter name is `STORAGE_PARAM` near the top of the script, if
  yours is named differently.

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
   Tag Doors & Windows script version 2026-09-29 tag-10
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
