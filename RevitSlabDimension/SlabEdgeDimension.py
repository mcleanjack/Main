# ============================================================================
# Revit Slab Auto-Dimension - floor / slab edges along a drawn line
# ----------------------------------------------------------------------------
# Paste this entire file into a single Dynamo "Python Script" node.
#
# Engine: CPython3 (Dynamo 2.6+ / Revit 2022+ default). Also runs on the
# legacy IronPython2 engine.
#
# What it does:
#   1. Takes one or more straight Detail Lines / Model Lines drawn across
#      the slab plan (picked on screen when the graph runs, or wired in).
#   2. Finds every floor / slab edge the line crosses in the active plan
#      view: the vertical edge faces of Floors and Structural Foundation
#      slabs, at ANY height - outer slab edges, steps and rebates (e.g. the
#      -172 steps), recesses, set-downs and openings.
#   3. Creates ONE continuous linear dimension string through all of them.
#      Edges at the same plan position (e.g. the top and bottom of a
#      stepped edge) count once. Lines drawn as a connected stepped path
#      (ends touching) are treated as one: runs in the same direction are
#      merged into one string on the longest run; a jog crossing no edges
#      is ignored. Separate lines each get their own string.
#
# The dimension is attached to the slab edge faces, so it updates if the
# slab edges move. All changes happen in one Dynamo transaction: a single
# Ctrl+Z in Revit undoes the whole run.
#
# Inputs (all optional - but don't leave a port unconnected, or Dynamo
# won't run the node; remove unused ports with the "-" button):
#   IN[0]  Line element(s) from "Select Model Element(s)", or a Boolean to
#          pick the line(s) on screen when the graph runs (click them,
#          then Finish on the Options Bar or press Enter).
#   IN[1]  Dimension type name (string), e.g. "Linear - 2.5mm Arial".
#          Blank = the project's default linear dimension type.
#   IN[2]  Delete the drawn line(s) afterwards (bool). Default False.
#   IN[3]  Include Structural Foundation slabs (bool). Default True. False
#          = Floors category only.
#   IN[4]  Add overall slab edges (bool). Default True: the string also
#          snaps to the outermost slab edges in its direction, across all
#          slabs in the view, so it captures the whole length of the slab
#          even where the line doesn't cross that edge (e.g. a porch that
#          sticks out). False = only edges the line crosses.
#
# Re-running: in Manual run mode the script flags its own node after each
# run, so every click of Run executes it again (see flag_self_for_rerun).
#
# Output (OUT):
#   [0] list of created Dimension elements
#   [1] status / report text
# ============================================================================

import clr
import math
import traceback

clr.AddReference('RevitAPI')
clr.AddReference('RevitAPIUI')
clr.AddReference('RevitServices')

from Autodesk.Revit.DB import (
    FilteredElementCollector, CurveElement, Line, XYZ, UV, ReferenceArray,
    SubTransaction, PlanarFace, Solid, Options, ViewDetailLevel, ViewPlan,
    DimensionType, DimensionStyleType, BuiltInParameter, BuiltInCategory,
    ElementMulticategoryFilter
)
from Autodesk.Revit.UI.Selection import ObjectType
from Autodesk.Revit.Exceptions import OperationCanceledException

from RevitServices.Persistence import DocumentManager
from RevitServices.Transactions import TransactionManager

from System.Collections.Generic import List as NetList

# ----------------------------------------------------------------------------
# Environment
# ----------------------------------------------------------------------------

doc = DocumentManager.Instance.CurrentDBDocument
uiapp = DocumentManager.Instance.CurrentUIApplication
uidoc = uiapp.ActiveUIDocument if uiapp is not None else None

# Shown at the top of the report, so you can check which copy is running.
SCRIPT_VERSION = "2026-09-29 slab-4"

# Internal units are decimal feet.
DEDUP_TOL = 0.003            # ~1 mm: edges closer than this count as one
PARALLEL_COS = math.cos(math.radians(1.0))   # edge face must be within 1
                                             # deg of square to the line
