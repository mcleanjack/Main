# ============================================================================
# Revit Auto-Dimension - wall faces along a drawn line, room names below
# ----------------------------------------------------------------------------
# Paste this entire file into a single Dynamo "Python Script" node.
#
# Engine: CPython3 (Dynamo 2.6+ / Revit 2022+ default). Also runs unmodified
# on the legacy IronPython2 engine.
#
# What it does:
#   1. Takes one or more straight Detail Lines / Model Lines that you have
#      drawn across the building in a plan view (wired in, or picked on
#      screen when the graph runs).
#   2. Finds every wall face in the active plan view that the line crosses:
#      both side faces (exterior and interior) of every wall, so you get
#      each wall's thickness and each room's clear width.
#   3. Creates ONE continuous linear dimension string along the drawn line
#      through all of those faces.
#   4. For each segment of the string, looks up the Room at the segment's
#      midpoint and writes the room name into that segment's "Below" text,
#      so the room name sits underneath the numeric value. Segments that
#      span a wall's thickness have no room, so they stay blank.
#
# All changes happen in one Dynamo transaction: a single Ctrl+Z in Revit
# undoes the whole run.
#
# Inputs (all optional):
#   IN[0]  Line element(s): a Detail Line / Model Line, or a list of them,
#          e.g. from "Select Model Element(s)". If nothing is wired, you are
#          asked to click a line in the active view when the graph runs.
#   IN[1]  Dimension type name (string), e.g. "Linear - 2.5mm Arial". Blank
#          = the project's default linear dimension type.
#   IN[2]  Include room number (bool). True gives "101 Kitchen". Default
#          False = name only.
#   IN[3]  Delete the drawn line afterwards (bool). Default False.
#
# Output (OUT):
#   [0] list of created Dimension elements
#   [1] status / report text (one line per input line, plus skips)
# ============================================================================

import clr
import math
import traceback

clr.AddReference('RevitAPI')
clr.AddReference('RevitAPIUI')
clr.AddReference('RevitServices')

from Autodesk.Revit.DB import (
    FilteredElementCollector, Wall, WallKind, CurveElement, Line, XYZ,
    UV, ReferenceArray, HostObjectUtils, ShellLayerType, PlanarFace,
    ViewPlan, PlanViewPlane, DimensionType, DimensionStyleType,
    BuiltInParameter
)
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

# Internal units are decimal feet.
DEDUP_TOL = 0.003            # ~1 mm: faces closer than this collapse to one
PARALLEL_COS = math.cos(math.radians(1.0))   # face must be within 1 deg of
                                             # perpendicular to the line
ROOM_PROBE_HEIGHT = 1.0      # probe rooms 1 ft above the view's level


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


def get_view_phase(view):
    try:
        p = view.get_Parameter(BuiltInParameter.VIEW_PHASE)
        if p is not None:
            return doc.GetElement(p.AsElementId())
    except Exception:
        pass
    return None


def get_level_z(view):
    level = view.GenLevel
    return level.ProjectElevation if level is not None else 0.0


def get_cut_plane_z(view):
    """Absolute Z of the view's cut plane, or None if unavailable."""
    try:
        vr = view.GetViewRange()
        level = doc.GetElement(vr.GetLevelId(PlanViewPlane.CutPlane))
        return level.ProjectElevation + vr.GetOffset(PlanViewPlane.CutPlane)
    except Exception:
        return None


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


def room_label(room, include_number):
    if room is None:
        return ""
    name = ""
    try:
        p = room.get_Parameter(BuiltInParameter.ROOM_NAME)
        name = p.AsString() if p is not None else ""
    except Exception:
        pass
    if include_number:
        try:
            number = room.Number or ""
        except Exception:
            number = ""
        return (number + " " + (name or "")).strip()
    return name or ""


