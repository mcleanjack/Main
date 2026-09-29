# ============================================================================
# Revit Tag Doors & Windows - click to tag, Enter to finish
# ----------------------------------------------------------------------------
# Paste this entire file into a single Dynamo "Python Script" node.
#
# Engine: CPython3 (Dynamo 2.6+ / Revit 2022+ default). Also runs on the
# legacy IronPython2 engine.
#
# What it does:
#   1. When the graph runs, Revit asks you to click windows and doors in the
#      active view. Click as many as you like, going round the model, then
#      press ENTER (or click Finish on the Options Bar). Esc cancels.
#   2. Tags each clicked window / door using Revit's "Tag By Category" mode
#      (IndependentTag with TagMode.TM_ADDBY_CATEGORY), i.e. the tag type
#      loaded as the default for the Doors / Windows category, exactly like
#      the Tag By Category button on the ribbon.
#   3. Places each tag at the window / door's location point.
#
# Anything else you click (walls, rooms, ...) is ignored. A window / door
# that already has a tag in this view is skipped (IN[2]), so running it
# twice doesn't double up. All tags are made in one transaction: a single
# Ctrl+Z in Revit undoes the whole run.
#
# Inputs (all optional - but don't leave a port unconnected, or Dynamo
# won't run the node; remove unused ports with the "-" button):
#   IN[0]  A Boolean (value ignored). Just gives the node an input.
#   IN[1]  Add leader (bool). Default False.
#   IN[2]  Skip windows / doors already tagged in this view (bool).
#          Default True.
#   IN[3]  Isolate windows & doors while you pick (bool). Default True:
#          everything else in the view is temporarily hidden (Revit's
#          Temporary Hide/Isolate) and comes back when the run finishes,
#          or if you cancel. If the view already has a temporary
#          hide/isolate on, it's left as it is.
#
# Re-running: in Manual run mode the script flags its own node after each
# run, so every click of Run executes it again (see flag_self_for_rerun).
#
# Output (OUT):
#   [0] list of created tags
#   [1] status / report text
# ============================================================================

import clr
import math
import traceback

clr.AddReference('RevitAPI')
clr.AddReference('RevitAPIUI')
clr.AddReference('RevitServices')

from Autodesk.Revit.DB import (
    FilteredElementCollector, IndependentTag, Reference, TagMode,
    TagOrientation, LocationPoint, BuiltInCategory, ViewType, XYZ,
    ElementId, TemporaryViewMode, FamilySymbol, BuiltInParameter, ViewPlan,
    Options, ViewDetailLevel, Solid, GeometryInstance, PlanarFace, Line
)
from System.Collections.Generic import List as NetList
from Autodesk.Revit.UI.Selection import ObjectType
from Autodesk.Revit.Exceptions import OperationCanceledException

from RevitServices.Persistence import DocumentManager
from RevitServices.Transactions import TransactionManager

# ----------------------------------------------------------------------------
# Environment
# ----------------------------------------------------------------------------

doc = DocumentManager.Instance.CurrentDBDocument
uiapp = DocumentManager.Instance.CurrentUIApplication
uidoc = uiapp.ActiveUIDocument if uiapp is not None else None

# Shown at the top of the report, so you can check which copy is running.
SCRIPT_VERSION = "2026-09-29 tag-10"

TAG_CATEGORIES = {
    BuiltInCategory.OST_Doors: "door",
    BuiltInCategory.OST_Windows: "window",
}

# Door tag rules: if the door's family or type name contains the text
# (not case-sensitive), it gets the tag(s) listed. Checked top to bottom
# and the first match wins, so put the most specific first (e.g. "Entry"
# and "Robe" before "Internal"). Doors matching no rule get the default
# door tag (Tag By Category). Edit / add rows here to suit your tags.
#
# Each tag is (tag family, tag type, orientation, offset in mm):
#   orientation  None     = the usual rule (see MATCH_WALL_DOORS below)
#                "along"  = tag runs the same way as the wall
#                "across" = tag is square to the wall
#                (plan views only; elsewhere tags are horizontal)
#   offset       moves the tag this far out from the door, towards the
#                side it faces, so two tags don't sit on top of each other
DOOR_TAG_RULES = [
    ("Entry", [
        ("GH-AN-Tag_Door", "Door Mark (H x W, Construction Type)",
         "along", 600),
        ("GH-AN-Tag_Door", "Internal", "across", 0),
    ]),
    ("Robe",     [("GH-AN-Tag_Door", "Robe Door", None, 0)]),
    ("Opening",  [("GH-AN-Tag_Door", "Bulkhead Height", None, 0)]),
    ("Internal", [("GH-AN-Tag_Door", "Internal", None, 0)]),
]
MM = 1.0 / 304.8     # feet per mm

