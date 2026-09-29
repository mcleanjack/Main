# ============================================================================
# Revit Material Tag Walls & Roofs - one click, whole elevation
# ----------------------------------------------------------------------------
# Paste this entire file into a single Dynamo "Python Script" node.
#
# Engine: CPython3 (Dynamo 2.6+ / Revit 2022+ default). Also runs on the
# legacy IronPython2 engine.
#
# What it does (one run = the whole active view):
#   1. Finds every wall and roof visible in the active ELEVATION or SECTION.
#   2. For each, picks the face that faces you in this view: a wall's
#      exterior face, or the roof's top face pointing towards the view.
#   3. Places a material tag, GH-AN-Tag_Material : Material Tag, on that
#      face, at its middle. Material tags read the material of the face
#      they point at, so you get the outside material (brick, cladding,
#      roof sheeting), not an inner layer.
#
# Walls / roofs already tagged in this view are skipped (IN[2]), so running
# it again after adding walls only tags the new ones. Walls seen edge-on or
# facing away from the view, and curtain walls, are skipped. All tags are
# made in one transaction: a single Ctrl+Z in Revit undoes the whole run.
#
# Inputs (all optional - but don't leave a port unconnected, or Dynamo
# won't run the node; remove unused ports with the "-" button):
#   IN[0]  A Boolean (value ignored). Keep it wired: it's flipped after each
#          run so the next click of Run executes again.
#   IN[1]  Add leader (bool). Default False.
#   IN[2]  Skip walls / roofs already tagged in this view (bool). Default
#          True.
#
# Output (OUT):
#   [0] list of created tags
#   [1] status / report text
# ============================================================================

import clr
import traceback

clr.AddReference('RevitAPI')
clr.AddReference('RevitServices')

from Autodesk.Revit.DB import (
    FilteredElementCollector, IndependentTag, TagMode, TagOrientation,
    BuiltInCategory, BuiltInParameter, FamilySymbol, HostObjectUtils,
    ShellLayerType, ViewType, WallKind, UV
)

from RevitServices.Persistence import DocumentManager
from RevitServices.Transactions import TransactionManager

# ----------------------------------------------------------------------------
# Environment
# ----------------------------------------------------------------------------

doc = DocumentManager.Instance.CurrentDBDocument

# Shown at the top of the report, so you can check which copy is running.
SCRIPT_VERSION = "2026-09-29 material-1"

# The material tag to use for every wall and roof.
MATERIAL_TAG_FAMILY = "GH-AN-Tag_Material"
MATERIAL_TAG_TYPE = "Material Tag"

# A face counts as facing the view if its normal points at least this much
# towards you (1 = straight at you, 0 = edge-on).
FACING_MIN = 0.1


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def _in(index, default=None):
    try:
        value = IN[index]
    except (IndexError, NameError):
        return default
    return default if value is None or value == "" else value


def eid_int(element_id):
    try:
        return int(element_id.Value)            # Revit 2024+
    except AttributeError:
        return int(element_id.IntegerValue)     # older Revit


def symbol_name(symbol):
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


def material_tag_type():
    """Id of MATERIAL_TAG_FAMILY : MATERIAL_TAG_TYPE, or None."""
    for sym in (FilteredElementCollector(doc)
                .OfCategory(BuiltInCategory.OST_MaterialTags)
                .OfClass(FamilySymbol)):
        try:
            if (sym.FamilyName.strip().lower()
                    == MATERIAL_TAG_FAMILY.strip().lower()
                    and symbol_name(sym).strip().lower()
                    == MATERIAL_TAG_TYPE.strip().lower()):
                return sym.Id
        except Exception:
            continue
    return None


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


def face_centre(face):
    """Middle of a face (centre of its UV bounds)."""
    bb = face.GetBoundingBox()
    return face.Evaluate(UV((bb.Min.U + bb.Max.U) / 2.0,
                            (bb.Min.V + bb.Max.V) / 2.0))


def facing_face(element, refs, view):
    """(reference, face) of the largest face in refs that faces the view,
    or None."""
    toward = view.ViewDirection        # points from the model to the viewer
    best, best_score = None, 0.0
    for ref in refs:
        try:
            face = element.GetGeometryObjectFromReference(ref)
            n = face.ComputeNormal(UV(0.5, 0.5))
            facing = n.DotProduct(toward)
            if facing < FACING_MIN:
                continue
            score = face.Area * facing      # how much of it you see
            if score > best_score:
                best, best_score = (ref, face), score
        except Exception:
            continue
    return best


def wall_face(wall, view):
    try:
        if wall.WallType.Kind == WallKind.Curtain:
            return None
    except Exception:
        pass
    try:
        refs = HostObjectUtils.GetSideFaces(wall, ShellLayerType.Exterior)
    except Exception:
        return None
    return facing_face(wall, refs, view)


def roof_face(roof, view):
    try:
        refs = HostObjectUtils.GetTopFaces(roof)
    except Exception:
        return None
    return facing_face(roof, refs, view)