def find_room(point_xy, room_z, phase):
    probe = XYZ(point_xy.X, point_xy.Y, room_z)
    try:
        if phase is not None:
            return doc.GetRoomAtPoint(probe, phase)
        return doc.GetRoomAtPoint(probe)
    except Exception:
        return None


# ----------------------------------------------------------------------------
# Getting the drawn line
# ----------------------------------------------------------------------------

def pick_lines():
    """Ask the user to click a line in the active view."""
    if uidoc is None:
        return []
    try:
        ref = uidoc.Selection.PickObject(
            ObjectType.Element,
            "Auto-Dimension: pick the line you drew across the walls")
    except OperationCanceledException:
        return []
    return [doc.GetElement(ref.ElementId)]


def line_from_element(element):
    """Return the element's bound Line, or raise ValueError."""
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
# Intersecting the line with wall faces
# ----------------------------------------------------------------------------

def collect_walls(view):
    return [w for w in FilteredElementCollector(doc, view.Id).OfClass(Wall)]


def wall_bbox_hits_line(wall, view, a, b):
    """Cheap 2D reject: does the line's XY box overlap the wall's XY box?"""
    bb = wall.get_BoundingBox(view) or wall.get_BoundingBox(None)
    if bb is None:
        return True
    pad = 0.1
    return not (max(a.X, b.X) < bb.Min.X - pad or min(a.X, b.X) > bb.Max.X + pad
                or max(a.Y, b.Y) < bb.Min.Y - pad
                or min(a.Y, b.Y) > bb.Max.Y + pad)


def face_mid_z(face):
    try:
        bb = face.GetBoundingBox()
        uv = UV((bb.Min.U + bb.Max.U) / 2.0, (bb.Min.V + bb.Max.V) / 2.0)
        return face.Evaluate(uv).Z
    except Exception:
        return None


def intersect_face(face, a, b, direction, z_candidates):
    """Intersect the drawn segment a->b (flattened, then lifted to each
    candidate Z) with a planar wall face. Returns the distance along the
    line from a, or None if the line misses the face.

    Also returns None if the face isn't square to the line: a linear
    dimension can only measure between faces perpendicular to it."""
    if not isinstance(face, PlanarFace):
        return None     # curved walls can't take a linear dimension
    n = flat(face.FaceNormal)
    if n.GetLength() < 1e-9:
        return None
    n = n.Normalize()
    if abs(n.DotProduct(direction)) < PARALLEL_COS:
        return None

    origin = face.Origin
    length = a.DistanceTo(b)
    denom = n.DotProduct(direction)
    for z in z_candidates:
        if z is None:
            continue
        a_z = XYZ(a.X, a.Y, z)
        t = n.DotProduct(flat(origin.Subtract(a_z))) / denom
        if t < -1e-6 or t > length + 1e-6:
            continue        # plane is off the ends of the drawn line
        p = a_z.Add(direction.Multiply(t))
        # Face.Project returns None when p is outside the bounded face,
        # which rejects faces the line only hits on their infinite plane.
        try:
            res = face.Project(p)
        except Exception:
            res = None
        if res is not None and res.Distance < 0.01:
            return t
    return None


def collect_face_hits(view, a, b, direction, skipped):
    """Return a list of (distance_along_line, Reference) for every wall
    side face crossed by the line."""
    cut_z = get_cut_plane_z(view)
    level_z = get_level_z(view)
    hits = []
    for wall in collect_walls(view):
        if not wall_bbox_hits_line(wall, view, a, b):
            continue
        try:
            if wall.WallType.Kind == WallKind.Curtain:
                skipped.append("Curtain wall %s skipped" % wall.Id)
                continue
        except Exception:
            pass
        for side in (ShellLayerType.Exterior, ShellLayerType.Interior):
            try:
                refs = HostObjectUtils.GetSideFaces(wall, side)
            except Exception:
                continue
            for ref in refs:
                try:
                    face = wall.GetGeometryObjectFromReference(ref)
                except Exception:
                    continue
                if face is None:
                    continue
                zs = [cut_z, level_z + ROOM_PROBE_HEIGHT, face_mid_z(face)]
                t = intersect_face(face, a, b, direction, zs)
                if t is not None:
                    hits.append((t, ref))
    hits.sort(key=lambda h: h[0])

    # Drop coincident faces (e.g. flush faces of two joined walls);
    # Revit rejects zero-length dimension segments.
    deduped = []
    for t, ref in hits:
        if deduped and abs(t - deduped[-1][0]) < DEDUP_TOL:
            continue
        deduped.append((t, ref))
    return deduped


