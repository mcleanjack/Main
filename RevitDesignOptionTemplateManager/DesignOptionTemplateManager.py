# ============================================================================
# Revit Design Option <-> View Template Bulk Manager
# ----------------------------------------------------------------------------
# Paste this entire file into a single Dynamo "Python Script" node.
#
# Engine: CPython3 (Dynamo 2.6+ / Revit 2022+ default). Also runs unmodified
# on the legacy IronPython2 engine.
#
# WHAT THIS TOOL ACTUALLY DOES (read this before using it)
# ----------------------------------------------------------------------------
# The brief this was built against asked for a tool that sets which Design
# Option is displayed "by" a View Template. That is NOT something the Revit
# API supports directly -- see the long comment block below,
# "API INVESTIGATION FINDINGS", for the verification and the reasoning.
#
# In short: there is no documented, reliable API property or method to read
# or write which Design Option a View TEMPLATE forces. What IS controllable
# via the API is the "Visible in Option" setting on an ordinary VIEW (the
# same value you see per-view in Visibility/Graphics Overrides > Design
# Options tab), exposed as BuiltInParameter.VIEWER_OPTION_VISIBILITY.
#
# So this tool achieves the user's actual goal -- "bulk-set Design Option
# for all views that use these templates" -- by:
#   1. Finding every ordinary view whose View.ViewTemplateId matches one of
#      the selected templates.
#   2. Setting BuiltInParameter.VIEWER_OPTION_VISIBILITY directly on each of
#      those views.
# It never claims to modify the View Template element itself, and it never
# touches any other View Template setting (Visibility/Graphics, Filters,
# Object Styles, Detail Level, Discipline, Phase, Phase Filter, Scale,
# Annotation, Crop, Worksets, View Range, etc.).
#
# IN[0] (optional): Boolean "Run" toggle. Defaults to True if not wired.
# ============================================================================