VERTICAL_TOL = 0.01          # |normal.Z| below this = a vertical face
FACE_Z_SAMPLES = 12          # heights tried up each edge face
CONNECT_TOL = 0.1            # ~30 mm: line ends this close are "connected"


# ----------------------------------------------------------------------------
# Inputs
# ----------------------------------------------------------------------------

def _in(index, default=None):
    try:
        value = IN[index]
    except (IndexError, NameError):
        return default
    return default if value is None or value == "" else value


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _unwrap(value):
    try:
        return UnwrapElement(value)
    except Exception:
        return value


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------

def safe_name(element, default=""):
    try:
        n = element.Name
        if n:
            return n
    except Exception:
        pass
    try:
        p = element.get_Parameter(BuiltInParameter.SYMBOL_NAME_PARAM)
        if p is not None and p.AsString():
            return p.AsString()
    except Exception:
        pass
    return default


def flat(v):
    return XYZ(v.X, v.Y, 0.0)


def find_dimension_type(name):
    if not name:
        return None
    wanted = str(name).strip().lower()
    for dt in FilteredElementCollector(doc).OfClass(DimensionType):
        try:
            if dt.StyleType != DimensionStyleType.Linear:
                continue
        except Exception:
            continue
        if safe_name(dt).strip().lower() == wanted:
            return dt
    return None


# ----------------------------------------------------------------------------
# Getting the drawn line
# ----------------------------------------------------------------------------

def pick_lines():
    """Ask the user to click one or more lines in the active view, then
    Finish (on the Options Bar) or press Enter."""
    if uidoc is None:
        return []
    try:
        refs = uidoc.Selection.PickObjects(
            ObjectType.Element,
            "Slab Auto-Dimension: pick the line(s) you drew across the slab, "
            "then click Finish")
    except OperationCanceledException:
        return []
    return [doc.GetElement(ref.ElementId) for ref in refs]


def line_from_element(element):
    """Return the element's bound Line, or raise ValueError."""
    try:
        valid = element is not None and element.IsValidObject
    except Exception:
        valid = False
    if not valid:
        raise ValueError("The selected line no longer exists (deleted, or "
                         "undone). Select the line again, or unwire IN[0] "
                         "to pick it on screen.")
    if not isinstance(element, CurveElement):
        raise ValueError("'%s' is not a Detail/Model Line."
                         % safe_name(element, str(element)))
    curve = element.GeometryCurve
    if not isinstance(curve, Line):
        raise ValueError("Line %s is curved; draw a straight line."
                         % element.Id)
    if curve.Length < 0.01:
        raise ValueError("Line %s is too short." % element.Id)
    return curve


# ----------------------------------------------------------------------------
# Finding the slab edges the line crosses
# ----------------------------------------------------------------------------

def collect_slabs(view, include_foundations):
    """Floors (and optionally Structural Foundation slabs) visible in the
    view. Only elements with their own solid geometry are used; isolated
    footings and other families are left out."""
    cats = NetList[BuiltInCategory]()
    cats.Add(BuiltInCategory.OST_Floors)
    if include_foundations:
        cats.Add(BuiltInCategory.OST_StructuralFoundation)
    return list(FilteredElementCollector(doc, view.Id)
                .WherePasses(ElementMulticategoryFilter(cats))
                .WhereElementIsNotElementType())


def bbox_hits_line(element, view, a, b):
    """Cheap 2D reject: does the line's XY box overlap the element's?"""
    bb = element.get_BoundingBox(view) or element.get_BoundingBox(None)
    if bb is None:
        return True
    pad = 0.1
    return not (max(a.X, b.X) < bb.Min.X - pad
                or min(a.X, b.X) > bb.Max.X + pad
                or max(a.Y, b.Y) < bb.Min.Y - pad
                or min(a.Y, b.Y) > bb.Max.Y + pad)