# ----------------------------------------------------------------------------
# Building the dimension
# ----------------------------------------------------------------------------

def dimension_along_line(view, line_elem, dim_type, include_number,
                         phase, room_z):
    curve = line_from_element(line_elem)
    a = flat(curve.GetEndPoint(0))
    b = flat(curve.GetEndPoint(1))
    direction = b.Subtract(a).Normalize()

    skipped = []
    hits = collect_face_hits(view, a, b, direction, skipped)
    if len(hits) < 2:
        raise ValueError(
            "Line %s crosses %d wall face(s) square to it; need at least 2. "
            "Check the line runs perpendicular to the walls and fully "
            "through them." % (line_elem.Id, len(hits)))

    refs = ReferenceArray()
    for _, ref in hits:
        refs.Append(ref)

    # Dimension line sits exactly where the user drew it.
    dim_line = Line.CreateBound(curve.GetEndPoint(0), curve.GetEndPoint(1))
    if dim_type is not None:
        dim = doc.Create.NewDimension(view, dim_line, refs, dim_type)
    else:
        dim = doc.Create.NewDimension(view, dim_line, refs)
    doc.Regenerate()

    # Room names under the value of each segment.
    labelled = 0
    if dim.NumberOfSegments == 0:
        # Only two references: the dimension itself is the one segment.
        label = room_label(find_room(dim.Origin, room_z, phase),
                           include_number)
        if label:
            dim.Below = label
            labelled += 1
    else:
        for seg in dim.Segments:
            label = room_label(find_room(seg.Origin, room_z, phase),
                               include_number)
            if label:
                seg.Below = label
                labelled += 1

    msg = ("Line %s: %d wall faces dimensioned, %d room label(s)."
           % (line_elem.Id, len(hits), labelled))
    if skipped:
        msg += " " + "; ".join(skipped) + "."
    return dim, msg


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main():
    view = doc.ActiveView
    if not isinstance(view, ViewPlan):
        return [], ("Active view '%s' is not a plan view. Open the floor "
                    "plan you drew the line in and run again."
                    % safe_name(view))

    elements = [_unwrap(e) for e in _as_list(_in(0))]
    if not elements:
        elements = pick_lines()
    if not elements:
        return [], "No line selected. Nothing done."

    dim_type_name = _in(1)
    dim_type = find_dimension_type(dim_type_name)
    report = []
    if dim_type_name and dim_type is None:
        report.append("Dimension type '%s' not found; used the default."
                      % dim_type_name)

    include_number = bool(_in(2, False))
    delete_line = bool(_in(3, False))
    phase = get_view_phase(view)
    room_z = get_level_z(view) + ROOM_PROBE_HEIGHT

    dims = []
    TransactionManager.Instance.EnsureInTransaction(doc)
    try:
        for element in elements:
            try:
                dim, msg = dimension_along_line(
                    view, element, dim_type, include_number, phase, room_z)
                dims.append(dim)
                report.append(msg)
                if delete_line:
                    doc.Delete(element.Id)
            except Exception as ex:
                report.append("FAILED: %s" % ex)
    finally:
        TransactionManager.Instance.TransactionTaskDone()

    return dims, "\n".join(report)


try:
    OUT = main()
except Exception:
    OUT = [], "Unexpected error:\n" + traceback.format_exc()