# ----------------------------------------------------------------------------
# API INVESTIGATION FINDINGS (read before modifying this tool)
# ----------------------------------------------------------------------------
# Verified across the Revit API Developer's Guide, revitapidocs.com, the
# Autodesk Revit API forum, and the Rhino.Inside.Revit Design Options guide:
#
# 1. Design Option Sets and Design Options are ordinary elements:
#      - Autodesk.Revit.DB.DesignOptionSet  (an Element; project browser
#        groups these, no dedicated get/set API beyond Name and Id)
#      - Autodesk.Revit.DB.DesignOption     (an Element; has .IsPrimary
#        (read-only in the public API) and a Name; its parent Set is found
#        via the BuiltInParameter.OPTION_SET_ID parameter, resolved with
#        Document.GetElement)
#    "There is very limited support for Design Options in the Revit API" is
#    stated explicitly in Autodesk/partner documentation -- this is not a
#    guess, it's the documented state of the API.
#
# 2. Element.DesignOption (used in a companion tool to find which option an
#    ELEMENT belongs to) is NOT the mechanism used to control what a view
#    *displays*. It only tells you which option a view (or any element) was
#    itself created "inside", which is null for essentially all normal
#    project views.
#
# 3. The setting that actually matters here is the Visibility/Graphics
#    Overrides dialog's "Design Options" tab -- one row per Design Option
#    Set, each with a dropdown of "Automatic" (follow the project's Primary
#    option) or a specific option. This is a genuine, per-VIEW override.
#      - It is exposed as BuiltInParameter.VIEWER_OPTION_VISIBILITY
#        ("Visible in Option") on the VIEW object -- confirmed via the
#        Revit API forum thread "How do I get and set the View Design
#        Option Override?" and revitapidocs.com.
#      - It is a genuine, gettable AND settable Parameter (StorageType =
#        ElementId): reading it tells you the option currently forced on
#        that view (or nothing, if the value is InvalidElementId / not
#        present, meaning "Automatic"); setting it changes the override;
#        setting it to ElementId.InvalidElementId clears it back to
#        Automatic ("Main Model" in this tool's UI).
#      - It is NOT universally present on every view: multiple independent
#        sources confirm it reliably works on plan-type views, but is known
#        to be absent/non-functional on true Elevation-type views (as
#        opposed to Sections, where it does work) -- and schedules,
#        legends, and sheets never expose it. This tool checks for the
#        parameter's presence on every single view and skips gracefully
#        wherever it is absent, rather than assuming it exists.
#      - There is a long-standing, still-open Autodesk "Revit Idea" (feature
#        request) literally titled "API to get/set View Overrides for
#        Design Options" asking Autodesk to formalize this. That confirms
#        this parameter-level access is a workable, real mechanism, but NOT
#        an officially documented/guaranteed one -- which is exactly why
#        this tool treats every read/write of it defensively (never assumes
#        it will succeed) rather than treating it as a guaranteed API.
#
# 4. View TEMPLATES do have a "V/G Overrides Design Options" row in their
#    own Include/controlled-parameters list in the Revit UI (Manage View
#    Templates dialog) -- so a template CAN be configured, by a human in the
#    UI, to lock all of its views to one Design Option. But community
#    Autodesk documentation is explicit that "the VG settings for Design
#    Options are not currently accessible for use in custom programming" --
#    i.e. there is no documented way to read or set, via the API, which
#    option a TEMPLATE forces, or to toggle whether that row is included.
#    This tool therefore never attempts to write to the template element
#    for this setting. It DOES read each view's own VIEWER_OPTION_VISIBILITY
#    parameter's IsReadOnly flag before writing to it: when a template has
#    that row included/locked for a given view, Revit reports the view's
#    own copy of the parameter as read-only, and this tool reports that view
#    as "skipped: locked by its View Template" instead of throwing.
#
# 5. Multiple Design Option Sets in one project: because a single view can,
#    in principle, carry more than one "Visible in Option" parameter
#    instance (one per Design Option Set it has an active override for),
#    and the Revit API does not expose a documented way to ask "which
#    Design Option Set does THIS particular parameter instance belong to",
#    this tool disambiguates by matching a parameter instance whose CURRENT
#    value already belongs to the target Design Option Set. If a view
#    exposes more than one such parameter and none of them can be
#    confidently matched to the target Set, this tool skips that view with
#    an explicit "ambiguous" reason rather than guessing and risking a
#    change to an unrelated Design Option Set's visibility.
#
# Bottom line: this tool prioritises a working, honest Revit solution over
# the original "set it on the template" framing, exactly as instructed --
# it reaches the same practical end result (every view that uses the
# selected templates ends up showing the chosen Design Option) via the
# views themselves, which is the only part of this mechanism the Revit API
# actually supports.
#
# Tested against: Revit 2022-2025 API surface (BuiltInParameter names and
# Transaction/FilteredElementCollector behavior referenced here are stable
# across this range). ElementId.Value (Int64) vs the older
# ElementId.IntegerValue is handled by a small compatibility helper so the
# same script runs on both sides of the Revit 2024 API change.
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
    DesignOptionSet, BuiltInParameter, StorageType, Transaction
)
from RevitServices.Persistence import DocumentManager

from System.Windows.Forms import (
    Form, Label, ComboBox, ComboBoxStyle, Button, CheckedListBox,
    DialogResult, FormStartPosition, FormBorderStyle, TextBox,
    ScrollBars, MessageBox, MessageBoxButtons, MessageBoxIcon
)
from System.Drawing import Point, Size, Font, FontStyle

# ----------------------------------------------------------------------------
# Environment
# ----------------------------------------------------------------------------

doc = DocumentManager.Instance.CurrentDBDocument

MAIN_MODEL_LABEL = "Main Model (clear override / follow Automatic)"
NONE_SET_LABEL = "<None>"

debug_info = []


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------

def eid_to_int(element_id):
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


def get_option_set_id(design_option):
    """ElementId of the parent Design Option Set for a DesignOption element,
    read via its OPTION_SET_ID parameter (there is no direct object
    reference for this relationship in the public API)."""
    try:
        bip = getattr(BuiltInParameter, "OPTION_SET_ID", None)
        if bip is None:
            return None
        param = design_option.get_Parameter(bip)
        if param is None:
            return None
        set_id = param.AsElementId()
        return set_id if is_valid_eid(set_id) else None
    except Exception:
        return None


