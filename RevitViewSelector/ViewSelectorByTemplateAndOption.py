# ============================================================================
# Revit View Selector - by View Template + Design Option
# ----------------------------------------------------------------------------
# Paste this entire file into a single Dynamo "Python Script" node.
#
# Engine: CPython3 (Dynamo 2.6+ / Revit 2022+ default). Also runs unmodified
# on the legacy IronPython2 engine if your environment still uses it.
#
# What it does:
#   1. Shows a small dialog (WinForms) listing every View Template and every
#      Design Option in the current model, plus a match-mode choice.
#   2. Finds every "normal" project view whose View Template / Design Option
#      (compared by Element Id, not by name) matches what was picked.
#   3. Selects those views in the active Revit UI (SetElementIds) and reports
#      how many were selected.
#
# This node ONLY reads the model and changes the active selection. It never
# starts a Transaction and never modifies any element, parameter, template,
# or design option.
#
# IN[0] (optional): Boolean "Run" toggle. Wire a Boolean node to it and set it
#                    True, then run the graph. If nothing is wired, the tool
#                    runs anyway (defaults to True) so a bare Python node also
#                    works stand-alone.
# ============================================================================

import clr
import traceback

clr.AddReference('RevitAPI')
clr.AddReference('RevitAPIUI')
clr.AddReference('RevitServices')
clr.AddReference('System.Windows.Forms')
clr.AddReference('System.Drawing')

from Autodesk.Revit.DB import (
    FilteredElementCollector, View, ViewType, ElementId, DesignOption,
    BuiltInParameter
)
from RevitServices.Persistence import DocumentManager

from System.Collections.Generic import List as NetList
from System.Windows.Forms import (
    Form, Label, ComboBox, ComboBoxStyle, Button, RadioButton,
    DialogResult, FormStartPosition, FormBorderStyle, GroupBox
)
from System.Drawing import Point, Size, Font, FontStyle

# ----------------------------------------------------------------------------
# Environment
# ----------------------------------------------------------------------------

doc = DocumentManager.Instance.CurrentDBDocument
uiapp = DocumentManager.Instance.CurrentUIApplication
uidoc = uiapp.ActiveUIDocument if uiapp is not None else None

NONE_ANY_LABEL = "<None / Any>"

# View types that are excluded from selection because they are not "normal"
# selectable project views (templates, schedules, sheets, and internal /
# system browser views behave differently in the API and are unreliable or
# meaningless targets for this kind of selection tool).
EXCLUDED_VIEW_TYPES = set()

_EXCLUDED_VIEWTYPE_NAMES = [
    "Schedule", "PanelSchedule", "ColumnSchedule",
    "DrawingSheet", "Internal", "ProjectBrowser", "SystemBrowser", "Undefined",
]
for _name in _EXCLUDED_VIEWTYPE_NAMES:
    _vt = getattr(ViewType, _name, None)
    if _vt is not None:
        EXCLUDED_VIEW_TYPES.add(_vt)


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------

def eid_to_int(element_id):
    """Convert an ElementId to a plain int across old/new Revit API versions."""
    if element_id is None:
        return None
    try:
        return int(element_id.Value)          # Revit 2024+
    except AttributeError:
        return int(element_id.IntegerValue)    # Revit < 2024


def is_valid_eid(element_id):
    if element_id is None:
        return False
    try:
        return element_id != ElementId.InvalidElementId
    except Exception:
        return False


def safe_name(element, default="<unnamed>"):
    try:
        n = element.Name
        return n if n else default
    except Exception:
        return default


def get_design_option_set_name(design_option):
    """Best-effort lookup of the parent Design Option Set name for a
    Design Option element. Falls back gracefully if the API shape differs
    across Revit versions."""
    try:
        bip = getattr(BuiltInParameter, "OPTION_SET_ID", None)
        if bip is None:
            return "Unknown Option Set"
        param = design_option.get_Parameter(bip)
        if param is None:
            return "Unknown Option Set"
        set_id = param.AsElementId()
        if not is_valid_eid(set_id):
            return "Unknown Option Set"
        set_elem = doc.GetElement(set_id)
        return safe_name(set_elem, "Unknown Option Set") if set_elem else "Unknown Option Set"
    except Exception:
        return "Unknown Option Set"


