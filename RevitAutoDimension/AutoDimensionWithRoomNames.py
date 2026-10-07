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
#      PORCH / ALFRESCO SLABS: these usually have no walls round them, so
#      the edges of any slab (Floor / Structural Foundation) under a room
#      named Porch or Alfresco are treated like external walls: picked up
#      where the line crosses them, as the overall end of the string, and
#      as visible corners (see PORCH_ROOM_WORDS).
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
    ElementMulticategoryFilter, Solid, Options, ViewDetailLevel
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
SCRIPT_VERSION = "2026-10-07 outside-1"

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

# Also snap to external walls the line doesn't cross:
#   - ADD_OVERALL_EXTERNAL: the outermost external wall at each end;
#   - ADD_FACADE_STEPS: every external corner you can see standing on the
#     line: outer wall faces that face back towards the line (nearest one
#     at each point), and each return wall where that outline steps in or
#     out. Corners on walls facing away from the line (e.g. the far jog
#     of an S-shaped wall the line cuts through) aren't included.
# Brick walls give the outer face of the brick; other external walls the
# outer face of their Structure layer.
ADD_OVERALL_EXTERNAL = True
ADD_FACADE_STEPS = True
STEP_MATCH_TOL = 0.05        # ~15 mm: a return wall face this close to a step


def _face_extent(face, axis, perp, first):
    """(s min, s max, perp min, perp max) of a planar face, from its UV
    bounds. s = distance along the axis, measured like the drawn lines."""
    bb = face.GetBoundingBox()
    ss, ds = [], []
    for u in (bb.Min.U, bb.Max.U):
        for v in (bb.Min.V, bb.Max.V):
            p = flat(face.Evaluate(UV(u, v)))
            ss.append(p.Subtract(first.a).DotProduct(axis) + first.s_a)
            ds.append(p.DotProduct(perp))
    return min(ss), max(ss), min(ds), max(ds)


def _stable(ref):
    try:
        return ref.ConvertToStableRepresentation(doc)
    except Exception:
        return None


def _wall_solid_faces(wall):
    """Vertical planar faces of the wall's own solid, with references."""
    opt = Options()
    opt.ComputeReferences = True
    try:
        geometry = wall.get_Geometry(opt)
    except Exception:
        return []
    faces = []
    if geometry is None:
        return faces
    for obj in geometry:
        if not isinstance(obj, Solid):
            continue
        try:
            if obj.Faces.Size == 0:
                continue
        except Exception:
            continue
        for face in obj.Faces:
            try:
                if (isinstance(face, PlanarFace) and face.Reference is not None
                        and abs(face.FaceNormal.Z) < 0.01):
                    faces.append(face)
            except Exception:
                continue
    return faces


def _external_faces(view, axis, perp, first):
    """Faces of the external walls in the view, split into facade faces
    (square to perp: they run along the string) [(side, s0, s1, depth)]
    and return faces (square to the axis: the string can measure to them)
    [(s, sign, d0, d1, wall, ref, direct)].

    The exterior side faces are used as before (direct False: brick walls
    give the brick face, others the outer face of the Structure layer).
    The wall's other vertical faces (its ends, e.g. a brick pier or a wall
    stopping at a corner, and its inner face) are added too (direct True:
    that face itself), so what you see from outside isn't missing the
    ends of walls."""
    facades, returns = [], []
    for wall in collect_walls(view):
        try:
            if wall.WallType.Kind == WallKind.Curtain or not is_external(wall):
                continue
            refs = list(HostObjectUtils.GetSideFaces(
                wall, ShellLayerType.Exterior))
        except Exception:
            continue
        side_keys = set()
        for ref in refs:
            key = _stable(ref)
            if key:
                side_keys.add(key)
            try:
                face = wall.GetGeometryObjectFromReference(ref)
                if not isinstance(face, PlanarFace):
                    continue
                n = flat(face.FaceNormal)
                if n.GetLength() < 1e-9:
                    continue
                n = n.Normalize()
                s0, s1, d0, d1 = _face_extent(face, axis, perp, first)
                if abs(n.DotProduct(perp)) >= PARALLEL_COS:
                    side = 1.0 if n.DotProduct(perp) > 0 else -1.0
                    facades.append((side, s0, s1, (d0 + d1) / 2.0))
                elif abs(n.DotProduct(axis)) >= PARALLEL_COS:
                    s = (flat(face.Origin).Subtract(first.a).DotProduct(axis)
                         + first.s_a)
                    sign = 1.0 if n.DotProduct(axis) > 0 else -1.0
                    returns.append((s, sign, d0, d1, wall, ref, False))
            except Exception:
                continue
        for face in _wall_solid_faces(wall):
            try:
                if _stable(face.Reference) in side_keys:
                    continue        # an exterior side face, done above
                n = flat(face.FaceNormal).Normalize()
                s0, s1, d0, d1 = _face_extent(face, axis, perp, first)
                if abs(n.DotProduct(perp)) >= PARALLEL_COS:
                    side = 1.0 if n.DotProduct(perp) > 0 else -1.0
                    facades.append((side, s0, s1, (d0 + d1) / 2.0))
                elif abs(n.DotProduct(axis)) >= PARALLEL_COS:
                    s = (flat(face.Origin).Subtract(first.a).DotProduct(axis)
                         + first.s_a)
                    sign = 1.0 if n.DotProduct(axis) > 0 else -1.0
                    returns.append((s, sign, d0, d1, wall, face.Reference,
                                    True))
            except Exception:
                continue
    return facades, returns