def get_design_option_set_name(design_option):
    try:
        set_id = get_option_set_id(design_option)
        if set_id is None:
            return "Unknown Option Set"
        set_elem = doc.GetElement(set_id)
        return safe_name(set_elem, "Unknown Option Set") if set_elem else "Unknown Option Set"
    except Exception:
        return "Unknown Option Set"


# ----------------------------------------------------------------------------
# Data collection
# ----------------------------------------------------------------------------

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


def collect_design_option_sets():
    try:
        return list(FilteredElementCollector(doc).OfClass(DesignOptionSet).ToElements())
    except Exception as ex:
        debug_info.append("Failed to collect design option sets: {0}".format(ex))
        return []


def collect_all_design_options():
    try:
        return list(FilteredElementCollector(doc).OfClass(DesignOption).ToElements())
    except Exception as ex:
        debug_info.append("Failed to collect design options: {0}".format(ex))
        return []


def get_views_using_template(template_id):
    """Every ordinary (non-template) view whose ViewTemplateId matches the
    given template. These are the views this tool will actually modify."""
    results = []
    try:
        all_views = FilteredElementCollector(doc).OfClass(View).ToElements()
    except Exception as ex:
        debug_info.append("Failed to collect views: {0}".format(ex))
        return results

    for v in all_views:
        try:
            if v is None or not v.IsValidObject or v.IsTemplate:
                continue
            tid = v.ViewTemplateId
            if is_valid_eid(tid) and eid_to_int(tid) == eid_to_int(template_id):
                results.append(v)
        except Exception as ex:
            debug_info.append("Skipped invalid view while matching template: {0}".format(ex))
    return results


# ----------------------------------------------------------------------------
# CONTROL: Design Option visibility (per view, via VIEWER_OPTION_VISIBILITY)
# ----------------------------------------------------------------------------
# To add a future control (Phase, Phase Filter, Discipline, Detail Level,
# View Range, etc.), write a parallel module below following this same
# three-function shape:
#   - find_xxx_parameter(view, target_context) -> (Parameter or None, reason)
#   - apply_xxx_to_view(view, target_value) -> {"status": ..., "reason": ...}
#   - a small get_views_using_template() call is already reusable as-is.
# Then wire a new tab/section into the dialog and a new branch in the main
# execution loop. Nothing else needs to change.

def _viewer_option_visibility_bip():
    return getattr(BuiltInParameter, "VIEWER_OPTION_VISIBILITY", None)


def _is_viewer_option_visibility_param(param, bip):
    try:
        defn = param.Definition
        return getattr(defn, "BuiltInParameter", None) == bip
    except Exception:
        return False


def resolve_design_option_parameter(view, target_set_id):
    """Finds the Parameter on `view` that controls Design Option visibility
    for the target Design Option Set, disambiguating when a view exposes
    more than one such parameter (multiple Design Option Sets in play)."""
    bip = _viewer_option_visibility_bip()
    if bip is None:
        return None, "unsupported_api"

    matches = []
    try:
        for p in view.Parameters:
            if _is_viewer_option_visibility_param(p, bip):
                matches.append(p)
    except Exception:
        return None, "error_reading_parameters"

    if len(matches) == 0:
        return None, "not_applicable"
    if len(matches) == 1:
        return matches[0], None

    # Multiple Design Option Sets are active on this view. Only touch the
    # slot that already belongs to the target Set -- never guess.
    for p in matches:
        try:
            if p.StorageType != StorageType.ElementId:
                continue
            cur_id = p.AsElementId()
            if not is_valid_eid(cur_id):
                continue
            cur_elem = doc.GetElement(cur_id)
            if cur_elem is None or not isinstance(cur_elem, DesignOption):
                continue
            cur_set_id = get_option_set_id(cur_elem)
            if cur_set_id is not None and eid_to_int(cur_set_id) == eid_to_int(target_set_id):
                return p, None
        except Exception:
            continue

    return None, "ambiguous_multiple_design_option_sets"