def get_view_template_id(view):
    """Returns the ElementId of the view's assigned View Template, or None."""
    try:
        tid = view.ViewTemplateId
        if not is_valid_eid(tid):
            return None
        return tid
    except Exception:
        return None


def get_view_design_option_id(view):
    """Returns the ElementId of the Design Option a view belongs to, or None.

    Not every view has a meaningful Design Option: Element.DesignOption is a
    property inherited from the base Element class, but for the vast
    majority of views (created outside any active design option) it simply
    returns null. We must not assume it is populated, and must not error on
    views where the property or its Id is unavailable.
    """
    try:
        do = view.DesignOption
        if do is None:
            return None
        return do.Id
    except Exception:
        return None


# ----------------------------------------------------------------------------
# Data collection
# ----------------------------------------------------------------------------

debug_info = []

def collect_view_templates():
    templates = []
    try:
        all_views = FilteredElementCollector(doc).OfClass(View).ToElements()
        for v in all_views:
            try:
                if v is not None and v.IsValidObject and v.IsTemplate:
                    templates.append(v)
            except Exception as ex:
                debug_info.append("Skipped invalid view while scanning templates: {0}".format(ex))
    except Exception as ex:
        debug_info.append("Failed to collect view templates: {0}".format(ex))
    return templates


def collect_design_options():
    options = []
    try:
        options = list(FilteredElementCollector(doc).OfClass(DesignOption).ToElements())
    except Exception as ex:
        debug_info.append("Failed to collect design options: {0}".format(ex))
    return options


def collect_candidate_views():
    """All 'normal' selectable project views, excluding templates, schedules,
    sheets, and internal/system views."""
    candidates = []
    excluded_count = 0
    try:
        all_views = FilteredElementCollector(doc).OfClass(View).ToElements()
    except Exception as ex:
        debug_info.append("Failed to collect views: {0}".format(ex))
        return candidates

    for v in all_views:
        try:
            if v is None or not v.IsValidObject:
                excluded_count += 1
                continue
            if v.IsTemplate:
                excluded_count += 1
                continue
            view_type = v.ViewType
            if view_type in EXCLUDED_VIEW_TYPES:
                excluded_count += 1
                continue
            candidates.append(v)
        except Exception as ex:
            excluded_count += 1
            debug_info.append("Skipped invalid/problematic view element: {0}".format(ex))

    debug_info.append("Total View elements scanned: {0}".format(len(all_views)))
    debug_info.append("Excluded (templates/schedules/sheets/system/invalid): {0}".format(excluded_count))
    debug_info.append("Candidate selectable views: {0}".format(len(candidates)))
    return candidates


# ----------------------------------------------------------------------------
# Modular filter framework
# ----------------------------------------------------------------------------
# Each filter takes (record, criteria) and returns True/False.
# `record` is a plain dict built in build_view_record() below.
# To add a new filter in future (View Type, Discipline, Phase, Sheet, etc.):
#   1. Add the relevant field to build_view_record().
#   2. Write a filter_by_xxx(record, criteria) function.
#   3. Register it in build_active_filters() under a new criteria key.
# No other part of the tool needs to change.

def build_view_record(view):
    return {
        "view": view,
        "id": view.Id,
        "name": safe_name(view, "<unnamed view>"),
        "view_type": _safe_view_type(view),
        "template_id": get_view_template_id(view),
        "design_option_id": get_view_design_option_id(view),
    }


def _safe_view_type(view):
    try:
        return view.ViewType
    except Exception:
        return None


def filter_by_view_template(record, criteria):
    target = criteria.get("template_id")
    if target is None:
        return True  # "None / Any" -> no restriction from this filter
    return record["template_id"] is not None and eid_to_int(record["template_id"]) == eid_to_int(target)


def filter_by_design_option(record, criteria):
    target = criteria.get("design_option_id")
    if target is None:
        return True  # "None / Any" -> no restriction from this filter
    return record["design_option_id"] is not None and eid_to_int(record["design_option_id"]) == eid_to_int(target)


# Future filters go here, e.g.:
# def filter_by_view_type(record, criteria):
#     target = criteria.get("view_type")
#     if target is None:
#         return True
#     return record["view_type"] == target

FILTER_REGISTRY = {
    "template_id": filter_by_view_template,
    "design_option_id": filter_by_design_option,
    # "view_type": filter_by_view_type,   # example future extension
}