def slab_faces(element):
    """Planar faces of the element's own solids, with references so they
    can be dimensioned. Geometry nested in family instances (footings etc.)
    is skipped."""
    opt = Options()
    opt.ComputeReferences = True
    opt.DetailLevel = ViewDetailLevel.Fine
    try:
        geometry = element.get_Geometry(opt)
    except Exception:
        return []
    faces = []
    if geometry is None:
        return faces
    for obj in geometry:
        if not isinstance(obj, Solid):
            continue
        try:
            if obj.Faces.Size == 0 or obj.Volume <= 0:
                continue
        except Exception:
            continue
        for face in obj.Faces:
            if isinstance(face, PlanarFace) and face.Reference is not None:
                faces.append(face)
    return faces


def face_z_samples(face, count=FACE_Z_SAMPLES):
    """Heights spread evenly up a face (bottom to top)."""
    try:
        bb = face.GetBoundingBox()
        zs = [face.Evaluate(UV(u, v)).Z
              for u in (bb.Min.U, bb.Max.U) for v in (bb.Min.V, bb.Max.V)]
    except Exception:
        return []
    z0, z1 = min(zs), max(zs)
    return [z0 + (z1 - z0) * (i + 0.5) / count for i in range(count)]


def edge_hit(face, a, b, direction):
    """Distance along the drawn segment a->b where it crosses this slab
    edge face, or None. Only vertical faces square to the line count:
    a linear dimension can only measure between faces perpendicular to
    it, and top / bottom faces of the slab aren't edges."""
    n = face.FaceNormal
    if abs(n.Z) > VERTICAL_TOL:
        return None             # top, bottom or sloped face
    n = flat(n)
    if n.GetLength() < 1e-9:
        return None
    n = n.Normalize()
    denom = n.DotProduct(direction)
    if abs(denom) < PARALLEL_COS:
        return None             # not square to the line
    length = a.DistanceTo(b)
    origin = face.Origin
    t = n.DotProduct(flat(origin).Subtract(a)) / denom
    if t < -1e-6 or t > length + 1e-6:
        return None             # beyond the ends of the drawn line
    p = a.Add(direction.Multiply(t))
    # The plane is crossed at p; check the face itself is there (not just
    # its infinite plane), at some height up the face.
    for z in face_z_samples(face):
        try:
            res = face.Project(XYZ(p.X, p.Y, z))
        except Exception:
            res = None
        if res is not None and res.Distance < 0.01:
            return t
    return None


def collect_edge_hits(view, a, b, direction, include_foundations, stats):
    """Sorted, de-duplicated [(t, Reference)] of slab edges crossed by the
    segment a->b."""
    hits = []
    for slab in collect_slabs(view, include_foundations):
        if not bbox_hits_line(slab, view, a, b):
            continue
        found = False
        for face in slab_faces(slab):
            t = edge_hit(face, a, b, direction)
            if t is not None:
                hits.append((t, face.Reference))
                found = True
        if found:
            stats["slabs"].add(str(slab.Id))
    hits.sort(key=lambda h: h[0])
    # Edges at the same plan position (e.g. top and bottom of a stepped
    # edge, or two slabs meeting flush) count once; Revit rejects
    # zero-length dimension segments.
    deduped = []
    for t, ref in hits:
        if deduped and abs(t - deduped[-1][0]) < DEDUP_TOL:
            continue
        deduped.append((t, ref))
    return deduped


# ----------------------------------------------------------------------------
# Grouping the drawn lines
# ----------------------------------------------------------------------------

class DrawnLine(object):
    """One selected line, flattened to plan. a / b / s_a / s_b are set
    along its group's axis once grouped (see direction_groups)."""
    def __init__(self, element, curve):
        self.element = element
        self.p0 = flat(curve.GetEndPoint(0))
        self.p1 = flat(curve.GetEndPoint(1))
        self.z = curve.GetEndPoint(0).Z
        self.length = self.p0.DistanceTo(self.p1)
        self.a = self.b = None
        self.s_a = self.s_b = 0.0


def canonical_direction(line):
    """Unit direction of the line, flipped to point +X (or +Y if vertical)
    so parallel lines drawn either way round get the same axis."""
    d = line.p1.Subtract(line.p0).Normalize()
    if d.X < -1e-9 or (abs(d.X) <= 1e-9 and d.Y < 0):
        d = d.Negate()
    return d


