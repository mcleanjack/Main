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
import traceback

clr.AddReference('RevitAPI')
clr.AddReference('RevitAPIUI')
clr.AddReference('RevitServices')

from Autodesk.Revit.DB import (
    FilteredElementCollector, IndependentTag, Reference, TagMode,
    TagOrientation, LocationPoint, BuiltInCategory, ViewType, XYZ,
    ElementId, TemporaryViewMode
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
SCRIPT_VERSION = "2026-09-29 tag-3"

TAG_CATEGORIES = {
    BuiltInCategory.OST_Doors: "door",
    BuiltInCategory.OST_Windows: "window",
}


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
            try:
                tag = IndependentTag.Create(
                    doc, view.Id, Reference(element), add_leader,
                    TagMode.TM_ADDBY_CATEGORY, TagOrientation.Horizontal,
                    point)
                tags.append(tag)
                counts[kind] += 1
                already.add(key)
            except Exception as ex:
                failed.append("%s %s: %s" % (kind, element.Id, ex))
    finally:
        TransactionManager.Instance.TransactionTaskDone()

    report.append("Tagged %d door(s) and %d window(s)."
                  % (counts["door"], counts["window"]))
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