# Tag orientation follows the host wall (PLAN VIEWS ONLY; elsewhere tags
# stay horizontal):
#   - WINDOWS, and doors whose name contains any of MATCH_WALL_DOORS: the
#     tag runs the same way as the wall (horizontal wall -> horizontal
#     tag, vertical wall -> vertical tag).
#   - all other doors: the tag is square to the wall (horizontal wall ->
#     vertical tag, vertical wall -> horizontal tag).
# "slid" covers Sliding / Slider doors.
MATCH_WALL_DOORS = ["slid", "robe", "opening"]

# Doors with this Yes/No instance parameter ticked (e.g. storage doors
# shown part-open at 30 degrees) get their tag(s) rotated to line up with
# the open door leaf, in plan views. The leaf angle is read from the door's
# own geometry, so it follows the actual swing / hand / facing.
STORAGE_PARAM = "For Storage"


# ----------------------------------------------------------------------------
# Inputs
# ----------------------------------------------------------------------------

def _in(index, default=None):
    try:
        value = IN[index]
    except (IndexError, NameError):
        return default
    return default if value is None or value == "" else value


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def category_ids():
    """{category ElementId as int: 'door' / 'window'}."""
    out = {}
    for bic, label in TAG_CATEGORIES.items():
        try:
            cat = doc.Settings.Categories.get_Item(bic)
            out[eid_int(cat.Id)] = label
        except Exception:
            pass
    return out


def eid_int(element_id):
    try:
        return int(element_id.Value)            # Revit 2024+
    except AttributeError:
        return int(element_id.IntegerValue)     # older Revit


def tagged_ids_in_view(view):
    """Ids (as ints) of elements that already have a tag in this view."""
    ids = set()
    for tag in FilteredElementCollector(doc, view.Id).OfClass(IndependentTag):
        try:
            for eid in tag.GetTaggedLocalElementIds():      # Revit 2022+
                ids.add(eid_int(eid))
        except AttributeError:
            try:
                ids.add(eid_int(tag.TaggedLocalElementId))  # older Revit
            except Exception:
                pass
        except Exception:
            pass
    return ids


def tag_point(element, view):
    """Where to put the tag: the element's location point, or the centre of
    its bounding box if it has none (e.g. some curtain wall doors)."""
    loc = element.Location
    if isinstance(loc, LocationPoint):
        return loc.Point
    bb = element.get_BoundingBox(view) or element.get_BoundingBox(None)
    if bb is not None:
        return XYZ((bb.Min.X + bb.Max.X) / 2.0, (bb.Min.Y + bb.Max.Y) / 2.0,
                   (bb.Min.Z + bb.Max.Z) / 2.0)
    return None


def isolate_doors_windows(view):
    """Temporarily isolate the Doors and Windows categories in the view,
    committed straight away so it shows while you pick. Returns True if
    applied (so it must be reset afterwards), False if skipped."""
    try:
        if view.IsTemporaryHideIsolateActive():
            return False    # the user's own temporary hide/isolate: keep it
    except Exception:
        pass
    ids = NetList[ElementId]()
    for bic in TAG_CATEGORIES:
        try:
            ids.Add(doc.Settings.Categories.get_Item(bic).Id)
        except Exception:
            pass
    if ids.Count == 0:
        return False
    TransactionManager.Instance.EnsureInTransaction(doc)
    view.IsolateCategoriesTemporary(ids)
    TransactionManager.Instance.ForceCloseTransaction()
    uidoc.RefreshActiveView()
    return True


def reset_isolate(view):
    """Bring back everything hidden by isolate_doors_windows."""
    TransactionManager.Instance.EnsureInTransaction(doc)
    try:
        view.DisableTemporaryViewMode(TemporaryViewMode.TemporaryHideIsolate)
    finally:
        TransactionManager.Instance.ForceCloseTransaction()
    try:
        uidoc.RefreshActiveView()
    except Exception:
        pass