def _outer_point(wall, ext_ref, s, sign, dim_line, view, notes):
    """(s, Reference) for an external wall's dimension point: the outer
    face of the brick, or (not brick) the outer face of its Structure
    layer. None if that face can't be referenced."""
    if has_brick(wall):
        return s, ext_ref
    layers = wall_layers(wall)
    offsets = structure_offsets(layers) if layers else None
    if not offsets:
        return s, ext_ref           # no Structure layer: its outer face
    offset = offsets[0]
    try:
        int_ref = list(HostObjectUtils.GetSideFaces(
            wall, ShellLayerType.Interior))[0]
    except Exception:
        int_ref = None
    ref = ref_at_offset(wall, view, dim_line, ext_ref, int_ref, offset,
                        sum(layers[0])) if int_ref is not None else None
    if ref is None:
        notes.append("Wall %s: outer face of its Structure layer not found"
                     % wall.Id)
        return None
    return s - sign * offset, ref


def _profile_steps(faces, nearest, keep=None, extra_cuts=()):
    """Steps in one side's visible outline: [(s, depth a, depth b)].
    faces: [(side, s0, s1, depth)]; nearest picks the face seen at each
    point along the string (the one closest to the line). keep(face, s),
    if given, says whether a face can be seen from the line at s."""
    cuts = sorted(set([f[1] for f in faces] + [f[2] for f in faces]
                      + list(extra_cuts)))
    profile = []        # (s start, s end, depth or None)
    for s0, s1 in zip(cuts, cuts[1:]):
        if s1 - s0 < DEDUP_TOL:
            continue
        mid = (s0 + s1) / 2.0
        covering = [f[3] for f in faces
                    if f[1] - DEDUP_TOL <= mid <= f[2] + DEDUP_TOL
                    and (keep is None or keep(f, mid))]
        profile.append((s0, s1, nearest(covering) if covering else None))
    steps = []
    previous = None
    for s0, s1, depth in profile:
        if previous is not None:
            if (previous[2] is None) != (depth is None) or (
                    depth is not None and previous[2] is not None
                    and abs(depth - previous[2]) > DEDUP_TOL):
                a = previous[2] if previous[2] is not None else depth
                b = depth if depth is not None else previous[2]
                steps.append((s0, a, b))
        previous = (s0, s1, depth)
    if profile and profile[0][2] is not None:
        steps.insert(0, (profile[0][0], profile[0][2], profile[0][2]))
    if profile and profile[-1][2] is not None:
        steps.append((profile[-1][1], profile[-1][2], profile[-1][2]))
    return steps