def connected_chains(lines):
    """Split lines into chains whose ends touch (within CONNECT_TOL), like
    a stepped path drawn as several lines. A line touching no other line
    is a chain on its own."""
    parent = list(range(len(lines)))

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(lines)):
        for j in range(i + 1, len(lines)):
            ends_i = (lines[i].p0, lines[i].p1)
            ends_j = (lines[j].p0, lines[j].p1)
            if any(p.DistanceTo(q) < CONNECT_TOL
                   for p in ends_i for q in ends_j):
                parent[root(i)] = root(j)
    chains = {}
    order = []
    for i, line in enumerate(lines):
        key = root(i)
        if key not in chains:
            chains[key] = []
            order.append(key)
        chains[key].append(line)
    return [chains[key] for key in order]


def direction_groups(chain):
    """Split one chain into groups of parallel lines; each group becomes
    one dimension string. Returns [(axis, [DrawnLine, ...]), ...] with each
    line's a / b (ordered along the axis) and s_a / s_b (distance of its
    ends along the axis) set."""
    groups = []
    for line in chain:
        d = canonical_direction(line)
        for axis, members in groups:
            if abs(axis.DotProduct(d)) >= PARALLEL_COS:
                members.append(line)
                break
        else:
            groups.append((d, [line]))

    for axis, members in groups:
        origin = members[0].p0
        for line in members:
            if line.p1.Subtract(line.p0).DotProduct(axis) >= 0:
                line.a, line.b = line.p0, line.p1
            else:
                line.a, line.b = line.p1, line.p0
            line.s_a = line.a.Subtract(origin).DotProduct(axis)
            line.s_b = line.s_a + line.length
    return groups


# ----------------------------------------------------------------------------
# Building the dimension
# ----------------------------------------------------------------------------

def overall_extremes(view, axis, members, include_foundations):
    """The outermost slab edges along the axis, across every slab in the
    view: [(s_min, Reference), (s_max, Reference)], s = distance along the
    axis measured the same way as the drawn lines. These capture the whole
    length of the slab even where the line doesn't cross that edge (e.g. a
    porch that sticks out beyond the part of the slab the line crosses)."""
    first = members[0]
    lo = hi = None
    for slab in collect_slabs(view, include_foundations):
        for face in slab_faces(slab):
            n = face.FaceNormal
            if abs(n.Z) > VERTICAL_TOL:
                continue
            n = flat(n)
            if n.GetLength() < 1e-9:
                continue
            if abs(n.Normalize().DotProduct(axis)) < PARALLEL_COS:
                continue    # not square to the string
            s = (flat(face.Origin).Subtract(first.a).DotProduct(axis)
                 + first.s_a)
            if lo is None or s < lo[0]:
                lo = (s, face.Reference)
            if hi is None or s > hi[0]:
                hi = (s, face.Reference)
    return [e for e in (lo, hi) if e is not None]


def group_hits(view, axis, members, include_foundations, stats,
               add_overall=False):
    """Every slab edge crossed by any line in the group, as a sorted,
    de-duplicated list of (s, Reference), s = distance along the axis."""
    hits = []
    for line in members:
        for t, ref in collect_edge_hits(view, line.a, line.b, axis,
                                        include_foundations, stats):
            hits.append((line.s_a + t, ref))
    if add_overall and hits:
        # Only for a run that crosses the slab, so a jog between runs
        # doesn't get an overall string of its own.
        for s, ref in overall_extremes(view, axis, members,
                                       include_foundations):
            if all(abs(s - h) >= DEDUP_TOL for h, _ in hits):
                hits.append((s, ref))
                stats["overall"] += 1
    hits.sort(key=lambda h: h[0])
    deduped = []
    for s, ref in hits:
        if deduped and abs(s - deduped[-1][0]) < DEDUP_TOL:
            continue    # same edge crossed by two of the lines
        deduped.append((s, ref))
    return deduped