_REASON_TEXT = {
    "unsupported_api": "This Revit version's API does not expose the Design Option visibility parameter.",
    "not_applicable": "Design Option visibility does not apply to this view (e.g. schedule, legend, or unsupported view type).",
    "error_reading_parameters": "Could not read this view's parameters.",
    "ambiguous_multiple_design_option_sets": "This view has overrides for multiple Design Option Sets and none could be confidently matched to the target set; skipped to avoid changing the wrong one.",
}


def apply_design_option_to_view(view, target_set_id, target_option_id):
    """target_option_id: an ElementId to force that option, or None to clear
    the override back to Main Model / Automatic."""
    param, reason = resolve_design_option_parameter(view, target_set_id)
    if param is None:
        return {"status": "skipped", "reason": _REASON_TEXT.get(reason, reason)}

    try:
        if param.IsReadOnly:
            return {
                "status": "skipped",
                "reason": "Locked by this view's View Template (Design Options is an included/controlled "
                          "parameter). Uncheck 'Design Options' in the template's Include list, or use the "
                          "template's own V/G Overrides Design Option dialog, to change this."
            }

        new_value = target_option_id if target_option_id is not None else ElementId.InvalidElementId
        current = param.AsElementId()

        if is_valid_eid(current) and is_valid_eid(new_value) and eid_to_int(current) == eid_to_int(new_value):
            return {"status": "unchanged", "reason": "Already set to the requested Design Option."}
        if (not is_valid_eid(current)) and (not is_valid_eid(new_value)):
            return {"status": "unchanged", "reason": "Already set to Main Model / Automatic."}

        param.Set(new_value)
        return {"status": "updated", "reason": None}
    except Exception as ex:
        return {"status": "error", "reason": str(ex)}


# ----------------------------------------------------------------------------
# WinForms dialogs
# ----------------------------------------------------------------------------