def create_material_tag(ref, view, add_leader, point, tag_type_id):
    """Material tag on a face reference, as tag_type_id if given."""
    if tag_type_id is not None:
        try:
            # Revit 2022+: create straight away as the chosen tag type.
            return IndependentTag.Create(doc, tag_type_id, view.Id, ref,
                                         add_leader,
                                         TagOrientation.Horizontal, point)
        except Exception:
            pass
    tag = IndependentTag.Create(doc, view.Id, ref, add_leader,
                                TagMode.TM_ADDBY_MATERIAL,
                                TagOrientation.Horizontal, point)
    if tag_type_id is not None and eid_int(tag.GetTypeId()) != \
            eid_int(tag_type_id):
        tag.ChangeTypeId(tag_type_id)
    return tag


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main():
    view = doc.ActiveView
    report = ["Material Tag Walls & Roofs script version %s" % SCRIPT_VERSION]
    if view.ViewType not in (ViewType.Elevation, ViewType.Section) \
            or view.IsTemplate:
        return [], "\n".join(report + [
            "Open an elevation or section view, then run again."])

    add_leader = bool(_in(1, False))
    skip_tagged = bool(_in(2, True))
    tag_type_id = material_tag_type()
    if tag_type_id is None:
        report.append("%s : %s isn't loaded; using the default material "
                      "tag." % (MATERIAL_TAG_FAMILY, MATERIAL_TAG_TYPE))

    already = tagged_ids_in_view(view) if skip_tagged else set()
    jobs = []
    for bic, kind, finder in ((BuiltInCategory.OST_Walls, "wall", wall_face),
                              (BuiltInCategory.OST_Roofs, "roof", roof_face)):
        for element in (FilteredElementCollector(doc, view.Id)
                        .OfCategory(bic).WhereElementIsNotElementType()):
            jobs.append((kind, element, finder))

    counts = {"wall": 0, "roof": 0}
    skipped_tagged = 0
    not_facing = {"wall": 0, "roof": 0}
    failed = []
    tags = []

    TransactionManager.Instance.EnsureInTransaction(doc)
    try:
        for kind, element, finder in jobs:
            if eid_int(element.Id) in already:
                skipped_tagged += 1
                continue
            found = finder(element, view)
            if found is None:
                not_facing[kind] += 1
                continue
            ref, face = found
            try:
                tag = create_material_tag(ref, view, add_leader,
                                          face_centre(face), tag_type_id)
                tags.append(tag)
                counts[kind] += 1
            except Exception as ex:
                failed.append("%s %s: %s" % (kind, element.Id, ex))
    finally:
        TransactionManager.Instance.TransactionTaskDone()

    report.append("Tagged %d wall(s) and %d roof(s) with %s : %s."
                  % (counts["wall"], counts["roof"], MATERIAL_TAG_FAMILY,
                     MATERIAL_TAG_TYPE))
    if skipped_tagged:
        report.append("%d already tagged in this view, skipped."
                      % skipped_tagged)
    if not_facing["wall"] or not_facing["roof"]:
        report.append("Skipped %d wall(s) and %d roof(s) with no face "
                      "towards this view (edge-on, facing away, or curtain "
                      "walls)." % (not_facing["wall"], not_facing["roof"]))
    if failed:
        report.append("Couldn't tag %d: %s" % (len(failed),
                                                "; ".join(failed)))
    return tags, "\n".join(report)


# ----------------------------------------------------------------------------
# Re-run on every click of Run
# ----------------------------------------------------------------------------

# Dynamo only re-runs a node whose inputs changed, so with nothing wired a
# second click of Run would do nothing. This marker lets the script find its
# own node and flag it as changed, so the next Run executes it again.
SELF_MARKER = "MATERIAL_TAG_WALLS_ROOFS_RERUN_MARKER"


def _input0_boolean(node):
    """The Boolean node wired into this node's IN[0], or None."""
    try:
        for connector in node.InPorts[0].Connectors:
            source = connector.Start.Owner
            if source.GetType().Name == "BoolSelector":
                return source
    except Exception:
        pass
    return None


def flag_self_for_rerun():
    """Make sure the next click of Run executes this node again.

    Dynamo only re-runs a node when something about it changed. Two things
    are done so that's always true after a run:
      1. mark this Python node as modified (force execute), and
      2. once the run has fully finished, flip the Boolean wired into IN[0]
         (True <-> False). Its value is ignored by this script, but to
         Dynamo it's a real input change - the same as you editing it -
         so the node re-runs on the next click of Run.

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

    toggles = [b for b in (_input0_boolean(n) for n in nodes) if b is not None]

    def mark():
        for node in nodes:
            node.MarkNodeAsModified(True)

    def flip():
        for boolean in toggles:
            boolean.Value = not boolean.Value

    try:
        mark()
    except Exception:
        pass

    done = []

    def on_run_finished(sender, args):
        if done:
            return
        done.append(True)
        try:
            workspace.EvaluationCompleted -= on_run_finished
        except Exception:
            pass
        try:
            flip()
        except Exception:
            pass
        try:
            mark()
        except Exception:
            pass

    try:
        workspace.EvaluationCompleted += on_run_finished
    except Exception:
        # Can't wait for the end of the run: flip now instead.
        try:
            flip()
        except Exception as ex:
            return "couldn't flip the IN[0] Boolean (%s)" % ex
    if not toggles:
        return ("no Boolean wired into IN[0] to flip - wire a Boolean node "
                "into IN[0]")
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
