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
#   2. Finds every wall the line crosses in the active plan view and
#      picks two faces to dimension on each, in this order of preference:
#        a. the two faces of the wall's 90mm layer (IN[5]), e.g. the stud
#           framing, not the plasterboard or cladding either side;
#        b. otherwise the two faces of the wall's CORE (IN[4]);
#        c. otherwise the finished (outer) faces.
#   3. Creates ONE continuous linear dimension string along the drawn line
#      through all of those faces.
#   4. For each segment of the string, looks up the Room at the segment's
#      midpoint and writes the room name into that segment's "Below" text,
#      so the room name sits underneath the numeric value. Segments that
#      span a wall's core have no room, so they stay blank.
#
# References: Revit only exposes a wall's finished faces and its core
# faces. Core faces have no documented API, so this uses the community-
# established reference "<UniqueId>:-9999:<n>", and checks each candidate
# by measuring it against the wall type's layer thicknesses before use.
#   - If the 90mm layer IS the core (or sits against a finished face), the
#     dimension is attached to the wall and follows it if it moves.
#   - If it isn't (e.g. core boundaries drawn around other layers too),
#     Revit has nothing on the wall to attach to, so a 100mm detail line in
#     '<Invisible lines>' style is placed at that face as an anchor
#     (Comments = "AutoDim anchor"). Those dimensions won't follow the wall;
#     fix the wall type's core boundaries to avoid them.
# Walls with no 90mm layer, or whose core can't be verified (stacked walls,
# a line ending inside the wall, etc.), fall back as above and the report
# says so.
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
#   IN[4]  Core fallback (bool). Default True = walls without the IN[5]
#          layer are dimensioned to their core. False = finished faces.
#   IN[5]  Layer thickness to dimension to, in mm (number). Default 90.
#          0 turns this off (core / finished faces only).
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
    UV, Reference, ReferenceArray, SubTransaction, HostObjectUtils,
    ShellLayerType, PlanarFace, MaterialFunctionAssignment,
    BuiltInCategory, GraphicsStyleType,
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
CORE_CHECK_TOL = 0.002       # ~0.6 mm: core ref must measure within this
# "<UniqueId>:-9999:<n>" indices tried for core faces (see core_reference).
# Community findings: 1 = wall centre, 2/3 = the two core faces,
# 4 = core centre. Each candidate is verified by measurement before use.
CORE_INDEX_CANDIDATES = [2, 3]
MM = 1.0 / 304.8
DEFAULT_TARGET_MM = 90.0     # layer to dimension to (IN[5])
LAYER_MATCH_TOL = 0.5 * MM   # a layer "is 90mm" if within 0.5 mm
ANCHOR_HALF_LENGTH = 50 * MM # anchor lines are 100 mm long
ANCHOR_COMMENT = "AutoDim anchor"


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


def layer_target(value):
    """IN[5] in mm -> internal feet, or None to turn layer matching off."""
    try:
        mm = float(value)
    except (TypeError, ValueError):
        return None
    return mm * MM if mm > 0 else None


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
    candidate Z) with a planar wall face. Returns (distance along the
    line from a, |cos| of the angle between line and face normal), or None
    if the line misses the face.

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
    cos_angle = abs(denom)
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
            return t, cos_angle
    return None


def wall_side_hits(wall, side, a, b, direction, zs_base):
    """[(t, cos_angle, Reference)] for this wall's side faces on one side
    that the line crosses."""
    hits = []
    try:
        refs = HostObjectUtils.GetSideFaces(wall, side)
    except Exception:
        return hits
    for ref in refs:
        try:
            face = wall.GetGeometryObjectFromReference(ref)
        except Exception:
            continue
        if face is None:
            continue
        hit = intersect_face(face, a, b, direction,
                             zs_base + [face_mid_z(face)])
        if hit is not None:
            hits.append((hit[0], hit[1], ref))
    return hits


def wall_layers(wall):
    """(widths, functions, first_core, last_core) from the wall type's
    compound structure, exterior layer first (matching
    ShellLayerType.Exterior). None for stacked/curtain/unlayered walls."""
    try:
        cs = wall.WallType.GetCompoundStructure()
    except Exception:
        return None
    if cs is None:
        return None
    layers = list(cs.GetLayers())
    if not layers:
        return None
    widths = [layer.Width for layer in layers]
    functions = [layer.Function for layer in layers]
    return (widths, functions,
            cs.GetFirstCoreLayerIndex(), cs.GetLastCoreLayerIndex())


