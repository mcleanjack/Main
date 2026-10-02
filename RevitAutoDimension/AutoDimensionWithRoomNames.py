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
#   2. Finds every wall the line crosses in the active plan view,
#      including where it passes through a hosted window or door (the
#      wall above the head / below the sill is used), and
#      dimensions ONLY to the faces of its Structure [1] layer(s), as set
#      in the wall type (Edit Type > Structure > Edit, Function column),
#      e.g. the 90mm timber frame, not the plasterboard either side.
#      BRICK EXTERNAL walls (wall type Function = Exterior, with "brick" in
#      a layer material or the type name) are dimensioned from
#      their outer face (e.g. the outside of the brick) to the INNER face
#      of the Structure layer, as one segment: brick 110 + cavity 40 +
#      frame 90 = 240. A brick skin modelled as its own external wall (no
#      Structure layer) gives just its outer face. External walls without
#      brick (e.g. weatherboard / cladding) are treated like internal
#      walls: both faces of the Structure layer.
#      Other walls with no Structure layer are skipped (IN[4] can include
#      them).
#   3. Creates ONE continuous linear dimension string through all of those
#      faces. Lines drawn as a connected stepped path (ends touching) are
#      treated as one: all the runs going the same direction are merged
#      into one string on the longest run, picking up every wall any of
#      them cross. A short jog between runs that crosses no walls is
#      ignored. Separate (unconnected) lines each get their own string.
#   4. For each segment of the string, looks up the Room at the segment's
#      midpoint and writes the room name into that segment's "Below" text,
#      so the room name sits underneath the numeric value. Segments that
#      span a wall's core have no room, so they stay blank.
#
# References: Revit only exposes a wall's finished faces and its core
# faces. Core faces have no documented API, so this uses the community-
# established reference "<UniqueId>:-9999:<n>", and checks each candidate
# by measuring it against the wall type's layer thicknesses before use.
# So each face of the Structure layer must be a Core Boundary (or a
# finished face): set the wall type up with the Core Boundary rows
# directly either side of the Structure layer. Walls where that isn't so,
# walls with no Structure layer, stacked walls, and walls the line ends
# inside are skipped and named in the report.
#
# All changes happen in one Dynamo transaction: a single Ctrl+Z in Revit
# undoes the whole run.
#
# Inputs (all optional):
#   IN[0]  Line element(s): a Detail Line / Model Line, or a list of them,
#          e.g. from "Select Model Element(s)". If nothing is wired, you are
#          asked to click the line(s) in the active view when the graph
#          runs; click Finish on the Options Bar (or press Enter) when done.
#          A Boolean here also means "pick on screen". A NUMBER here also
#          means "pick on screen" and is used as the wall pick-up height
#          in mm (see IN[5]), so a single Number node on IN[0] is enough.
#   IN[1]  Dimension type name (string), e.g. "Linear - 2.5mm Arial". Blank
#          = the project's default linear dimension type.
#   IN[2]  Include room number (bool). True gives "101 Kitchen". Default
#          False = name only.
#   IN[3]  Delete the drawn line afterwards (bool). Default False.
#   IN[4]  Include walls with no Structure layer (bool). Default False =
#          skip them. True = dimension them to their core faces, or their
#          finished faces if they have no core.
#   IN[5]  Wall pick-up height in mm above the view's level (number), e.g.
#          1200. Walls are only picked up where the line crosses them at
#          this height. Blank = automatic: the view's cut plane first,
#          then heights up the whole wall (so a line through a window or
#          door still finds the wall above the head / below the sill).
#          If no height is wired in (IN[5] or a number on IN[0]), a pop-up
#          asks for it each run, remembering the last value, with an
#          "Automatic" tick box.
#
# Re-running: Dynamo normally only re-runs a node when one of its inputs
# changes. In Manual run mode this script flags its own node after each
# run, so every click of Run executes it again with no toggling needed
# (see flag_self_for_rerun at the bottom). Don't use Automatic mode.
#
# Output (OUT):
#   [0] list of created Dimension elements
#   [1] status / report text (one line per dimension string, plus skips)
# ============================================================================