def dimension_group(view, axis, members, dim_type, include_foundations,
                    add_overall):
    """One dimension string for a group of parallel, connected lines.
    Returns (Dimension or None, message)."""
    stats = {"slabs": set(), "overall": 0}
    hits = group_hits(view, axis, members, include_foundations, stats,
                      add_overall)
    ids = ", ".join(str(line.element.Id) for line in members)
    label = "Line" if len(members) == 1 else "Lines"
    if len(hits) < 2:
        return None, ("%s %s: %d slab edge(s) found, no dimension made "
                      "(fine for a short jog between runs)."
                      % (label, ids, len(hits)))

    # The string sits on the longest line, stretched to cover them all.
    host = max(members, key=lambda line: line.length)
    s_min = min(line.s_a for line in members)
    s_max = max(line.s_b for line in members)
    p_start = host.a.Add(axis.Multiply(s_min - host.s_a))
    p_end = host.a.Add(axis.Multiply(s_max - host.s_a))
    dim_line = Line.CreateBound(XYZ(p_start.X, p_start.Y, host.z),
                                XYZ(p_end.X, p_end.Y, host.z))

    refs = ReferenceArray()
    for _, ref in hits:
        refs.Append(ref)
    if dim_type is not None:
        dim = doc.Create.NewDimension(view, dim_line, refs, dim_type)
    else:
        dim = doc.Create.NewDimension(view, dim_line, refs)

    msg = ("%s %s: %d slab edges dimensioned across %d slab(s)."
           % (label, ids, len(hits), len(stats["slabs"])))
    if stats["overall"]:
        msg += (" %d overall slab edge(s) added beyond the line."
                % stats["overall"])
    return dim, msg


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main():
    view = doc.ActiveView
    if not isinstance(view, ViewPlan):
        return [], ("Active view '%s' is not a plan view. Open the slab "
                    "plan you drew the line in and run again."
                    % safe_name(view))

    # A Boolean / number / text on IN[0] isn't a line: pick on screen.
    elements = [_unwrap(e) for e in _as_list(_in(0))
                if not isinstance(e, (bool, int, float, str))]
    if not elements:
        elements = pick_lines()
    if not elements:
        return [], "No line selected. Nothing done."

    report = ["Slab Auto-Dimension script version %s" % SCRIPT_VERSION]
    dim_type_name = _in(1)
    dim_type = find_dimension_type(dim_type_name)
    if dim_type_name and dim_type is None:
        report.append("Dimension type '%s' not found; used the default."
                      % dim_type_name)
    delete_line = bool(_in(2, False))
    include_foundations = bool(_in(3, True))
    add_overall = bool(_in(4, True))

    lines = []
    for element in elements:
        try:
            lines.append(DrawnLine(element, line_from_element(element)))
        except Exception as ex:
            report.append("FAILED: %s" % ex)

    dims = []
    TransactionManager.Instance.EnsureInTransaction(doc)
    try:
        for chain in connected_chains(lines):
            # One sub-transaction per chain: a failed chain leaves nothing
            # behind, the others still go through.
            st = SubTransaction(doc)
            st.Start()
            try:
                made = []
                for axis, members in direction_groups(chain):
                    dim, msg = dimension_group(view, axis, members, dim_type,
                                               include_foundations,
                                               add_overall)
                    report.append(msg)
                    if dim is not None:
                        made.append(dim)
                if made and delete_line:
                    for line in chain:
                        doc.Delete(line.element.Id)
                st.Commit()
                dims.extend(made)
            except Exception as ex:
                st.RollBack()
                report.append("FAILED: %s" % ex)
    finally:
        TransactionManager.Instance.TransactionTaskDone()

    if lines and not dims:
        report.append("No dimensions made. Check the line runs square to "
                      "the slab edges and fully across them, and that the "
                      "slabs are visible in this view.")
    return dims, "\n".join(report)


# ----------------------------------------------------------------------------
# Re-run on every click of Run
# ----------------------------------------------------------------------------

# Dynamo only re-runs a node whose inputs changed, so with nothing wired a
# second click of Run would do nothing. This marker lets the script find its
# own node and flag it as changed, so the next Run executes it again.
SELF_MARKER = "SLAB_DIMENSION_RERUN_MARKER"


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