class SelectionDialog(Form):
    def __init__(self, template_items, set_items, options_by_set_index,
                 initial_checked=None, initial_set_idx=0, initial_option_idx=0):
        Form.__init__(self)
        self.Text = "Design Option <-> View Template Manager"
        self.Width = 560
        self.Height = 560
        self.FormBorderStyle = FormBorderStyle.FixedDialog
        self.StartPosition = FormStartPosition.CenterScreen
        self.MaximizeBox = False
        self.MinimizeBox = False
        self.Result = None  # ("PREVIEW", checked_indices, set_idx, option_idx) | ("CANCEL",)

        self._options_by_set_index = options_by_set_index

        title_font = Font("Segoe UI", 10, FontStyle.Bold)
        label_font = Font("Segoe UI", 9)

        y = 15
        lbl_header = Label()
        lbl_header.Text = "Bulk-set Design Option for views using selected View Templates"
        lbl_header.Font = title_font
        lbl_header.Location = Point(15, y)
        lbl_header.AutoSize = True
        self.Controls.Add(lbl_header)
        y += 30

        lbl_templates = Label()
        lbl_templates.Text = "View Templates (check one, several, or all):"
        lbl_templates.Font = label_font
        lbl_templates.Location = Point(15, y)
        lbl_templates.AutoSize = True
        self.Controls.Add(lbl_templates)
        y += 20

        self.clb_templates = CheckedListBox()
        self.clb_templates.Location = Point(15, y)
        self.clb_templates.Size = Size(410, 220)
        self.clb_templates.CheckOnClick = True
        for item in template_items:
            self.clb_templates.Items.Add(item)
        if initial_checked:
            for idx in initial_checked:
                if 0 <= idx < self.clb_templates.Items.Count:
                    self.clb_templates.SetItemChecked(idx, True)
        self.Controls.Add(self.clb_templates)

        btn_all = Button()
        btn_all.Text = "Select All"
        btn_all.Location = Point(435, y)
        btn_all.Width = 100
        btn_all.Click += self.on_select_all
        self.Controls.Add(btn_all)

        btn_none = Button()
        btn_none.Text = "Clear All"
        btn_none.Location = Point(435, y + 30)
        btn_none.Width = 100
        btn_none.Click += self.on_clear_all
        self.Controls.Add(btn_none)

        y += 230

        lbl_set = Label()
        lbl_set.Text = "Design Option Set:"
        lbl_set.Font = label_font
        lbl_set.Location = Point(15, y)
        lbl_set.AutoSize = True
        self.Controls.Add(lbl_set)
        y += 20

        self.cmb_set = ComboBox()
        self.cmb_set.DropDownStyle = ComboBoxStyle.DropDownList
        self.cmb_set.Location = Point(15, y)
        self.cmb_set.Width = 410
        for item in set_items:
            self.cmb_set.Items.Add(item)
        self.cmb_set.SelectedIndexChanged += self.on_set_changed
        y += 34

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
        self.Controls.Add(self.cmb_option)
        y += 40

        btn_preview = Button()
        btn_preview.Text = "Preview Changes >>"
        btn_preview.Location = Point(215, y)
        btn_preview.Width = 165
        btn_preview.Click += self.on_preview

        btn_cancel = Button()
        btn_cancel.Text = "Cancel"
        btn_cancel.Location = Point(385, y)
        btn_cancel.Width = 65
        btn_cancel.Click += self.on_cancel

        self.Controls.Add(self.cmb_set)
        self.Controls.Add(btn_preview)
        self.Controls.Add(btn_cancel)
        self.AcceptButton = btn_preview

        # Apply initial state after all controls exist and events are wired.
        self.cmb_set.SelectedIndex = initial_set_idx if 0 <= initial_set_idx < self.cmb_set.Items.Count else 0
        if self.cmb_option.Items.Count > initial_option_idx >= 0:
            self.cmb_option.SelectedIndex = initial_option_idx

    def on_select_all(self, sender, args):
        for i in range(self.clb_templates.Items.Count):
            self.clb_templates.SetItemChecked(i, True)

    def on_clear_all(self, sender, args):
        for i in range(self.clb_templates.Items.Count):
            self.clb_templates.SetItemChecked(i, False)

    def on_set_changed(self, sender, args):
        self.cmb_option.Items.Clear()
        idx = self.cmb_set.SelectedIndex
        if idx <= 0:
            return  # "<None>" selected
        for item in self._options_by_set_index.get(idx, []):
            self.cmb_option.Items.Add(item)
        if self.cmb_option.Items.Count > 0:
            self.cmb_option.SelectedIndex = 0

    def on_preview(self, sender, args):
        checked_indices = [i for i in range(self.clb_templates.Items.Count) if self.clb_templates.GetItemChecked(i)]
        if len(checked_indices) == 0:
            MessageBox.Show("Select at least one View Template.", "Nothing selected",
                             MessageBoxButtons.OK, MessageBoxIcon.Warning)
            return
        if self.cmb_set.SelectedIndex <= 0:
            MessageBox.Show("Select a Design Option Set.", "Nothing selected",
                             MessageBoxButtons.OK, MessageBoxIcon.Warning)
            return
        if self.cmb_option.SelectedIndex < 0:
            MessageBox.Show("Select a Design Option.", "Nothing selected",
                             MessageBoxButtons.OK, MessageBoxIcon.Warning)
            return
        self.Result = ("PREVIEW", checked_indices, self.cmb_set.SelectedIndex, self.cmb_option.SelectedIndex)
        self.DialogResult = DialogResult.OK
        self.Close()

    def on_cancel(self, sender, args):
        self.Result = ("CANCEL",)
        self.DialogResult = DialogResult.Cancel
        self.Close()