def _facade_steps(facades, line_d, look=0, inside=None, cuts=()):
    """Corners of the external walls you can see standing on the line:
    [(s, depth a, depth b)].

    look = +1 / -1: you're looking one way from the line (the pop-up's
    Look up / down / left / right). Only the outer walls AHEAD of the line
    that way count, nearest first - so for a line through an S-bend you
    get the corner ahead of you, not the one behind. You only see outer
    wall faces facing back towards you, so the far side of the house
    never shows through a gap in the near side. Where the line runs
    through the house (inside(s) True) nothing is added: inside, the
    string only picks up the walls the line crosses. cuts: where the line
    crosses walls, so in / out changes line up with them.

    look = 0 (Both sides):
    A facade face (an exterior face running along the string) is only
    visible if it faces back towards the line - e.g. looking north from
    the line you see the south-facing outer walls north of it, not the
    ones behind you or facing away. On each side of the line, the face
    nearest the line is the one seen at each point along the string, and
    every place that outline steps in or out is a corner to dimension."""
    if look:
        ahead = [f for f in facades if (f[3] - line_d) * look > DEDUP_TOL]
        if not ahead:
            return []

        def keep(face, s):
            # face[0] * look < 0: the face looks back towards the line.
            return face[0] * look < 0 and not (inside is not None
                                               and inside(s))
        return _profile_steps(ahead, min if look > 0 else max, keep, cuts)
    ahead = [f for f in facades
             if f[3] > line_d + DEDUP_TOL and f[0] < 0]     # faces back
    behind = [f for f in facades
              if f[3] < line_d - DEDUP_TOL and f[0] > 0]    # faces back
    steps = []
    if ahead:
        steps.extend(_profile_steps(ahead, min))
    if behind:
        steps.extend(_profile_steps(behind, max))
    return steps


def look_sign(view, axis):
    """+1 / -1 / 0: which side of the string (along perp = axis turned
    90 degrees) you're looking towards, from the pop-up's choices.
    Strings running across the view use the horizontal-line choice
    (up / down / both); strings running up the view use the vertical-line
    choice (left / right / both)."""
    look_h, look_v = LOOK
    perp = XYZ(-axis.Y, axis.X, 0.0)
    try:
        right, up = view.RightDirection, view.UpDirection
    except Exception:
        right, up = XYZ(1, 0, 0), XYZ(0, 1, 0)
    if abs(axis.DotProduct(right)) >= abs(axis.DotProduct(up)):
        # Line runs across the view: look up or down.
        if look_h == "up":
            towards = up
        elif look_h == "down":
            towards = up.Negate()
        else:
            return 0
    else:
        # Line runs up the view: look left or right.
        if look_v == "left":
            towards = right.Negate()
        elif look_v == "right":
            towards = right
        else:
            return 0
    return 1 if perp.DotProduct(towards) >= 0 else -1


# ----------------------------------------------------------------------------
# Porch / alfresco slabs
# ----------------------------------------------------------------------------

# A porch or alfresco usually has no walls round it, so the string also
# snaps to the edges of any slab (Floor / Structural Foundation) under a
# room whose name contains one of these words (not case-sensitive). Those
# edges are treated like external walls: picked up where the line crosses
# them, as the overall end of the string, and as visible corners.
ADD_PORCH_SLABS = True
PORCH_ROOM_WORDS = ["porch", "alfresco"]
PORCH_PROBE_IN = 300 * MM    # look for the room this far inside the edge
PORCH_MERGE_TOL = 20 * MM    # a slab edge this close to a wall point is
                             # left out (no tiny 10 mm segments)
OUTLINE_PROBE = 0.15         # ~45 mm: probe just outside a slab edge
VERTICAL_TOL = 0.01          # |normal.Z| below this = a vertical face


def collect_slabs(view):
    """Floors and Structural Foundation slabs visible in the view."""
    cats = NetList[BuiltInCategory]()
    cats.Add(BuiltInCategory.OST_Floors)
    cats.Add(BuiltInCategory.OST_StructuralFoundation)
    try:
        return list(FilteredElementCollector(doc, view.Id)
                    .WherePasses(ElementMulticategoryFilter(cats))
                    .WhereElementIsNotElementType())
    except Exception:
        return []


def slab_faces(element):
    """Planar faces of the slab's own solids, with references so they can
    be dimensioned (footing families etc. are skipped)."""
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


def _top_faces(slabs):
    """Top faces of the slabs, to tell outline edges from step edges."""
    faces = []
    for slab in slabs:
        try:
            for ref in HostObjectUtils.GetTopFaces(slab):
                face = slab.GetGeometryObjectFromReference(ref)
                if face is not None:
                    faces.append(face)
        except Exception:
            continue
    return faces


def _on_a_slab(point, top_faces):
    """True if a slab's top face is directly above / below this point."""
    for face in top_faces:
        try:
            res = face.Project(point)
            if res is None:
                continue
            q = res.XYZPoint
            if math.hypot(q.X - point.X, q.Y - point.Y) < 0.01:
                return True
        except Exception:
            continue
    return False