def build_active_filters(mode):
    """Mode decides WHICH filters are active; each active filter still
    respects its own 'None / Any' (no restriction) selection."""
    if mode == "TEMPLATE_ONLY":
        keys = ["template_id"]
    elif mode == "OPTION_ONLY":
        keys = ["design_option_id"]
    else:  # "AND"
        keys = ["template_id", "design_option_id"]
    return [FILTER_REGISTRY[k] for k in keys if k in FILTER_REGISTRY]


def view_matches(record, criteria, active_filters):
    for f in active_filters:
        try:
            if not f(record, criteria):
                return False
        except Exception as ex:
            debug_info.append("Filter error on view '{0}': {1}".format(record.get("name"), ex))
            return False
    return True


# ----------------------------------------------------------------------------
# WinForms dialog
# ----------------------------------------------------------------------------

class ViewSelectorDialog(Form):
    def __init__(self, template_items, option_items):
        Form.__init__(self)
        self.Text = "Select Views by Template + Design Option"
        self.Width = 460
        self.Height = 330
        self.FormBorderStyle = FormBorderStyle.FixedDialog
        self.StartPosition = FormStartPosition.CenterScreen
        self.MaximizeBox = False
        self.MinimizeBox = False
        self.Result = None  # ("OK" | "CANCEL", template_index, option_index, mode)

        title_font = Font("Segoe UI", 10, FontStyle.Bold)
        label_font = Font("Segoe UI", 9)

        y = 15

        lbl_header = Label()
        lbl_header.Text = "Revit View Selector"
        lbl_header.Font = title_font
        lbl_header.Location = Point(15, y)
        lbl_header.AutoSize = True
        self.Controls.Add(lbl_header)
        y += 32

        # --- View Template dropdown ---
        lbl_template = Label()
        lbl_template.Text = "View Template:"
        lbl_template.Font = label_font
        lbl_template.Location = Point(15, y)
        lbl_template.AutoSize = True
        self.Controls.Add(lbl_template)
        y += 20

        self.cmb_template = ComboBox()
        self.cmb_template.DropDownStyle = ComboBoxStyle.DropDownList
        self.cmb_template.Location = Point(15, y)
        self.cmb_template.Width = 410
        for item in template_items:
            self.cmb_template.Items.Add(item)
        self.cmb_template.SelectedIndex = 0
        self.Controls.Add(self.cmb_template)
        y += 34

        # --- Design Option dropdown ---
        lbl_option = Label()
        lbl_option.Text = "Design Option:"
        lbl_option.Font = label_font
        lbl_option.Location = Point(15, y)
        lbl_option.AutoSize = True
        self.Controls.Add(lbl_option)
        y += 20

        self.cmb_option = ComboBox()
        self.cmb_option.DropDownStyle = ComboBoxStyle.DropDownList
        self.cmb_option.Location = Point(15, y)
        self.cmb_option.Width = 410
        for item in option_items:
            self.cmb_option.Items.Add(item)
        self.cmb_option.SelectedIndex = 0
        self.Controls.Add(self.cmb_option)
        y += 40

        # --- Match mode ---
        grp = GroupBox()
        grp.Text = "Match mode"
        grp.Location = Point(15, y)
        grp.Width = 410
        grp.Height = 90

        self.rb_and = RadioButton()
        self.rb_and.Text = "Match View Template AND Design Option"
        self.rb_and.Location = Point(10, 20)
        self.rb_and.AutoSize = True
        self.rb_and.Checked = True

        self.rb_template_only = RadioButton()
        self.rb_template_only.Text = "Match View Template only"
        self.rb_template_only.Location = Point(10, 42)
        self.rb_template_only.AutoSize = True

        self.rb_option_only = RadioButton()
        self.rb_option_only.Text = "Match Design Option only"
        self.rb_option_only.Location = Point(10, 64)
        self.rb_option_only.AutoSize = True

        grp.Controls.Add(self.rb_and)
        grp.Controls.Add(self.rb_template_only)
        grp.Controls.Add(self.rb_option_only)
        self.Controls.Add(grp)
        y += 100

        # --- Buttons ---
        btn_ok = Button()
        btn_ok.Text = "Select Matching Views"
        btn_ok.Location = Point(190, y)
        btn_ok.Width = 165
        btn_ok.Click += self.on_ok

        btn_cancel = Button()
        btn_cancel.Text = "Cancel"
        btn_cancel.Location = Point(360, y)
        btn_cancel.Width = 65
        btn_cancel.Click += self.on_cancel

        self.Controls.Add(btn_ok)
        self.Controls.Add(btn_cancel)

        self.AcceptButton = btn_ok

    def on_ok(self, sender, args):
        if self.rb_template_only.Checked:
            mode = "TEMPLATE_ONLY"
        elif self.rb_option_only.Checked:
            mode = "OPTION_ONLY"
        else:
            mode = "AND"
        self.Result = ("OK", self.cmb_template.SelectedIndex, self.cmb_option.SelectedIndex, mode)
        self.DialogResult = DialogResult.OK
        self.Close()

    def on_cancel(self, sender, args):
        self.Result = ("CANCEL", None, None, None)
        self.DialogResult = DialogResult.Cancel
        self.Close()