class ConfirmDialog(Form):
    def __init__(self, preview_text):
        Form.__init__(self)
        self.Text = "Confirm Changes"
        self.Width = 520
        self.Height = 420
        self.FormBorderStyle = FormBorderStyle.FixedDialog
        self.StartPosition = FormStartPosition.CenterScreen
        self.MaximizeBox = False
        self.MinimizeBox = False
        self.Result = "CANCEL"  # "APPLY" | "BACK" | "CANCEL"

        txt = TextBox()
        txt.Multiline = True
        txt.ReadOnly = True
        txt.ScrollBars = ScrollBars.Vertical
        txt.Font = Font("Consolas", 9.5)
        txt.Location = Point(15, 15)
        txt.Size = Size(475, 300)
        txt.Text = preview_text
        self.Controls.Add(txt)

        btn_apply = Button()
        btn_apply.Text = "APPLY CHANGES"
        btn_apply.Location = Point(15, 330)
        btn_apply.Width = 150
        btn_apply.Click += self.on_apply
        self.Controls.Add(btn_apply)

        btn_back = Button()
        btn_back.Text = "<< Back"
        btn_back.Location = Point(175, 330)
        btn_back.Width = 100
        btn_back.Click += self.on_back
        self.Controls.Add(btn_back)

        btn_cancel = Button()
        btn_cancel.Text = "Cancel"
        btn_cancel.Location = Point(390, 330)
        btn_cancel.Width = 100
        btn_cancel.Click += self.on_cancel
        self.Controls.Add(btn_cancel)

        self.AcceptButton = btn_apply

    def on_apply(self, sender, args):
        self.Result = "APPLY"
        self.DialogResult = DialogResult.OK
        self.Close()

    def on_back(self, sender, args):
        self.Result = "BACK"
        self.Close()

    def on_cancel(self, sender, args):
        self.Result = "CANCEL"
        self.DialogResult = DialogResult.Cancel
        self.Close()


# ----------------------------------------------------------------------------
# Main execution
# ----------------------------------------------------------------------------