def is_porch_room(room):
    name = room_label(room, False).lower()
    return any(word in name for word in PORCH_ROOM_WORDS)


def _edge_samples(face):
    """Points spread along a vertical slab edge face (plan positions)."""
    bb = face.GetBoundingBox()
    fractions = [0.1, 0.3, 0.5, 0.7, 0.9]
    um = (bb.Min.U + bb.Max.U) / 2.0
    vm = (bb.Min.V + bb.Max.V) / 2.0
    points = []
    for f in fractions:
        points.append(face.Evaluate(UV(bb.Min.U + (bb.Max.U - bb.Min.U) * f,
                                       vm)))
        points.append(face.Evaluate(UV(um, bb.Min.V
                                       + (bb.Max.V - bb.Min.V) * f)))
    return points


def _is_porch_edge(face, n, tops, rooms):
    """True if this slab edge is on the slab outline (no slab just beyond
    it) and has a Porch / Alfresco room just inside it."""
    samples = _edge_samples(face)
    centre = samples[4]
    if _on_a_slab(centre.Add(n.Multiply(OUTLINE_PROBE)), tops):
        return False        # a step between slabs, not the outline
    for p in samples:
        room = rooms.find(flat(p).Subtract(n.Multiply(PORCH_PROBE_IN)))
        if room is not None and is_porch_room(room):
            return True
    return False


def porch_faces(view, axis, perp, first, rooms):
    """Outline edges of Porch / Alfresco slabs, split like
    _external_faces: facades [(side, s0, s1, depth)] running along the
    string, and returns [(s, sign, d0, d1, None, ref)] square to it."""
    facades, returns = [], []
    if not ADD_PORCH_SLABS or rooms is None:
        return facades, returns
    slabs = collect_slabs(view)
    if not slabs:
        return facades, returns
    tops = _top_faces(slabs)
    for slab in slabs:
        for face in slab_faces(slab):
            try:
                n = face.FaceNormal
                if abs(n.Z) > VERTICAL_TOL:
                    continue        # top, bottom or sloped face
                n = flat(n)
                if n.GetLength() < 1e-9:
                    continue
                n = n.Normalize()
                along = abs(n.DotProduct(perp)) >= PARALLEL_COS
                square = abs(n.DotProduct(axis)) >= PARALLEL_COS
                if not (along or square):
                    continue
                if not _is_porch_edge(face, n, tops, rooms):
                    continue
                s0, s1, d0, d1 = _face_extent(face, axis, perp, first)
                if along:
                    side = 1.0 if n.DotProduct(perp) > 0 else -1.0
                    facades.append((side, s0, s1, (d0 + d1) / 2.0))
                else:
                    s = (flat(face.Origin).Subtract(first.a).DotProduct(axis)
                         + first.s_a)
                    sign = 1.0 if n.DotProduct(axis) > 0 else -1.0
                    returns.append((s, sign, d0, d1, None, face.Reference))
            except Exception:
                continue
    return facades, returns


def porch_crossings(members, perp, porch_returns):
    """[(s, Reference)] of Porch / Alfresco slab edges the lines cross."""
    out = []
    for line in members:
        line_d = line.a.DotProduct(perp)
        for r in porch_returns:
            if (line.s_a - DEDUP_TOL <= r[0] <= line.s_b + DEDUP_TOL
                    and r[2] - DEDUP_TOL <= line_d <= r[3] + DEDUP_TOL):
                out.append((r[0], r[5]))
    return out


def line_inside_house(axis, members, rooms):
    """inside(s): True if the line at s (distance along the string) is in
    a room of the house. Porch / Alfresco rooms and no room count as
    outside. Cached, as the same points get asked about more than once."""
    first = members[0]
    cache = {}

    def inside(s):
        key = round(s, 2)
        if key not in cache:
            point = None
            for line in members:
                if line.s_a - DEDUP_TOL <= s <= line.s_b + DEDUP_TOL:
                    point = line.a.Add(axis.Multiply(s - line.s_a))
                    break
            if point is None:
                point = first.a.Add(axis.Multiply(s - first.s_a))
            room = rooms.find(point) if rooms is not None else None
            cache[key] = room is not None and not is_porch_room(room)
        return cache[key]
    return inside