def target_layer_offsets(layers, target):
    """Distances in from the exterior finish face to the two faces of the
    layer that is `target` thick, or None if the wall has no such layer.
    If several layers match, prefer a Structure layer, then one inside the
    core, then the one nearest the exterior."""
    widths, functions, first, last = layers
    matches = [i for i, w in enumerate(widths)
               if abs(w - target) < LAYER_MATCH_TOL]
    if not matches:
        return None

    def rank(i):
        return (0 if functions[i] == MaterialFunctionAssignment.Structure
                else 1,
                0 if first <= i <= last else 1,
                i)
    k = min(matches, key=rank)
    start = sum(widths[:k])
    return start, start + widths[k]


def core_offsets(layers):
    """Distances in from the exterior finish face to the two core faces,
    or None if the wall type has no core boundaries."""
    widths, _, first, last = layers
    if first < 0 or last < first:
        return None
    return sum(widths[:first]), sum(widths[:last + 1])


def core_reference(wall, index):
    """Build a reference to one of the wall's internal planes.

    The Revit API has no documented call for core faces. The widely used
    workaround is the stable representation "<UniqueId>:-9999:<n>", where
    n selects the wall centre / core faces / core centre. Which n is which
    is undocumented, so callers must verify the result (see ref_at_offset)."""
    try:
        return Reference.ParseFromStableRepresentation(
            doc, "%s:-9999:%d" % (wall.UniqueId, index))
    except Exception:
        return None


def measure(view, dim_line, ref_a, ref_b):
    """Length of a throwaway dimension between two references, or None.
    Runs in a SubTransaction that is always rolled back."""
    st = SubTransaction(doc)
    st.Start()
    try:
        ra = ReferenceArray()
        ra.Append(ref_a)
        ra.Append(ref_b)
        d = doc.Create.NewDimension(view, dim_line, ra)
        doc.Regenerate()
        v = d.Value
        return float(v) if v is not None else None
    except Exception:
        return None
    finally:
        st.RollBack()


def ref_at_offset(wall, view, dim_line, ref_ext, ref_int, offset, total):
    """A reference on the wall itself that lies `offset` in from the
    exterior finish face: a finish face or a core face (verified by
    measuring it). None if the wall has no reference there."""
    if offset < DEDUP_TOL:
        return ref_ext
    if total - offset < DEDUP_TOL:
        return ref_int
    for index in CORE_INDEX_CANDIDATES:
        ref = core_reference(wall, index)
        if ref is None:
            continue
        v = measure(view, dim_line, ref_ext, ref)
        if v is not None and abs(v - offset) < CORE_CHECK_TOL:
            return ref
    return None


def invisible_line_style():
    """The '<Invisible lines>' line style, or None if it can't be found."""
    try:
        lines = doc.Settings.Categories.get_Item(BuiltInCategory.OST_Lines)
        for sub in lines.SubCategories:
            if "invisible" in sub.Name.lower():
                return sub.GetGraphicsStyle(GraphicsStyleType.Projection)
    except Exception:
        pass
    return None


def make_anchor(view, start, direction, t, style):
    """Short detail line square to the dimension line at distance t along
    it, for layer faces Revit gives no wall reference to. Returns the
    line's reference."""
    p = start.Add(direction.Multiply(t))
    perp = XYZ(-direction.Y, direction.X, 0.0)
    seg = Line.CreateBound(p.Subtract(perp.Multiply(ANCHOR_HALF_LENGTH)),
                           p.Add(perp.Multiply(ANCHOR_HALF_LENGTH)))
    dc = doc.Create.NewDetailCurve(view, seg)
    if style is not None:
        try:
            dc.LineStyle = style
        except Exception:
            pass
    try:
        dc.get_Parameter(
            BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS).Set(ANCHOR_COMMENT)
    except Exception:
        pass
    return dc.GeometryCurve.Reference


def offset_hits(wall, view, dim_line, ext, inn, layers, offsets, anchor):
    """Dimension points for one wall at the given distances in from its
    exterior finish face. ext / inn are (t, cos_angle, ref) of the finish
    faces the line crosses. `anchor(t)` makes a stand-in reference where
    the wall has none; if anchor is None such walls return None.
    Returns ([(t, ref), ...], anchors_made) or None."""
    total = sum(layers[0])
    t_ext, cos_ext, ref_ext = ext
    t_int, _, ref_int = inn
    step = 1.0 if t_int > t_ext else -1.0
    out = []
    made = 0
    for offset in offsets:
        t = t_ext + step * offset / cos_ext
        ref = ref_at_offset(wall, view, dim_line, ref_ext, ref_int,
                            offset, total)
        if ref is None:
            if anchor is None:
                return None
            try:
                ref = anchor(t)
            except Exception:
                return None
            made += 1
        out.append((t, ref))
    return out, made