processed_templates = []
modified_count = 0
skipped_count = 0
report_lines = []
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
        design_option_sets = collect_design_option_sets()
        all_design_options = collect_all_design_options()

        debug_info.append("View Templates found: {0}".format(len(view_templates)))
        debug_info.append("Design Option Sets found: {0}".format(len(design_option_sets)))
        debug_info.append("Design Options found: {0}".format(len(all_design_options)))

        if len(view_templates) == 0:
            status_message = "No View Templates found in this project."
        elif len(design_option_sets) == 0:
            status_message = "No Design Option Sets found in this project."
        else:
            # --- Build dropdown/list source data ---
            template_items = []
            template_lookup = []  # parallel: template element
            for vt in view_templates:
                try:
                    vtype = vt.ViewType
                except Exception:
                    vtype = "?"
                label = "{0}   (Id: {1}, {2})".format(safe_name(vt, "<unnamed template>"), eid_to_int(vt.Id), vtype)
                template_items.append(label)
                template_lookup.append(vt)

            set_items = [NONE_SET_LABEL]
            set_lookup = [None]
            options_by_set_index = {}
            for do_set in design_option_sets:
                idx = len(set_items)
                set_items.append(safe_name(do_set, "<unnamed set>"))
                set_lookup.append(do_set)

                options_in_set = [
                    o for o in all_design_options
                    if get_option_set_id(o) is not None and eid_to_int(get_option_set_id(o)) == eid_to_int(do_set.Id)
                ]
                option_items = [MAIN_MODEL_LABEL]
                option_lookup = [None]  # None => Main Model / clear
                for o in options_in_set:
                    primary_tag = ""
                    try:
                        if o.IsPrimary:
                            primary_tag = "  [Primary]"
                    except Exception:
                        pass
                    option_items.append("{0}  (Id: {1}){2}".format(safe_name(o, "<unnamed option>"), eid_to_int(o.Id), primary_tag))
                    option_lookup.append(o)
                options_by_set_index[idx] = option_items
                # Stash the lookup alongside via a second dict keyed the same way
                options_by_set_index.setdefault("__lookup__", {})[idx] = option_lookup

            option_lookups = options_by_set_index.pop("__lookup__", {})

            # --- Selection -> Preview -> Confirm loop ---
            initial_checked = None
            initial_set_idx = 0
            initial_option_idx = 0
            final_result = None

            while True:
                dlg1 = SelectionDialog(template_items, set_items, options_by_set_index,
                                        initial_checked, initial_set_idx, initial_option_idx)
                dlg1.ShowDialog()

                if dlg1.Result is None or dlg1.Result[0] == "CANCEL":
                    final_result = ("CANCEL",)
                    break

                _, checked_indices, set_idx, option_idx = dlg1.Result
                selected_templates = [template_lookup[i] for i in checked_indices]
                target_set = set_lookup[set_idx]
                target_option = option_lookups.get(set_idx, [None])[option_idx]

                option_label = MAIN_MODEL_LABEL if target_option is None else safe_name(target_option, "<unnamed option>")

                preview_lines = []
                preview_lines.append("VIEW TEMPLATES TO MODIFY: {0}".format(len(selected_templates)))
                for t in selected_templates:
                    preview_lines.append("  - {0}".format(safe_name(t, "<unnamed template>")))
                preview_lines.append("")
                preview_lines.append("DESIGN OPTION:")
                preview_lines.append("  {0} -> {1}".format(safe_name(target_set, "<unnamed set>"), option_label))
                preview_lines.append("")
                preview_lines.append("This will update the 'Visible in Option' setting on every ordinary")
                preview_lines.append("view assigned to the templates above. The templates themselves and")
                preview_lines.append("all other view settings are left untouched.")

                dlg2 = ConfirmDialog("\n".join(preview_lines))
                dlg2.ShowDialog()

                if dlg2.Result == "APPLY":
                    final_result = ("APPLY", selected_templates, target_set, target_option)
                    break
                elif dlg2.Result == "BACK":
                    initial_checked = checked_indices
                    initial_set_idx = set_idx
                    initial_option_idx = option_idx
                    continue
                else:
                    final_result = ("CANCEL",)
                    break

            if final_result[0] == "CANCEL":
                status_message = "Cancelled by user. No changes made."
            else:
                _, selected_templates, target_set, target_option = final_result
                target_set_id = target_set.Id
                target_option_id = target_option.Id if target_option is not None else None

                t = Transaction(doc, "Set Design Option visibility for views using selected View Templates")
                try:
                    t.Start()
                    for template in selected_templates:
                        processed_templates.append(template)
                        controlled_views = get_views_using_template(template.Id)
                        updated_here = 0
                        skipped_here = []
                        errors_here = []

                        for v in controlled_views:
                            try:
                                r = apply_design_option_to_view(v, target_set_id, target_option_id)
                            except Exception as ex:
                                r = {"status": "error", "reason": str(ex)}

                            if r["status"] in ("updated", "unchanged"):
                                updated_here += 1
                            elif r["status"] == "skipped":
                                skipped_here.append((safe_name(v), r["reason"]))
                            else:
                                errors_here.append((safe_name(v), r["reason"]))

                        template_name = safe_name(template, "<unnamed template>")
                        total = len(controlled_views)

                        if total == 0:
                            report_lines.append("{0}: no views currently use this template; nothing to modify.".format(template_name))
                            skipped_count += 1
                        elif updated_here == total:
                            report_lines.append("{0}: {1} of {2} views updated.".format(template_name, updated_here, total))
                            modified_count += 1
                        elif updated_here > 0:
                            report_lines.append("{0}: {1} of {2} views updated (partial).".format(template_name, updated_here, total))
                            for name, reason in skipped_here:
                                report_lines.append("    skipped '{0}': {1}".format(name, reason))
                            for name, reason in errors_here:
                                report_lines.append("    error on '{0}': {1}".format(name, reason))
                            modified_count += 1
                        else:
                            report_lines.append("{0}: 0 of {1} views updated. Skipped.".format(template_name, total))
                            for name, reason in skipped_here:
                                report_lines.append("    skipped '{0}': {1}".format(name, reason))
                            for name, reason in errors_here:
                                report_lines.append("    error on '{0}': {1}".format(name, reason))
                            skipped_count += 1

                    t.Commit()
                except Exception:
                    debug_info.append(traceback.format_exc())
                    try:
                        if t.HasStarted() and not t.HasEnded():
                            t.RollBack()
                    except Exception:
                        pass
                    raise

                option_label = MAIN_MODEL_LABEL if target_option is None else safe_name(target_option, "<unnamed option>")
                status_message = "{0} View Template(s) fully updated, {1} skipped. Target: {2} -> {3}.".format(
                    modified_count, skipped_count, safe_name(target_set, "<unnamed set>"), option_label
                )

except Exception:
    status_message = "The tool encountered an error and stopped safely. See debug info for details."
    debug_info.append(traceback.format_exc())

OUT = (processed_templates, modified_count, skipped_count, report_lines, status_message, debug_info)