# ----------------------------------------------------------------------------
# Main execution
# ----------------------------------------------------------------------------

matching_views = []
matching_names = []
matching_ids = []
status_message = ""

try:
    run_trigger = True
    try:
        run_trigger = bool(IN[0])
    except Exception:
        run_trigger = True

    if not run_trigger:
        status_message = "Run input is False. Set the 'Run' Boolean to True and run the graph."
    else:
        view_templates = collect_view_templates()
        design_options = collect_design_options()
        candidate_views = collect_candidate_views()

        # Build dropdown display lists, index 0 is always "None / Any".
        template_items = [NONE_ANY_LABEL]
        template_lookup = [None]  # parallel list of ElementId (or None)
        for vt in view_templates:
            label = "{0}  (Id: {1})".format(safe_name(vt, "<unnamed template>"), eid_to_int(vt.Id))
            template_items.append(label)
            template_lookup.append(vt.Id)

        option_items = [NONE_ANY_LABEL]
        option_lookup = [None]
        for do in design_options:
            set_name = get_design_option_set_name(do)
            primary_tag = ""
            try:
                if do.IsPrimary:
                    primary_tag = "  [Primary]"
            except Exception:
                pass
            label = "{0}  -  Set: {1}{2}".format(safe_name(do, "<unnamed option>"), set_name, primary_tag)
            option_items.append(label)
            option_lookup.append(do.Id)

        debug_info.append("View Templates found: {0}".format(len(view_templates)))
        debug_info.append("Design Options found: {0}".format(len(design_options)))

        dialog = ViewSelectorDialog(template_items, option_items)
        dialog.ShowDialog()

        if dialog.Result is None or dialog.Result[0] != "OK":
            status_message = "Cancelled by user. No changes made to the current selection."
        else:
            _, t_idx, o_idx, mode = dialog.Result

            criteria = {
                "mode": mode,
                "template_id": template_lookup[t_idx] if t_idx is not None else None,
                "design_option_id": option_lookup[o_idx] if o_idx is not None else None,
            }
            debug_info.append("Selected mode: {0}".format(mode))
            debug_info.append("Selected template Id: {0}".format(eid_to_int(criteria["template_id"])))
            debug_info.append("Selected design option Id: {0}".format(eid_to_int(criteria["design_option_id"])))

            active_filters = build_active_filters(mode)

            for v in candidate_views:
                try:
                    record = build_view_record(v)
                    if view_matches(record, criteria, active_filters):
                        matching_views.append(v)
                        matching_names.append(record["name"])
                        matching_ids.append(eid_to_int(record["id"]))
                except Exception as ex:
                    debug_info.append("Error evaluating view: {0}".format(ex))

            # --- Selection ---
            try:
                if uidoc is not None:
                    id_net_list = NetList[ElementId]([v.Id for v in matching_views])
                    uidoc.Selection.SetElementIds(id_net_list)
                else:
                    debug_info.append("No active UIDocument available; selection not applied.")
            except Exception as ex:
                debug_info.append("Failed to set selection: {0}".format(ex))

            count = len(matching_views)
            if count == 0:
                status_message = "No views match the selected criteria."
            elif count == 1:
                status_message = "1 view selected."
            else:
                status_message = "{0} views selected.".format(count)

except Exception:
    status_message = "The tool encountered an error and stopped safely. See debug info for details."
    debug_info.append(traceback.format_exc())

OUT = (matching_views, len(matching_views), matching_names, matching_ids, status_message, debug_info)
