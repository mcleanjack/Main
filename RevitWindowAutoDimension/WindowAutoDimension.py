# ============================================================================
# Revit Window Auto-Dimension - dimension between windows in plan views
# ----------------------------------------------------------------------------
# Paste this entire file into a single Dynamo "Python Script" node, or open
# the ready-made WindowAutoDimension.dyn in the same folder.
#
# Engine: CPython3 (Dynamo 2.6+ / Revit 2022+ default). Also runs unmodified
# on the legacy IronPython2 engine.
#
# What it does:
#   1. Prompts you to click windows (and/or doors) in the active plan view.
#      Click as many as you like, then press "Finish" on the Options Bar
#      (or Enter). Esc cancels.
#   2. Groups the picked openings by their host wall. For each host wall it
#      builds ONE continuous dimension string, placed outside the wall's
#      exterior face, that runs:
#
#        outer face of the perpendicular wall before the first opening
#          -> jamb, jamb of opening 1
#          -> jamb, jamb of opening 2 ... (etc.)
#          -> outer face of the perpendicular wall after the last opening
#
#      "Perpendicular wall" = the nearest wall, visible in the view, that
#      runs perpendicular to the host wall and meets it beyond the outermost
#      picked opening. Its "outer" face is the face pointing away from the
#      openings (i.e. the external corner of the building for end walls).
#      If no such wall is found on a side, the host wall's own end face is
#      used instead.
#
#   Jambs are taken from the opening cut in the host wall itself (the wall
#   faces on each side of the rough/masonry opening), so the string snaps to
#   real, stable wall geometry. If an opening has no cut faces (e.g. a
#   family that doesn't cut the wall), its centreline reference is used.
#
# Inputs:
#   IN[0] Boolean  Run            - set True and run the graph to start picking.
#   IN[1] Number   Offset (mm)    - distance of the dimension line from the
#                                   host wall's exterior face (model mm).
#                                   Default 1000.
#   IN[2] String   Dimension Type - name of a linear dimension type to use.
#                                   Leave blank to use the project default.
#
# Output (OUT): [created Dimension elements, report lines]
# ============================================================================

import clr
import traceback

clr.AddReference('RevitAPI')
clr.AddReference('RevitAPIUI')
clr.AddReference('RevitServices')

from Autodesk.Revit.DB import (
    FilteredElementCollector, Wall, LocationCurve, LocationPoint, Line, XYZ,
    Options, Solid, PlanarFace, GeometryInstance, ReferenceArray,
    DimensionType, DimensionStyleType, BuiltInCategory, BuiltInParameter,
    FamilyInstanceReferenceType, UnitUtils, ViewPlan, ViewType
)
from Autodesk.Revit.UI.Selection import ObjectType
from Autodesk.Revit.Exceptions import OperationCanceledException
from RevitServices.Persistence import DocumentManager
from RevitServices.Transactions import TransactionManager

# ----------------------------------------------------------------------------
# Environment / inputs
# ----------------------------------------------------------------------------

doc = DocumentManager.Instance.CurrentDBDocument
uidoc = DocumentManager.Instance.CurrentUIApplication.ActiveUIDocument
view = uidoc.ActiveView


def _in(index, default):
    try:
        value = IN[index]
    except (NameError, IndexError):
        return default
    return default if value is None else value


RUN = bool(_in(0, True))
OFFSET_MM = float(_in(1, 1000.0))
DIM_TYPE_NAME = str(_in(2, "")).strip()

# Categories that can be picked. Doors are accepted too, since they sit in
# the same wall and are usually part of the same dimension string.
PICKABLE_CATEGORIES = (BuiltInCategory.OST_Windows, BuiltInCategory.OST_Doors)


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------

def mm(value):
    """Millimetres -> Revit internal feet, across old/new unit APIs."""
    try:
        from Autodesk.Revit.DB import UnitTypeId
        return UnitUtils.ConvertToInternalUnits(value, UnitTypeId.Millimeters)
    except Exception:
        from Autodesk.Revit.DB import DisplayUnitType
        return UnitUtils.ConvertToInternalUnits(
            value, DisplayUnitType.DUT_MILLIMETERS)


TOL_SAME = mm(1.0)       # two references closer than this are duplicates
TOL_SEARCH = mm(100.0)   # slack when matching faces / walls to openings


def eid_int(element_id):
    try:
        return element_id.Value            # Revit 2024+
    except AttributeError:
        return element_id.IntegerValue     # older versions


def is_pickable(el):
    cat = el.Category
    if cat is None:
        return False
    for bic in PICKABLE_CATEGORIES:
        try:
            if cat.BuiltInCategory == bic:  # Revit 2023+
                return True
        except AttributeError:
            if eid_int(cat.Id) == int(bic):
                return True
    return False


def flat(v):
    return XYZ(v.X, v.Y, 0)


def along(point, origin, direction):
    """Signed distance of point from origin, measured along direction."""
    return point.Subtract(origin).DotProduct(direction)


def type_name(element_type):
    p = element_type.get_Parameter(BuiltInParameter.SYMBOL_NAME_PARAM)
    return p.AsString() if p is not None else ""