def symbol_name(symbol):
    """Type name of a family type (works on both Python engines)."""
    try:
        p = symbol.get_Parameter(BuiltInParameter.SYMBOL_NAME_PARAM)
        if p is not None and p.AsString():
            return p.AsString()
    except Exception:
        pass
    try:
        return symbol.Name or ""
    except Exception:
        return ""


def door_tag_types():
    """{(family lower, type lower): tag type ElementId} for loaded door
    tags."""
    out = {}
    for sym in (FilteredElementCollector(doc)
                .OfCategory(BuiltInCategory.OST_DoorTags)
                .OfClass(FamilySymbol)):
        try:
            out[(sym.FamilyName.strip().lower(),
                 symbol_name(sym).strip().lower())] = sym.Id
        except Exception:
            continue
    return out


def door_rule(door):
    """The DOOR_TAG_RULES row whose text is in the door's family or type
    name, or None."""
    try:
        symbol = door.Symbol
        name = (symbol.FamilyName + " " + symbol_name(symbol)).lower()
    except Exception:
        name = (getattr(door, "Name", "") or "").lower()
    for text, specs in DOOR_TAG_RULES:
        if text.lower() in name:
            return text, specs
    return None


def element_name(element):
    """'family type' of a door / window, lower case."""
    try:
        symbol = element.Symbol
        return (symbol.FamilyName + " " + symbol_name(symbol)).lower()
    except Exception:
        return (getattr(element, "Name", "") or "").lower()


def wall_direction(element):
    """Direction of the host wall, or None. Uses the wall's location line
    if straight, else the element's own along-the-wall direction."""
    try:
        curve = element.Host.Location.Curve
        d = curve.GetEndPoint(1).Subtract(curve.GetEndPoint(0))
        if d.GetLength() > 1e-9:
            return d.Normalize()
    except Exception:
        pass
    try:
        return element.HandOrientation     # along the wall for hosted
    except Exception:
        return None


def tag_orientation(element, view, kind, mode=None):
    """Tag orientation from the host wall, in plan views only (elsewhere
    every tag stays horizontal):
      - windows, and doors named like MATCH_WALL_DOORS (sliding, robe,
        opening): tag MATCHES the wall - horizontal in a wall running
        across the plan, vertical in a wall running up it;
      - all other doors: tag is square to the wall - vertical in a wall
        running across the plan, horizontal in a wall running up it.
    mode "along" / "across" (from DOOR_TAG_RULES) overrides that choice.
    Anything whose wall can't be read keeps a horizontal tag."""
    if not isinstance(view, ViewPlan):
        return TagOrientation.Horizontal
    d = wall_direction(element)
    if d is None:
        return TagOrientation.Horizontal
    try:
        across = abs(d.DotProduct(view.RightDirection))
        up = abs(d.DotProduct(view.UpDirection))
    except Exception:
        return TagOrientation.Horizontal
    wall_across = across >= up
    if mode == "along":
        matches_wall = True
    elif mode == "across":
        matches_wall = False
    else:
        matches_wall = kind == "window" or any(
            text in element_name(element) for text in MATCH_WALL_DOORS)
    if matches_wall:
        return TagOrientation.Horizontal if wall_across \
            else TagOrientation.Vertical
    return TagOrientation.Vertical if wall_across \
        else TagOrientation.Horizontal


def is_for_storage(door):
    """True if the door's STORAGE_PARAM (Yes/No) is ticked."""
    try:
        p = door.LookupParameter(STORAGE_PARAM)
        return p is not None and p.AsInteger() == 1
    except Exception:
        return False


def _geometry_objects(geometry):
    """Every geometry object in a geometry element, including inside
    family instances (in model coordinates)."""
    for obj in geometry:
        if isinstance(obj, GeometryInstance):
            for inner in _geometry_objects(obj.GetInstanceGeometry()):
                yield inner
        else:
            yield obj


def _is_angled(dx, dy, wall):
    """True if direction (dx, dy) is neither along nor square to the wall
    (more than 3 degrees off both)."""
    length = math.hypot(dx, dy)
    if length < 1e-9:
        return False
    c = abs(dx * wall.X + dy * wall.Y) / length
    return math.sin(math.radians(3)) < c < math.cos(math.radians(3))