def _match_steps(steps, returns):
    """For each outline step [(s, depth a, depth b)], the return face at
    that point along the string whose extent reaches across the step."""
    out = []
    for s, depth_a, depth_b in steps:
        lo, hi = min(depth_a, depth_b), max(depth_a, depth_b)
        best, best_overlap = None, -1.0
        for r in returns:
            if abs(r[0] - s) > STEP_MATCH_TOL:
                continue
            overlap = min(hi, r[3]) - max(lo, r[2])
            if overlap < -STEP_MATCH_TOL:
                continue        # a return face elsewhere along the line
            if overlap > best_overlap:
                best, best_overlap = r, overlap
        if best is not None:
            out.append(best)
    return out


def external_points(view, axis, members, notes, porch=None, rooms=None,
                    cuts=()):
    """[(s, Reference, is_slab, wall id)] for external walls the line
    doesn't have to cross: the outermost external wall at each end, and
    the corners of the outside of the house seen from the parts of the
    line outside it. porch = porch_faces(...): Porch / Alfresco slab
    edges, used like external walls but with their own outline, so a slab
    at floor level never hides a wall corner (or the other way round)."""
    first = members[0]
    perp = XYZ(-axis.Y, axis.X, 0.0)
    dim_line = Line.CreateBound(XYZ(first.a.X, first.a.Y, first.z),
                                XYZ(first.b.X, first.b.Y, first.z))
    wall_facades, wall_returns = _external_faces(view, axis, perp, first)
    slab_facades, slab_returns = porch if porch else ([], [])
    slab_returns = [r[:6] + (True,) for r in slab_returns]
    picked = []     # return tuples (s, sign, d0, d1, wall, ref, direct)

    if ADD_OVERALL_EXTERNAL:
        # The outermost external wall at each end, and the outermost
        # Porch / Alfresco slab edge if that sticks out further.
        for candidates in (wall_returns, wall_returns + slab_returns):
            lows = [r for r in candidates if r[1] < 0]
            highs = [r for r in candidates if r[1] > 0]
            if lows:
                picked.append(min(lows, key=lambda r: r[0]))
            if highs:
                picked.append(max(highs, key=lambda r: r[0]))

    if ADD_FACADE_STEPS:
        line_d = first.a.DotProduct(perp)
        look = look_sign(view, axis)
        inside = line_inside_house(axis, members, rooms)
        for facades, returns in ((wall_facades, wall_returns),
                                 (slab_facades, slab_returns)):
            if facades:
                picked.extend(_match_steps(
                    _facade_steps(facades, line_d, look, inside, cuts),
                    returns))

    out, seen = [], set()
    for s, sign, d0, d1, wall, ref, direct in picked:
        key = (str(wall.Id) if wall is not None else "slab", round(s, 3))
        if key in seen:
            continue
        seen.add(key)
        if wall is None:
            out.append((s, ref, True, None))    # Porch / Alfresco slab
            continue
        if direct:
            out.append((s, ref, False, str(wall.Id)))   # wall end etc.
            continue
        point = _outer_point(wall, ref, s, sign, dim_line, view, notes)
        if point is not None:
            out.append((point[0], point[1], False, str(wall.Id)))
    return out


def group_hits(view, axis, members, include_others, stats, notes, pick_z,
               rooms=None):
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
    if hits:
        # Only for a run that crosses walls, so a jog between runs doesn't
        # get a string of its own. Points the line already has are skipped.
        first = members[0]
        perp = XYZ(-axis.Y, axis.X, 0.0)
        porch = porch_faces(view, axis, perp, first, rooms)
        extra = [(s, ref, True, None)
                 for s, ref in porch_crossings(members, perp, porch[1])]
        if ADD_OVERALL_EXTERNAL or ADD_FACADE_STEPS:
            extra += external_points(view, axis, members, notes, porch,
                                     rooms, [h[0] for h in hits])
        added = []
        for s, ref, is_slab, wall_id in extra:
            tol = PORCH_MERGE_TOL if is_slab else DEDUP_TOL
            if all(abs(s - h[0]) >= tol for h in hits):
                hits.append((s, ref))
                stats["porch" if is_slab else "overall"] += 1
                if wall_id and wall_id not in added:
                    added.append(wall_id)
        if added:
            notes.append("external walls added beyond the line: %s"
                         % ", ".join(added))
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
             "outer": 0, "overall": 0, "porch": 0}
    hits = group_hits(view, axis, members, include_others, stats, notes,
                      pick_z, rooms)
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
             "%d brick external with outer face" % stats["outer"],
             "%d external wall point(s) added beyond the line"
             % stats["overall"],
             "%d Porch/Alfresco slab edge(s)" % stats["porch"]]
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