import clr
import math
import os
import tempfile
import traceback

clr.AddReference('RevitAPI')
clr.AddReference('RevitAPIUI')
clr.AddReference('RevitServices')

from Autodesk.Revit.DB import (
    FilteredElementCollector, Wall, WallKind, CurveElement, Line, XYZ,
    UV, Reference, ReferenceArray, SubTransaction, HostObjectUtils,
    ShellLayerType, PlanarFace, MaterialFunctionAssignment, WallFunction,
    ViewPlan, PlanViewPlane, DimensionType, DimensionStyleType,
    BuiltInParameter, BuiltInCategory, RevitLinkInstance,
    ElementMulticategoryFilter
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
SCRIPT_VERSION = "2026-10-02 brick-1"

# Internal units are decimal feet.
DEDUP_TOL = 0.003            # ~1 mm: faces closer than this collapse to one
PARALLEL_COS = math.cos(math.radians(1.0))   # face must be within 1 deg of
                                             # perpendicular to the line
ROOM_PROBE_HEIGHT = 1.0      # probe rooms 1 ft above the view's level
FACE_Z_SAMPLES = 24          # heights tried up each wall face (openings)
MM = 1.0 / 304.8              # feet per mm
CONNECT_TOL = 0.1            # ~30 mm: line ends this close are "connected"
CORE_CHECK_TOL = 0.002       # ~0.6 mm: core ref must measure within this
# "<UniqueId>:-9999:<n>" indices tried for core faces (see core_reference).
# Community findings: 1 = wall centre, 2/3 = the two core faces,
# 4 = core centre. That numbering is undocumented and may differ for walls
# with more layers, so 2 and 3 are tried first, then 1 and 4-12. Each
# candidate is verified by measurement before use, so a wrong one is never
# used.
CORE_INDEX_CANDIDATES = [2, 3, 1] + list(range(4, 13))
# Per wall: what each candidate measured, for the report if none fit.
CORE_DIAG = {}


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


def height_mm(value):
    """IN[5] as a number of mm, or None if blank / not a number. A
    Boolean (e.g. an old Refresh toggle left wired here) counts as blank."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value).strip().lower().replace("mm", ""))
    except (TypeError, ValueError):
        return None


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


def eid_int(element_id):
    try:
        return int(element_id.Value)            # Revit 2024+
    except AttributeError:
        return int(element_id.IntegerValue)     # older Revit


# Categories used to work out which design options a view is showing.
OPTION_PROBE_CATEGORIES = [
    "OST_Walls", "OST_Floors", "OST_Doors", "OST_Windows", "OST_Roofs",
    "OST_Rooms", "OST_Ceilings", "OST_Stairs", "OST_Railings",
    "OST_Columns", "OST_StructuralColumns", "OST_Furniture",
    "OST_Casework", "OST_PlumbingFixtures", "OST_GenericModel",
]


def visible_design_options(view):
    """Ids (ints) of the design options shown in this view, found from
    the design options of the elements Revit shows in it."""
    cats = NetList[BuiltInCategory]()
    for name in OPTION_PROBE_CATEGORIES:
        bic = getattr(BuiltInCategory, name, None)
        if bic is not None:
            cats.Add(bic)
    ids = set()
    try:
        elements = (FilteredElementCollector(doc, view.Id)
                    .WherePasses(ElementMulticategoryFilter(cats))
                    .WhereElementIsNotElementType())
    except Exception:
        return ids
    for element in elements:
        try:
            option = element.DesignOption
            if option is not None:
                ids.add(eid_int(option.Id))
        except Exception:
            continue
    return ids


def room_option(room):
    """(design option id as int, is primary) of a room, or (None, True)
    for a room in the main model."""
    try:
        option = room.DesignOption
    except Exception:
        option = None
    if option is None:
        return None, True
    try:
        primary = bool(option.IsPrimary)
    except Exception:
        primary = False
    return eid_int(option.Id), primary


class RoomFinder(object):
    """Finds the room at a plan point on the view's level, in this model
    or in any loaded linked model (rooms are often in a linked
    architectural model even when their tags show in this one).

    Rooms in the view's phase are preferred, but rooms in other phases are
    still used if nothing else is there, so a phase mismatch doesn't mean
    no labels at all.

    Design options: only rooms in the main model or in a design option
    this view is showing are used, so where two options overlap the room
    names come from the option you can see. In linked models, only main
    model and primary option rooms are used."""

    def __init__(self, view, room_z, phase):
        self.room_z = room_z
        self.level_z = room_z - ROOM_PROBE_HEIGHT
        self.phase_name = safe_name(phase) if phase is not None else ""
        self.shown_options = visible_design_options(view)
        self.hidden_option_rooms = 0
        self.host = self._placed_rooms(doc, self._host_room_shown)
        self.links = []
        for inst in FilteredElementCollector(doc).OfClass(RevitLinkInstance):
            try:
                link_doc = inst.GetLinkDocument()
                if link_doc is None:
                    continue    # link not loaded
                rooms = self._placed_rooms(link_doc, self._link_room_shown)
                if rooms:
                    inverse = inst.GetTotalTransform().Inverse
                    self.links.append((inverse, rooms))
            except Exception:
                continue

    def _host_room_shown(self, room):
        option_id, _ = room_option(room)
        return option_id is None or option_id in self.shown_options

    @staticmethod
    def _link_room_shown(room):
        option_id, primary = room_option(room)
        return option_id is None or primary

    def _placed_rooms(self, source_doc, shown):
        """[(room, bounding box, phase name)] for placed, enclosed rooms
        that shown(room) accepts (design option visible in the view)."""
        out = []
        rooms = (FilteredElementCollector(source_doc)
                 .OfCategory(BuiltInCategory.OST_Rooms)
                 .WhereElementIsNotElementType())
        for room in rooms:
            try:
                if room.Area <= 0:
                    continue    # not placed, or not enclosed
                if not shown(room):
                    self.hidden_option_rooms += 1
                    continue    # in a design option this view isn't showing
                bb = room.get_BoundingBox(None)
                if bb is None:
                    continue
                phase_name = ""
                p = room.get_Parameter(BuiltInParameter.ROOM_PHASE)
                if p is not None:
                    phase_name = safe_name(source_doc.GetElement(
                        p.AsElementId()))
                out.append((room, bb, phase_name))
            except Exception:
                continue
        return out

    def counts(self):
        return len(self.host), sum(len(r) for _, r in self.links)

    def _search(self, rooms, x, y, level_z, probe_z):
        candidates = [(room, bb) for room, bb, phase_name in
                      sorted(rooms, key=lambda r: r[2] != self.phase_name)
                      if bb.Min.X - 0.01 <= x <= bb.Max.X + 0.01
                      and bb.Min.Y - 0.01 <= y <= bb.Max.Y + 0.01
                      # room sits on this level (base within 3 ft of it)
                      and abs(bb.Min.Z - level_z) < 3.0]
        for room, bb in candidates:
            # Probe at the usual height, or halfway up a very low room.
            z = min(probe_z, (bb.Min.Z + bb.Max.Z) / 2.0)
            z = max(z, bb.Min.Z + 0.01)
            try:
                if room.IsPointInRoom(XYZ(x, y, z)):
                    return room
            except Exception:
                continue
        return None

    def find(self, point):
        room = self._search(self.host, point.X, point.Y,
                            self.level_z, self.room_z)
        if room is not None:
            return room
        for inverse, rooms in self.links:
            p = inverse.OfPoint(XYZ(point.X, point.Y, self.room_z))
            base = inverse.OfPoint(XYZ(point.X, point.Y, self.level_z))
            room = self._search(rooms, p.X, p.Y, base.Z, p.Z)
            if room is not None:
                return room
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
            "Auto-Dimension: pick the line(s) you drew across the walls, "
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


def face_z_samples(face, count=FACE_Z_SAMPLES):
    """Heights spread evenly up a wall face. A window or door leaves a
    hole in the face, so probing only one height can miss the wall; one of
    these heights will land on the wall below the sill or above the head."""
    try:
        bb = face.GetBoundingBox()
        zs = [face.Evaluate(UV(u, v)).Z
              for u in (bb.Min.U, bb.Max.U) for v in (bb.Min.V, bb.Max.V)]
    except Exception:
        return []
    z0, z1 = min(zs), max(zs)
    return [z0 + (z1 - z0) * (i + 0.5) / count for i in range(count)]


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


def wall_side_hits(wall, side, a, b, direction, zs_base, sample_face=True):
    """[(t, cos_angle, Reference)] for this wall's side faces on one side
    that the line crosses at the heights in zs_base (plus heights up the
    whole face if sample_face)."""
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
                             zs_base + (face_z_samples(face)
                                        if sample_face else []))
        if hit is not None:
            # Revit often splits one side of a wall into several flat faces
            # stacked up its height (e.g. where a floor or ceiling joins
            # it). They sit in the same plane, so the line hits them at the
            # same point: keep one, or the wall looks like it's crossed
            # more than once.
            if any(abs(hit[0] - t) < DEDUP_TOL for t, _, _ in hits):
                continue
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


# External walls are dimensioned from their outer face only if the wall
# type contains one of these words in a layer's material name or in the
# wall type name (not case-sensitive). Other external walls are treated
# like internal walls (both faces of the Structure layer).
OUTER_FACE_WORDS = ["brick"]


def has_brick(wall):
    """True if any layer material, or the wall type name, contains one of
    OUTER_FACE_WORDS."""
    words = [w.lower() for w in OUTER_FACE_WORDS]
    names = []
    try:
        names.append(safe_name(wall.WallType))
    except Exception:
        pass
    try:
        for layer in wall.WallType.GetCompoundStructure().GetLayers():
            material = doc.GetElement(layer.MaterialId)
            if material is not None:
                names.append(safe_name(material))
    except Exception:
        pass
    return any(w in (n or "").lower() for n in names for w in words)


def is_external(wall):
    """True if the wall's type has Function = Exterior
    (Edit Type > Construction > Function)."""
    try:
        return wall.WallType.Function == WallFunction.Exterior
    except Exception:
        pass
    try:
        p = wall.WallType.get_Parameter(BuiltInParameter.FUNCTION_PARAM)
        return p is not None and p.AsInteger() == int(WallFunction.Exterior)
    except Exception:
        return False


def structure_offsets(layers):
    """Distances in from the exterior finish face to the faces of the
    wall's Structure [1] layers, or None if it has none. Adjacent
    Structure layers count as one block (two faces); separate blocks,
    e.g. a double stud wall, give two faces each."""
    widths, functions, _, _ = layers
    offsets = []
    pos = 0.0
    in_block = False
    for width, function in zip(widths, functions):
        is_structure = function == MaterialFunctionAssignment.Structure
        if is_structure and not in_block:
            offsets.append(pos)
        elif in_block and not is_structure:
            offsets.append(pos)
        in_block = is_structure
        pos += width
    if in_block:
        offsets.append(pos)
    return offsets or None


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
    # Check each candidate by measuring from the exterior finish face, and
    # failing that from the interior finish face.
    measured = []
    for anchor, expected in ((ref_ext, offset), (ref_int, total - offset)):
        for index in CORE_INDEX_CANDIDATES:
            ref = core_reference(wall, index)
            if ref is None:
                if anchor is ref_ext:
                    measured.append("%d=no ref" % index)
                continue
            v = measure(view, dim_line, anchor, ref)
            if v is not None and abs(v - expected) < CORE_CHECK_TOL:
                return ref
            if anchor is ref_ext:
                measured.append("%d=%s" % (
                    index, "-" if v is None else "%g" % round(v / MM, 1)))
    CORE_DIAG[str(wall.Id)] = ("wanted %g mm in from the outside; "
                               "references measured: %s"
                               % (round(offset / MM, 1), ", ".join(measured)))
    return None


def offset_hits(wall, view, dim_line, ext, inn, layers, offsets):
    """Dimension points for one wall at the given distances in from its
    exterior finish face. ext / inn are (t, cos_angle, ref) of the finish
    faces the line crosses. Returns [(t, ref), ...], or None if the wall
    has no reference at one of the distances (i.e. it isn't a finish or
    core face)."""
    total = sum(layers[0])
    t_ext, cos_ext, ref_ext = ext
    t_int, _, ref_int = inn
    step = 1.0 if t_int > t_ext else -1.0
    out = []
    for offset in offsets:
        ref = ref_at_offset(wall, view, dim_line, ref_ext, ref_int,
                            offset, total)
        if ref is None:
            return None
        out.append((t_ext + step * offset / cos_ext, ref))
    return out


def collect_face_hits(view, a, b, direction, dim_line, include_others,
                      stats, notes, pick_z=None):
    """Return a sorted, de-duplicated list of (distance_along_line,
    Reference) for every wall face to dimension along the line.

    Each wall is dimensioned to the faces of its Structure [1] layer(s).
    External walls (type Function = Exterior) get their outer face and the
    inner face of the Structure layer instead.
    Other walls without a Structure layer are skipped, unless
    include_others is True, in which case they use their core faces, or
    failing that finished faces.

    pick_z: absolute height to pick walls up at (IN[5]), or None for
    automatic (cut plane, then up the whole face)."""
    if pick_z is not None:
        zs_base, sample_face = [pick_z], False
    else:
        zs_base = [get_cut_plane_z(view),
                   get_level_z(view) + ROOM_PROBE_HEIGHT]
        sample_face = True
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
                             direction, zs_base, sample_face)
        inn = wall_side_hits(wall, ShellLayerType.Interior, a, b,
                             direction, zs_base, sample_face)
        if not ext and not inn:
            continue

        layers = wall_layers(wall)
        clean = layers is not None and len(ext) == 1 and len(inn) == 1

        # Brick external walls also get their outer (exterior finished)
        # face, i.e. the outside of the brick. It's a real face of the wall,
        # so the dimension stays attached to it. Other external walls are
        # treated like internal ones (both Structure faces).
        outer = None
        if len(ext) == 1 and is_external(wall) and has_brick(wall):
            outer = (ext[0][0], ext[0][2])
            hits.append(outer)
            stats["outer"] += 1

        offsets = structure_offsets(layers) if clean else None
        if offsets and outer is not None:
            # External wall: outer face (added above) + only the INNER
            # face of the Structure layer, e.g. outside of brick to inside
            # of frame = 110 + 40 + 90 = 240 as one segment.
            offsets = offsets[-1:]
        if offsets:
            res = offset_hits(wall, view, dim_line, ext[0], inn[0], layers,
                              offsets)
            if res:
                hits.extend(res)
                stats["structure"] += 1
                continue

        if offsets:
            why = ("Structure layer isn't between the Core Boundary rows "
                   "in its wall type")
            if str(wall.Id) in CORE_DIAG:
                why += "; " + CORE_DIAG[str(wall.Id)]
        elif layers is not None and not clean:
            why = ("line doesn't cross it cleanly: %d exterior / %d "
                   "interior face(s) hit" % (len(ext), len(inn)))
        elif layers is None:
            why = "no layer structure"
        else:
            why = "no Structure layer"
        if not include_others:
            if outer is not None:
                # e.g. a brick skin modelled as its own wall: its outer
                # face is all that's wanted from it.
                if why != "no Structure layer":
                    notes.append("Wall %s: outer face only (%s)"
                                 % (wall.Id, why))
            else:
                notes.append("Wall %s skipped (%s)" % (wall.Id, why))
                stats["skipped"] += 1
            continue

        if clean:
            core = core_offsets(layers)
            res = core and offset_hits(wall, view, dim_line, ext[0],
                                       inn[0], layers, core)
            if res:
                hits.extend(res)
                stats["core"] += 1
                continue
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

def group_hits(view, axis, members, include_others, stats, notes, pick_z):
    """Every wall face crossed by any line in the group, as a sorted,
    de-duplicated list of (s, Reference), s = distance along the axis."""
    hits = []
    for line in members:
        dim_line = Line.CreateBound(XYZ(line.a.X, line.a.Y, line.z),
                                    XYZ(line.b.X, line.b.Y, line.z))
        for t, ref in collect_face_hits(view, line.a, line.b, axis,
                                        dim_line, include_others,
                                        stats, notes, pick_z):
            hits.append((line.s_a + t, ref))
    hits.sort(key=lambda h: h[0])
    deduped = []
    for s, ref in hits:
        if deduped and abs(s - deduped[-1][0]) < DEDUP_TOL:
            continue    # same face crossed by two of the lines
        deduped.append((s, ref))
    return deduped


def room_probe(point, axis, members):
    """Where to look up the room for a dimension segment centred at
    `point`: on whichever drawn line covers that stretch of the string, so
    a stepped path labels the rooms it actually passes through."""
    p = flat(point)
    first = members[0]
    s = p.Subtract(first.a).DotProduct(axis) + first.s_a
    for line in members:
        if line.s_a - DEDUP_TOL <= s <= line.s_b + DEDUP_TOL:
            return line.a.Add(axis.Multiply(s - line.s_a))
    return p


def segment_midpoints(segments, hits, axis, members):
    """Plan point at the middle of each dimension segment, worked out from
    the wall faces we dimensioned (sorted along the axis), not from
    Revit's Segment.Origin. Falls back to Origin if Revit merged or
    dropped a reference and the counts no longer line up."""
    first = members[0]

    def at(s):
        return first.a.Add(axis.Multiply(s - first.s_a))

    if len(segments) == len(hits) - 1:
        mids = [at((hits[i][0] + hits[i + 1][0]) / 2.0)
                for i in range(len(segments))]
        # Revit lists segments along the dimension line; flip if it runs
        # the other way to ours.
        try:
            o0 = flat(segments[0].Origin).Subtract(first.a).DotProduct(axis)
            o1 = flat(segments[-1].Origin).Subtract(first.a).DotProduct(axis)
            if len(segments) > 1 and o0 > o1 + DEDUP_TOL:
                mids.reverse()
        except Exception:
            pass
        return mids
    return [seg.Origin for seg in segments]


def dimension_group(view, axis, members, dim_type, include_number, rooms,
                    include_others, pick_z):
    """One dimension string for a group of parallel, connected lines.
    Returns (Dimension or None, message)."""
    notes = []
    stats = {"structure": 0, "core": 0, "finish": 0, "skipped": 0,
             "outer": 0}
    hits = group_hits(view, axis, members, include_others, stats, notes,
                      pick_z)
    ids = ", ".join(str(line.element.Id) for line in members)
    label = "Line" if len(members) == 1 else "Lines"
    if len(hits) < 2:
        return None, ("%s %s: %d wall face(s) found, no dimension made "
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
    doc.Regenerate()

    # Room names under the value of each segment. With only two
    # references the dimension itself is the one segment.
    labelled = 0
    segments = [dim] if dim.NumberOfSegments == 0 else list(dim.Segments)
    for seg, mid in zip(segments, segment_midpoints(segments, hits, axis,
                                                    members)):
        name = room_label(rooms.find(room_probe(mid, axis, members)),
                          include_number)
        if name:
            seg.Below = name
            labelled += 1
    if labelled == 0:
        host_rooms, link_rooms = rooms.counts()
        notes.append("No room found under any segment (placed rooms: %d in "
                     "this model, %d in linked models). Check the rooms are "
                     "placed and enclosed on this level, and the line runs "
                     "through them" % (host_rooms, link_rooms))

    parts = ["%d on Structure layer" % stats["structure"],
             "%d brick external with outer face" % stats["outer"]]
    if include_others:
        parts.append("%d on core" % stats["core"])
        parts.append("%d on finish faces" % stats["finish"])
    else:
        parts.append("%d skipped" % stats["skipped"])
    msg = ("%s %s: %d faces dimensioned (walls: %s), %d room label(s)."
           % (label, ids, len(hits), ", ".join(parts), labelled))
    unique_notes = []
    for note in notes:
        if note not in unique_notes:
            unique_notes.append(note)
    if unique_notes:
        msg += " " + "; ".join(unique_notes) + "."
    return dim, msg


# ----------------------------------------------------------------------------
# Pop-up: wall pick-up height
# ----------------------------------------------------------------------------

SETTINGS_FILE = os.path.join(tempfile.gettempdir(),
                             "AutoDimension_settings.txt")
CANCELLED = object()


def load_last_height():
    """Last height typed in the pop-up ("" = automatic)."""
    try:
        with open(SETTINGS_FILE) as f:
            return f.read().strip()
    except Exception:
        return ""


def save_last_height(text):
    try:
        with open(SETTINGS_FILE, "w") as f:
            f.write(text)
    except Exception:
        pass


def ask_pick_height():
    """Pop-up asking for the wall pick-up height. Returns a height in mm,
    None for automatic, or CANCELLED. Built from a plain Form (no
    subclass) so it works on both the CPython3 and IronPython engines."""
    clr.AddReference('System.Windows.Forms')
    clr.AddReference('System.Drawing')
    from System.Windows.Forms import (
        Form, Label, TextBox, CheckBox, Button, DialogResult,
        FormStartPosition, FormBorderStyle, MessageBox)
    from System.Drawing import Point, Size

    last = load_last_height()

    form = Form()
    form.Text = "Auto-Dimension"
    form.ClientSize = Size(380, 170)
    form.StartPosition = FormStartPosition.CenterScreen
    form.FormBorderStyle = FormBorderStyle.FixedDialog
    form.MaximizeBox = False
    form.MinimizeBox = False
    form.TopMost = True     # don't open hidden behind Revit / Dynamo

    label = Label()
    label.Text = "Wall pick-up height above the view's level (mm):"
    label.Location = Point(15, 15)
    label.Size = Size(350, 20)
    form.Controls.Add(label)

    box = TextBox()
    box.Location = Point(15, 40)
    box.Size = Size(120, 22)
    box.Text = last
    form.Controls.Add(box)

    auto = CheckBox()
    auto.Text = ("Automatic (cut plane, then up the whole wall - finds "
                 "walls through windows and doors)")
    auto.Location = Point(15, 72)
    auto.Size = Size(350, 36)
    auto.Checked = (last == "")
    box.Enabled = not auto.Checked
    form.Controls.Add(auto)

    def on_auto_changed(sender, args):
        box.Enabled = not auto.Checked
        if box.Enabled:
            box.Focus()
    auto.CheckedChanged += on_auto_changed

    ok = Button()
    ok.Text = "OK - pick lines"
    ok.Location = Point(165, 125)
    ok.Size = Size(110, 30)
    form.Controls.Add(ok)

    cancel = Button()
    cancel.Text = "Cancel"
    cancel.Location = Point(285, 125)
    cancel.Size = Size(80, 30)
    cancel.DialogResult = DialogResult.Cancel
    form.Controls.Add(cancel)

    form.AcceptButton = ok
    form.CancelButton = cancel

    def on_ok(sender, args):
        if not auto.Checked:
            value = height_mm(box.Text)
            if value is None or value <= 0:
                MessageBox.Show("Enter a height in mm greater than 0, "
                                "e.g. 1200, or tick Automatic.",
                                "Auto-Dimension")
                box.Focus()
                return      # keep the pop-up open
        form.DialogResult = DialogResult.OK
        form.Close()
    ok.Click += on_ok

    if form.ShowDialog() != DialogResult.OK:
        return CANCELLED
    if auto.Checked:
        save_last_height("")
        return None
    text = box.Text.strip()
    save_last_height(text)
    return height_mm(text)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main():
    view = doc.ActiveView
    if not isinstance(view, ViewPlan):
        return [], ("Active view '%s' is not a plan view. Open the floor "
                    "plan you drew the line in and run again."
                    % safe_name(view))

    # A Boolean / number / text wired into IN[0] isn't a line, so it falls
    # back to picking on screen. A number there is also taken as the wall
    # pick-up height (same as IN[5]), so one Number node on IN[0] is all
    # the setup that option needs.
    raw_in0 = _as_list(_in(0))
    elements = [_unwrap(e) for e in raw_in0
                if not isinstance(e, (bool, int, float, str))]
    in0_height = next((height_mm(e) for e in raw_in0
                       if isinstance(e, (int, float))
                       and not isinstance(e, bool)), None)

    # Wall pick-up height: IN[5], else a number on IN[0], else ask in a
    # pop-up (remembers the last value).
    pick_height = height_mm(_in(5))
    if pick_height is None:
        pick_height = in0_height
    if pick_height is None:
        answer = ask_pick_height()
        if answer is CANCELLED:
            return [], "Cancelled. Nothing done."
        pick_height = answer

    if not elements:
        elements = pick_lines()
    if not elements:
        return [], "No line selected. Nothing done."

    dim_type_name = _in(1)
    dim_type = find_dimension_type(dim_type_name)
    report = ["Auto-Dimension script version %s" % SCRIPT_VERSION]
    if dim_type_name and dim_type is None:
        report.append("Dimension type '%s' not found; used the default."
                      % dim_type_name)

    include_number = bool(_in(2, False))
    delete_line = bool(_in(3, False))
    include_others = bool(_in(4, False))
    pick_z = (get_level_z(view) + pick_height * MM
              if pick_height is not None else None)
    if pick_height is not None:
        report.append("Picking up walls at %g mm above the view's level."
                      % pick_height)
    rooms = RoomFinder(view, get_level_z(view) + ROOM_PROBE_HEIGHT,
                       get_view_phase(view))
    if rooms.hidden_option_rooms:
        report.append("Ignored %d room(s) in design options not shown in "
                      "this view." % rooms.hidden_option_rooms)

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
                    dim, msg = dimension_group(
                        view, axis, members, dim_type, include_number,
                        rooms, include_others, pick_z)
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
        if pick_height is not None:
            report.append("No dimensions made: no walls found at %g mm "
                          "above the view's level. Lower the pick-up "
                          "height, or set it blank for automatic."
                          % pick_height)
        else:
            report.append("No dimensions made. Check the lines run "
                          "perpendicular to the walls and fully through "
                          "them.")
    return dims, "\n".join(report)


# ----------------------------------------------------------------------------
# Re-run on every click of Run
# ----------------------------------------------------------------------------

# Dynamo only re-runs a node whose inputs changed, so with nothing wired a
# second click of Run would do nothing. This marker lets the script find its
# own node and flag it as changed, so the next Run executes it again.
SELF_MARKER = "AUTO_DIMENSION_RERUN_MARKER"


def _input0_boolean(node, workspace=None):
    """The Boolean node wired into this node's IN[0], or None. Looks via the
    node's port and via the workspace's connectors, and accepts any node
    whose type name contains "Bool" or whose Value is a bool, so it copes
    with differences between Dynamo versions."""
    sources = []
    try:
        for connector in node.InPorts[0].Connectors:
            sources.append(connector.Start.Owner)
    except Exception:
        pass
    if workspace is not None:
        try:
            for connector in workspace.Connectors:
                end = connector.End
                if end.Owner.GUID == node.GUID and end.Index == 0:
                    sources.append(connector.Start.Owner)
        except Exception:
            pass
    for source in sources:
        try:
            if "Bool" in source.GetType().Name:
                return source
        except Exception:
            pass
        try:
            if isinstance(_get_value(source), bool):
                return source
        except Exception:
            pass
    return None


def _get_value(node):
    """A Boolean node's Value, via .NET reflection if Python can't see it."""
    try:
        return node.Value
    except Exception:
        prop = node.GetType().GetProperty("Value")
        return prop.GetValue(node, None)


def _set_value(node, value):
    try:
        node.Value = value
    except Exception:
        prop = node.GetType().GetProperty("Value")
        prop.SetValue(node, value, None)


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

    toggles = [b for b in (_input0_boolean(n, workspace) for n in nodes)
               if b is not None]

    def mark():
        for node in nodes:
            node.MarkNodeAsModified(True)

    def flip():
        for boolean in toggles:
            _set_value(boolean, not _get_value(boolean))

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