def _leaf_from_plan_lines(door, view, wall):
    """Leaf direction from the door's plan linework in this view: the
    longest straight line at an angle to the wall. Door families usually
    draw the open leaf this way in plan (while the 3D panel stays shut)."""
    opt = Options()
    opt.View = view
    try:
        geometry = door.get_Geometry(opt)
    except Exception:
        return None
    if geometry is None:
        return None
    best, best_len = None, 0.0
    for obj in _geometry_objects(geometry):
        if not isinstance(obj, Line):
            continue
        a, b = obj.GetEndPoint(0), obj.GetEndPoint(1)
        dx, dy = b.X - a.X, b.Y - a.Y
        if not _is_angled(dx, dy, wall):
            continue
        length = math.hypot(dx, dy)
        if length > best_len:
            best, best_len = (dx, dy), length
    return best


def _leaf_from_3d(door, wall):
    """Leaf direction from the door's 3D geometry: the largest vertical
    flat face at an angle to the wall (runs square to that face's normal)."""
    opt = Options()
    opt.DetailLevel = ViewDetailLevel.Fine
    try:
        geometry = door.get_Geometry(opt)
    except Exception:
        return None
    if geometry is None:
        return None
    best, best_area = None, 0.0
    for solid in _geometry_objects(geometry):
        if not isinstance(solid, Solid):
            continue
        try:
            faces = list(solid.Faces)
        except Exception:
            continue
        for face in faces:
            if not isinstance(face, PlanarFace):
                continue
            n = face.FaceNormal
            if abs(n.Z) > 0.01:
                continue                    # top / bottom face
            if not _is_angled(n.X, n.Y, wall):
                continue
            if face.Area > best_area:
                best, best_area = (-n.Y, n.X), face.Area
    return best


def leaf_angle(door, view):
    """Angle (radians, in the view, between -90 and +90 deg so text reads
    upright) of the open door leaf, from the door's plan linework in this
    view, else its 3D panel. None if no angled leaf is found."""
    wall = wall_direction(door)
    if wall is None:
        return None
    leaf = _leaf_from_plan_lines(door, view, wall) or _leaf_from_3d(door, wall)
    if leaf is None:
        return None
    leaf = XYZ(leaf[0], leaf[1], 0.0)
    try:
        angle = math.atan2(leaf.DotProduct(view.UpDirection),
                           leaf.DotProduct(view.RightDirection))
    except Exception:
        return None
    while angle > math.pi / 2:
        angle -= math.pi
    while angle <= -math.pi / 2:
        angle += math.pi
    return angle


def rotate_tag(tag, angle):
    """Free-rotate a tag to angle (radians). Revit 2023+ only."""
    tag.TagOrientation = TagOrientation.AnyModelDirection
    tag.RotationAngle = angle


def offset_point(element, point, offset_mm):
    """point moved offset_mm towards the side the door / window faces."""
    if not offset_mm:
        return point
    try:
        f = element.FacingOrientation
        return XYZ(point.X + f.X * offset_mm * MM,
                   point.Y + f.Y * offset_mm * MM, point.Z)
    except Exception:
        return point


def create_tag(element, view, add_leader, point, tag_type_id, orientation):
    """Tag By Category; with tag_type_id, switch the new tag to that
    type."""
    tag = None
    if tag_type_id is not None:
        try:
            # Revit 2022+: create straight away as the chosen tag type.
            tag = IndependentTag.Create(
                doc, tag_type_id, view.Id, Reference(element), add_leader,
                orientation, point)
        except Exception:
            tag = None
    if tag is None:
        tag = IndependentTag.Create(
            doc, view.Id, Reference(element), add_leader,
            TagMode.TM_ADDBY_CATEGORY, orientation, point)
        if tag_type_id is not None and tag.GetTypeId() != tag_type_id:
            tag.ChangeTypeId(tag_type_id)
    try:
        if tag.TagOrientation != orientation:
            tag.TagOrientation = orientation
    except Exception:
        pass
    return tag