def geometry_options():
    opts = Options()
    opts.ComputeReferences = True
    opts.IncludeNonVisibleObjects = False
    opts.View = view
    return opts


def planar_faces(element):
    """All planar faces (with references) of an element in the active view."""
    faces = []
    geo = element.get_Geometry(geometry_options())
    if geo is None:
        return faces
    solids = []
    for g in geo:
        if isinstance(g, Solid):
            solids.append(g)
        elif isinstance(g, GeometryInstance):
            for gi in g.GetInstanceGeometry():
                if isinstance(gi, Solid):
                    solids.append(gi)
    for solid in solids:
        if solid.Volume <= 0:
            continue
        for f in solid.Faces:
            if isinstance(f, PlanarFace) and f.Reference is not None:
                faces.append(f)
    return faces


def faces_across(element, origin, direction):
    """Faces whose normal is parallel to direction.

    Returns a list of (position along direction, sign of normal, reference).
    sign > 0 means the face points towards +direction.
    """
    result = []
    for f in planar_faces(element):
        dot = f.FaceNormal.DotProduct(direction)
        if abs(dot) > 0.999:
            result.append((along(f.Origin, origin, direction),
                           1 if dot > 0 else -1, f.Reference))
    return result


def find_dimension_type():
    if not DIM_TYPE_NAME:
        return None
    for dt in FilteredElementCollector(doc).OfClass(DimensionType):
        try:
            if dt.StyleType != DimensionStyleType.Linear:
                continue
        except Exception:
            pass
        if type_name(dt) == DIM_TYPE_NAME:
            return dt
    return None


# ----------------------------------------------------------------------------
# Core
# ----------------------------------------------------------------------------

def opening_span(opening, origin, direction):
    """(min, centre, max) of an opening's bounding box along direction."""
    centre = along(opening.Location.Point, origin, direction)
    bb = opening.get_BoundingBox(None)
    if bb is None:
        return centre, centre, centre
    values = [along(XYZ(x, y, origin.Z), origin, direction)
              for x in (bb.Min.X, bb.Max.X) for y in (bb.Min.Y, bb.Max.Y)]
    return min(values), centre, max(values)


def jamb_references(opening, host_faces, origin, direction, report):
    """References for the two jambs of an opening, as (position, reference)."""
    lo, centre, hi = opening_span(opening, origin, direction)
    # Low-side jamb: wall face that points INTO the opening (+direction).
    left = [(p, r) for p, s, r in host_faces
            if s > 0 and lo - TOL_SEARCH <= p < centre]
    # High-side jamb: wall face that points INTO the opening (-direction).
    right = [(p, r) for p, s, r in host_faces
             if s < 0 and centre < p <= hi + TOL_SEARCH]
    if left and right:
        # Tightest faces = the actual opening in the wall.
        return [max(left, key=lambda t: t[0]), min(right, key=lambda t: t[0])]

    report.append("Opening {} does not cut its host wall - dimensioned to its "
                  "centreline instead.".format(eid_int(opening.Id)))
    refs = opening.GetReferences(FamilyInstanceReferenceType.CenterLeftRight)
    if refs is not None and refs.Count > 0:
        return [(centre, refs[0])]
    report.append("Opening {} has no usable reference - skipped."
                  .format(eid_int(opening.Id)))
    return []


def perpendicular_walls(host, origin, direction, normal):
    """Walls in the view that run perpendicular to host and meet it.

    Returns a list of (position along host direction, wall).
    """
    host_line = host.Location.Curve
    s0 = along(host_line.GetEndPoint(0), origin, direction)
    s1 = along(host_line.GetEndPoint(1), origin, direction)
    s_min, s_max = min(s0, s1), max(s0, s1)
    host_half = host.Width / 2.0

    result = []
    walls = FilteredElementCollector(doc, view.Id).OfClass(Wall) \
        .WhereElementIsNotElementType()
    for w in walls:
        if eid_int(w.Id) == eid_int(host.Id):
            continue
        loc = w.Location
        if not isinstance(loc, LocationCurve) or not isinstance(loc.Curve, Line):
            continue
        line = loc.Curve
        if abs(flat(line.Direction).Normalize().DotProduct(direction)) > 0.01:
            continue  # not perpendicular
        p0, p1 = line.GetEndPoint(0), line.GetEndPoint(1)
        pos = along(p0, origin, direction)
        reach = w.Width / 2.0 + TOL_SEARCH
        if pos < s_min - reach or pos > s_max + reach:
            continue  # beyond the ends of the host wall
        n0, n1 = along(p0, origin, normal), along(p1, origin, normal)
        slack = host_half + w.Width / 2.0 + TOL_SEARCH
        if min(n0, n1) > slack or max(n0, n1) < -slack:
            continue  # doesn't touch the host wall
        result.append((pos, w))
    return result