def collect_face_hits(view, a, b, direction, dim_line, target, core_only,
                      stats, notes):
    """Return a sorted, de-duplicated list of (distance_along_line,
    Reference) for every wall face to dimension along the line.

    Per wall, in order of preference:
      1. the faces of its `target`-thick layer (e.g. 90mm framing),
      2. its core faces (if core_only),
      3. its finished faces."""
    cut_z = get_cut_plane_z(view)
    level_z = get_level_z(view)
    zs_base = [cut_z, level_z + ROOM_PROBE_HEIGHT]
    start = XYZ(a.X, a.Y, dim_line.GetEndPoint(0).Z)
    style = invisible_line_style()

    def anchor(t):
        return make_anchor(view, start, direction, t, style)

    hits = []
    for wall in collect_walls(view):
        if not wall_bbox_hits_line(wall, view, a, b):
            continue
        try:
            if wall.WallType.Kind == WallKind.Curtain:
                notes.append("Curtain wall %s skipped" % wall.Id)
                continue
        except Exception:
            pass
        ext = wall_side_hits(wall, ShellLayerType.Exterior, a, b,
                             direction, zs_base)
        inn = wall_side_hits(wall, ShellLayerType.Interior, a, b,
                             direction, zs_base)
        if not ext and not inn:
            continue

        layers = wall_layers(wall)
        clean = layers is not None and len(ext) == 1 and len(inn) == 1

        if clean and target:
            offsets = target_layer_offsets(layers, target)
            res = offsets and offset_hits(wall, view, dim_line, ext[0],
                                          inn[0], layers, offsets, anchor)
            if res:
                hits.extend(res[0])
                stats["layer"] += 1
                stats["anchors"] += res[1]
                continue
        if clean and core_only:
            offsets = core_offsets(layers)
            res = offsets and offset_hits(wall, view, dim_line, ext[0],
                                          inn[0], layers, offsets, None)
            if res:
                hits.extend(res[0])
                stats["core"] += 1
                continue
        if core_only:
            notes.append("Wall %s: core faces not found, used finish faces"
                         % wall.Id)
        hits.extend((t, ref) for t, _, ref in ext + inn)
        stats["finish"] += 1

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
                         phase, room_z, core_only, target):
    curve = line_from_element(line_elem)
    a = flat(curve.GetEndPoint(0))
    b = flat(curve.GetEndPoint(1))
    direction = b.Subtract(a).Normalize()

    # Dimension line sits exactly where the user drew it.
    dim_line = Line.CreateBound(curve.GetEndPoint(0), curve.GetEndPoint(1))

    notes = []
    stats = {"layer": 0, "core": 0, "finish": 0, "anchors": 0}
    hits = collect_face_hits(view, a, b, direction, dim_line, target,
                             core_only, stats, notes)
    if len(hits) < 2:
        raise ValueError(
            "Line %s crosses %d wall face(s) square to it; need at least 2. "
            "Check the line runs perpendicular to the walls and fully "
            "through them." % (line_elem.Id, len(hits)))

    refs = ReferenceArray()
    for _, ref in hits:
        refs.Append(ref)

    doc.Regenerate()    # anchor lines need their geometry before use
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

    parts = []
    if target:
        parts.append("%d on %gmm layer" % (stats["layer"],
                                           round(target * 304.8, 1)))
    if core_only:
        parts.append("%d on core" % stats["core"])
    parts.append("%d on finish faces" % stats["finish"])
    msg = ("Line %s: %d faces dimensioned (walls: %s), %d room label(s)."
           % (line_elem.Id, len(hits), ", ".join(parts), labelled))
    if stats["anchors"]:
        msg += (" %d invisible anchor line(s) added where the layer isn't "
                "the wall's core (Comments = '%s')."
                % (stats["anchors"], ANCHOR_COMMENT))
    if notes:
        msg += " " + "; ".join(notes) + "."
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
    core_only = bool(_in(4, True))
    target = layer_target(_in(5, DEFAULT_TARGET_MM))
    phase = get_view_phase(view)
    room_z = get_level_z(view) + ROOM_PROBE_HEIGHT

    dims = []
    TransactionManager.Instance.EnsureInTransaction(doc)
    try:
        for element in elements:
            # One sub-transaction per line: a failed line leaves nothing
            # behind (e.g. anchor lines), the others still go through.
            st = SubTransaction(doc)
            st.Start()
            try:
                dim, msg = dimension_along_line(
                    view, element, dim_type, include_number, phase, room_z,
                    core_only, target)
                if delete_line:
                    doc.Delete(element.Id)
                st.Commit()
                dims.append(dim)
                report.append(msg)
            except Exception as ex:
                st.RollBack()
                report.append("FAILED: %s" % ex)
    finally:
        TransactionManager.Instance.TransactionTaskDone()

    return dims, "\n".join(report)


try:
    OUT = main()
except Exception:
    OUT = [], "Unexpected error:\n" + traceback.format_exc()