def pick_elements():
    """Click windows / doors, then Enter or Finish. None if cancelled."""
    try:
        refs = uidoc.Selection.PickObjects(
            ObjectType.Element,
            "Tag Doors & Windows: click the windows and doors to tag, "
            "then press Enter (or click Finish)")
    except OperationCanceledException:
        return None
    return [doc.GetElement(ref.ElementId) for ref in refs]


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main():
    view = doc.ActiveView
    if view.ViewType == ViewType.ThreeD or view.IsTemplate:
        return [], ("Open a plan, elevation or section view to tag in, "
                    "then run again.")
    if uidoc is None:
        return [], "Revit UI isn't available."

    add_leader = bool(_in(1, False))
    skip_tagged = bool(_in(2, True))
    isolate = bool(_in(3, True))
    report = ["Tag Doors & Windows script version %s" % SCRIPT_VERSION]

    isolated = False
    if isolate:
        try:
            isolated = isolate_doors_windows(view)
            if not isolated:
                report.append("View already had a temporary hide/isolate "
                              "on, so it was left as it is.")
        except Exception as ex:
            report.append("Couldn't isolate windows & doors (%s); picking "
                          "in the normal view." % ex)
    reset_problem = None
    try:
        tags, text = tag_picked(view, add_leader, skip_tagged, report)
    finally:
        # Always bring everything back, even on cancel or an error.
        if isolated:
            try:
                reset_isolate(view)
            except Exception as ex:
                reset_problem = ex
    if reset_problem is not None:
        text += ("\nCouldn't reset the temporary isolate (%s). Use the "
                 "sunglasses icon > Reset Temporary Hide/Isolate."
                 % reset_problem)
    return tags, text


def tag_picked(view, add_leader, skip_tagged, report):
    """Pick windows / doors, tag them, return (tags, report text)."""
    picked = pick_elements()
    if picked is None:
        return [], "\n".join(report + ["Cancelled. Nothing tagged."])

    cats = category_ids()
    already = tagged_ids_in_view(view) if skip_tagged else set()
    counts = {"door": 0, "window": 0}
    tag_types = door_tag_types()
    by_rule = {}            # "Robe Door" -> count
    vertical = 0
    rotated = 0
    storage_angle = {}      # door id -> leaf angle (For Storage doors)
    storage_no_leaf = []
    rotate_failed = []
    missing_types = set()
    skipped_tagged = 0
    ignored = 0
    failed = []
    tags = []
    seen = set()

    TransactionManager.Instance.EnsureInTransaction(doc)
    try:
        for element in picked:
            if element is None or element.Category is None:
                ignored += 1
                continue
            kind = cats.get(eid_int(element.Category.Id))
            if kind is None:
                ignored += 1        # not a window or door
                continue
            key = eid_int(element.Id)
            if key in seen:
                continue            # clicked twice
            seen.add(key)
            if key in already:
                skipped_tagged += 1
                continue
            point = tag_point(element, view)
            if point is None:
                failed.append("%s %s: no location" % (kind, element.Id))
                continue
            rule = door_rule(element) if kind == "door" else None
            if kind == "door" and isinstance(view, ViewPlan) \
                    and is_for_storage(element):
                storage_angle[key] = leaf_angle(element, view)
                if storage_angle[key] is None:
                    storage_no_leaf.append(str(element.Id))
            # No rule: one default tag (Tag By Category), usual orientation.
            specs = rule[1] if rule is not None else [(None, None, None, 0)]
            made = 0
            for i, (family, tag_type, mode, offset_mm) in enumerate(specs):
                tag_type_id = None
                if family:
                    tag_type_id = tag_types.get((family.lower(),
                                                 tag_type.lower()))
                    if tag_type_id is None:
                        missing_types.add("%s : %s" % (family, tag_type))
                        if i > 0:
                            continue    # don't add a 2nd default tag
                try:
                    orientation = tag_orientation(element, view, kind, mode)
                    angle = storage_angle.get(key, None) if kind == "door" \
                        else None
                    if angle is not None:
                        orientation = TagOrientation.Horizontal
                    tag = create_tag(element, view, add_leader,
                                     offset_point(element, point, offset_mm),
                                     tag_type_id, orientation)
                    if angle is not None:
                        try:
                            rotate_tag(tag, angle)
                            rotated += 1
                        except Exception as ex:
                            rotate_failed.append("%s (%s)" % (element.Id, ex))
                    elif orientation == TagOrientation.Vertical:
                        vertical += 1
                    if tag_type_id is not None:
                        by_rule[tag_type] = by_rule.get(tag_type, 0) + 1
                    tags.append(tag)
                    made += 1
                except Exception as ex:
                    failed.append("%s %s: %s" % (kind, element.Id, ex))
            if made:
                counts[kind] += 1
                already.add(key)
    finally:
        TransactionManager.Instance.TransactionTaskDone()

    report.append("Tagged %d door(s) and %d window(s)."
                  % (counts["door"], counts["window"]))
    if tags:
        report.append("%d tag(s) vertical, %d horizontal."
                      % (vertical, len(tags) - vertical))
    if rotated:
        report.append("%d tag(s) rotated to the door leaf (For Storage "
                      "doors)." % rotated)
    if storage_no_leaf:
        report.append("For Storage door(s) with no angled leaf found in "
                      "their plan lines or 3D panel, tagged normally: "
                      + ", ".join(storage_no_leaf))
    if rotate_failed:
        report.append("Couldn't rotate tag(s) - free tag rotation needs "
                      "Revit 2023 or later: " + "; ".join(rotate_failed))
    if by_rule:
        report.append("Door tag types used: " + ", ".join(
            "%s x%d" % (name, n) for name, n in sorted(by_rule.items())))
    if missing_types:
        report.append("Tag type(s) not loaded, used the default door tag "
                      "instead: " + ", ".join(sorted(missing_types)))
    if skipped_tagged:
        report.append("%d already tagged in this view, skipped."
                      % skipped_tagged)
    if ignored:
        report.append("%d clicked element(s) weren't windows or doors, "
                      "ignored." % ignored)
    if failed:
        report.append("Couldn't tag %d: %s" % (len(failed), "; ".join(failed)))
        if any("tag" in f.lower() and "load" in f.lower() for f in failed):
            report.append("Load a Door / Window tag family and set it as "
                          "the default (Annotate > Tag > Loaded Tags and "
                          "Symbols).")
    return tags, "\n".join(report)