LOOK_FILE = os.path.join(tempfile.gettempdir(), "AutoDimension_look.txt")
LOOK_CHOICES_H = [
    ("up", "Look up the view (at the house above the line)"),
    ("down", "Look down the view (at the house below the line)"),
]
LOOK_CHOICES_V = [
    ("left", "Look left (at the house left of the line)"),
    ("right", "Look right (at the house right of the line)"),
]
LOOK = ("up", "left")       # (horizontal lines, vertical lines)


def load_look():
    """(horizontal choice, vertical choice) from the last pop-up."""
    try:
        with open(LOOK_FILE) as f:
            parts = f.read().strip().split(",")
    except Exception:
        return LOOK
    if len(parts) == 1:
        # Older single setting: up meant left on vertical lines.
        old = {"down": ("down", "right")}
        return old.get(parts[0], LOOK)
    h = parts[0] if parts[0] in ("up", "down") else LOOK[0]
    v = parts[1] if parts[1] in ("left", "right") else LOOK[1]
    return (h, v)


def save_look(value):
    try:
        with open(LOOK_FILE, "w") as f:
            f.write("%s,%s" % value)
    except Exception:
        pass


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
    """Pop-up asking for the wall pick-up height and the look direction.
    Returns (height in mm or None for automatic, look) or CANCELLED. Built from a plain Form (no
    subclass) so it works on both the CPython3 and IronPython engines."""
    clr.AddReference('System.Windows.Forms')
    clr.AddReference('System.Drawing')
    from System.Windows.Forms import (
        Form, Label, TextBox, CheckBox, Button, DialogResult,
        FormStartPosition, FormBorderStyle, MessageBox, RadioButton,
        GroupBox)
    from System.Drawing import Point, Size

    last = load_last_height()
    last_look = load_look()

    form = Form()
    form.Text = "Auto-Dimension"
    form.ClientSize = Size(400, 370)
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

    look_label = Label()
    look_label.Text = ("External wall corners the line doesn't cross - "
                       "which way are you looking from the line?")
    look_label.Location = Point(15, 115)
    look_label.Size = Size(375, 32)
    form.Controls.Add(look_label)

    def radio_group(title, choices, current, top):
        # Each group in its own box, so the two sets of options are
        # chosen separately.
        group = GroupBox()
        group.Text = title
        group.Location = Point(15, top)
        group.Size = Size(375, 80)
        form.Controls.Add(group)
        radios = []
        for i, (key, text) in enumerate(choices):
            radio = RadioButton()
            radio.Text = text
            radio.Location = Point(10, 20 + i * 26)
            radio.Size = Size(355, 24)
            radio.Checked = (key == current)
            group.Controls.Add(radio)
            radios.append((key, radio))
        return radios

    radios_h = radio_group("Horizontal lines (running across the view)",
                           LOOK_CHOICES_H, last_look[0], 150)
    radios_v = radio_group("Vertical lines (running up the view)",
                           LOOK_CHOICES_V, last_look[1], 237)

    ok = Button()
    ok.Text = "OK - pick lines"
    ok.Location = Point(185, 327)
    ok.Size = Size(110, 30)
    form.Controls.Add(ok)

    cancel = Button()
    cancel.Text = "Cancel"
    cancel.Location = Point(305, 327)
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
    look = (next((key for key, radio in radios_h if radio.Checked), LOOK[0]),
            next((key for key, radio in radios_v if radio.Checked), LOOK[1]))
    save_look(look)
    if auto.Checked:
        save_last_height("")
        return None, look
    text = box.Text.strip()
    save_last_height(text)
    return height_mm(text), look


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
    global LOOK
    LOOK = load_look()      # last choice, if the pop-up isn't shown
    pick_height = height_mm(_in(5))
    if pick_height is None:
        pick_height = in0_height
    if pick_height is None:
        answer = ask_pick_height()
        if answer is CANCELLED:
            return [], "Cancelled. Nothing done."
        pick_height, LOOK = answer

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