def dimension_wall(host, openings, dim_type, report):
    line = host.Location.Curve
    origin = line.GetEndPoint(0)
    direction = flat(line.Direction).Normalize()
    exterior = flat(host.Orientation).Normalize()

    host_faces = faces_across(host, origin, direction)

    # --- Opening jambs --------------------------------------------------
    points = []
    spans = []
    for o in sorted(openings,
                    key=lambda e: along(e.Location.Point, origin, direction)):
        jambs = jamb_references(o, host_faces, origin, direction, report)
        points.extend(jambs)
        spans.extend(p for p, _ in jambs)
    if not points:
        report.append("Wall {}: nothing to dimension.".format(eid_int(host.Id)))
        return None
    first, last = min(spans), max(spans)

    # --- End references: perpendicular walls (or host wall ends) ---------
    perp = perpendicular_walls(host, origin, direction, exterior)
    before = [t for t in perp if t[0] < first]
    after = [t for t in perp if t[0] > last]

    def outer_face(wall, sign, pick):
        faces = [(p, r) for p, s, r in faces_across(wall, origin, direction)
                 if s == sign]
        return pick(faces, key=lambda t: t[0]) if faces else None

    start = None
    if before:
        start = outer_face(max(before, key=lambda t: t[0])[1], -1, min)
    if start is None:
        start = outer_face(host, -1, min)
        report.append("Wall {}: no perpendicular wall before the first "
                      "opening - using the wall end.".format(eid_int(host.Id)))
    end = None
    if after:
        end = outer_face(min(after, key=lambda t: t[0])[1], 1, max)
    if end is None:
        end = outer_face(host, 1, max)
        report.append("Wall {}: no perpendicular wall after the last "
                      "opening - using the wall end.".format(eid_int(host.Id)))

    for extra in (start, end):
        if extra is not None:
            points.append(extra)

    # --- Sort, de-duplicate, build the ReferenceArray --------------------
    points.sort(key=lambda t: t[0])
    ref_array = ReferenceArray()
    kept = []
    for pos, ref in points:
        if kept and abs(pos - kept[-1]) < TOL_SAME:
            continue
        ref_array.Append(ref)
        kept.append(pos)
    if ref_array.Size < 2:
        report.append("Wall {}: fewer than two references - skipped."
                      .format(eid_int(host.Id)))
        return None

    # --- Dimension line, parallel to the wall on its exterior side --------
    ext_faces = [along(f.Origin, origin, exterior)
                 for f in planar_faces(host)
                 if f.FaceNormal.DotProduct(exterior) > 0.999]
    ext_face = max(ext_faces) if ext_faces else host.Width / 2.0
    offset = ext_face + mm(OFFSET_MM)
    base = origin.Add(exterior.Multiply(offset))
    p_start = base.Add(direction.Multiply(kept[0]))
    p_end = base.Add(direction.Multiply(kept[-1]))
    dim_line = Line.CreateBound(p_start, p_end)

    if dim_type is not None:
        dim = doc.Create.NewDimension(view, dim_line, ref_array, dim_type)
    else:
        dim = doc.Create.NewDimension(view, dim_line, ref_array)
    report.append("Wall {}: created dimension with {} segments.".format(
        eid_int(host.Id), ref_array.Size - 1))
    return dim


def main():
    report = []
    dims = []

    if not RUN:
        return [dims, ["Set Run to True and run the graph to start."]]
    if not isinstance(view, ViewPlan) or view.ViewType not in (
            ViewType.FloorPlan, ViewType.CeilingPlan, ViewType.AreaPlan,
            ViewType.EngineeringPlan):
        return [dims, ["Open a floor plan view and run again."]]

    try:
        picked = uidoc.Selection.PickObjects(
            ObjectType.Element,
            "Click the windows to dimension, then press Finish (Esc to cancel)")
    except OperationCanceledException:
        return [dims, ["Selection cancelled."]]

    # Group openings by host wall.
    groups = {}
    for ref in picked:
        el = doc.GetElement(ref)
        if el is None or not is_pickable(el):
            continue
        host = getattr(el, "Host", None)
        if not isinstance(host, Wall):
            report.append("Opening {} is not hosted by a wall - skipped."
                          .format(eid_int(el.Id)))
            continue
        if not isinstance(host.Location.Curve, Line):
            report.append("Wall {} is curved - not supported."
                          .format(eid_int(host.Id)))
            continue
        if not isinstance(el.Location, LocationPoint):
            continue
        key = eid_int(host.Id)
        if key not in groups:
            groups[key] = (host, [])
        groups[key][1].append(el)

    if not groups:
        return [dims, report + ["No windows selected."]]

    dim_type = find_dimension_type()
    if DIM_TYPE_NAME and dim_type is None:
        report.append("Dimension type '{}' not found - using the default."
                      .format(DIM_TYPE_NAME))

    TransactionManager.Instance.EnsureInTransaction(doc)
    try:
        for host, openings in groups.values():
            try:
                dim = dimension_wall(host, openings, dim_type, report)
                if dim is not None:
                    dims.append(dim)
            except Exception as ex:
                report.append("Wall {}: failed - {}".format(
                    eid_int(host.Id), ex))
    finally:
        TransactionManager.Instance.TransactionTaskDone()

    return [dims, report]


try:
    OUT = main()
except Exception:
    OUT = [[], [traceback.format_exc()]]