# ----------------------------------------------------------------------------
# Re-run on every click of Run
# ----------------------------------------------------------------------------

# Dynamo only re-runs a node whose inputs changed, so with nothing wired a
# second click of Run would do nothing. This marker lets the script find its
# own node and flag it as changed, so the next Run executes it again.
SELF_MARKER = "TAG_DOORS_WINDOWS_RERUN_MARKER"


def flag_self_for_rerun():
    """Mark this Python node as modified so the next Run re-executes it.

    Done twice, to be safe across Dynamo versions: straight away, and
    again once this run has fully finished (Dynamo's EvaluationCompleted
    event), in case anything clears the flag at the end of the run.

    Only in Manual run mode, so Automatic mode never re-prompts on its own.
    Returns None on success, else a short reason."""
    try:
        clr.AddReference('DynamoRevitDS')
        from Dynamo.Applications import DynamoRevit
        try:
            model = DynamoRevit.RevitDynamoModel
        except Exception:
            model = DynamoRevit().RevitDynamoModel
        workspace = model.CurrentWorkspace
        run_type = workspace.RunSettings.RunType
        # CPython3 hands .NET enums over as numbers (Manual = 0), IronPython
        # as enum values, so compare against the enum itself.
        try:
            from Dynamo.Models import RunType
            manual = run_type == RunType.Manual
        except Exception:
            manual = str(run_type) in ("Manual", "0")
        if not manual:
            return "Dynamo isn't in Manual run mode"
        nodes = [node for node in workspace.Nodes
                 if SELF_MARKER in (getattr(node, "Script", None) or "")]
        if not nodes:
            return "couldn't find this Python node"
    except Exception as ex:
        return "couldn't reach Dynamo (%s)" % ex

    def mark():
        for node in nodes:
            node.MarkNodeAsModified(True)

    try:
        mark()
    except Exception as ex:
        return "couldn't flag the node (%s)" % ex

    done = []

    def on_run_finished(sender, args):
        if done:
            return
        done.append(True)
        try:
            mark()
        except Exception:
            pass
        try:
            workspace.EvaluationCompleted -= on_run_finished
        except Exception:
            pass

    try:
        workspace.EvaluationCompleted += on_run_finished
    except Exception:
        pass    # the immediate flag above still applies
    return None


try:
    OUT = main()
except Exception:
    OUT = [], "Unexpected error:\n" + traceback.format_exc()
_rerun_problem = flag_self_for_rerun()
if _rerun_problem:
    OUT = OUT[0], (OUT[1] + "\nNote: the next Run may not re-run this "
                   "node automatically (%s). Use Dynamo Player instead, or "
                   "re-wire an input to force a re-run." % _rerun_problem)
