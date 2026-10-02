# ============================================================================
# Revit Window / Door Mark Renumber - by clicking in order
# ----------------------------------------------------------------------------
# Paste this entire file into a single Dynamo "Python Script" node.
#
# Engine: CPython3 / PythonNet3 (Dynamo 2.13+ / Revit 2022+). Also runs
# unmodified on the legacy IronPython2 engine.
#
# Works on WINDOWS or DOORS - choose at the top of the dialog. Everything
# below says "window", but applies the same way to doors in doors mode.
#
# What it does:
#   1. Shows a small dialog asking for the Mark format: prefix (e.g. "W."),
#      start number (e.g. 1) and number of digits (e.g. 2 -> "W.01").
#      Defaults are guessed from the Marks of the windows in the active view.
#   2. Lets you click windows in the active view ONE AT A TIME, in the order
#      you want them numbered. You can click either the window tag OR the
#      window itself; anything else can't be picked. Clicked windows stay
#      highlighted blue until the script finishes. Click Finish (the green
#      tick on the Options Bar) when you're done, or Cancel to stop.
#      Don't drag a selection box - that loses the click order.
#   3. Optionally (checkbox in the dialog) shows a preview
#      ("W.06 -> W.01", ...) and asks for confirmation.
#   4. Writes the new values to each window's "Mark" parameter (Identity
#      Data) in a single transaction, so one Ctrl+Z in Revit undoes it all.
#      Tags read the Mark, so they update automatically.
#
# Windows you DIDN'T click (visible in the active view) are, depending on the
# option chosen in the dialog, either:
#   - left alone, with any duplicate Marks that result listed in the report, or
#   - renumbered after the clicked ones, keeping their current Mark order.
# Only windows visible in the active view are considered, so windows in
# Design Options the view isn't showing are never touched. Matching Marks in
# two DIFFERENT Design Options are intentional: they are never changed and
# never reported as duplicates. Only a match in the same option, or one
# involving a main-model window, is reported (it is still never changed).
#
# IMPORTANT: run the graph in MANUAL run mode (not Automatic), otherwise the
# picking session restarts every time the graph is re-evaluated. Just press
# Run again for the next renumber (see IN[0] below).
#
# IN[0]: wire a Boolean node here. Its value doesn't matter - at the end of
#        every run the script flips it (True <-> False). Dynamo only re-runs
#        a node when an input has changed, so the flip is what lets you press
#        Run again for the next renumber. Without it, Run only works once.
#        (Marker used to find this node: RENUMBER-BY-CLICK-NODE)
#
# OUT = (renamed_windows, changed_count, report_lines, status_message,
#        debug_info)
# ============================================================================

import clr
import re
import traceback
import uuid

clr.AddReference('RevitAPI')
clr.AddReference('RevitAPIUI')
clr.AddReference('RevitServices')
clr.AddReference('System.Windows.Forms')
clr.AddReference('System.Drawing')

from Autodesk.Revit.DB import (
    FilteredElementCollector, BuiltInCategory, BuiltInParameter, ElementId,
    FamilyInstance, IndependentTag
)
from Autodesk.Revit.UI.Selection import ObjectType, ISelectionFilter
from Autodesk.Revit.Exceptions import OperationCanceledException
from RevitServices.Persistence import DocumentManager
from RevitServices.Transactions import TransactionManager

from System.Collections.Generic import List as NetList
from System.Windows.Forms import (
    Form, Label, TextBox, Button, RadioButton, GroupBox, CheckBox,
    DialogResult, FormStartPosition, FormBorderStyle, ScrollBars
)
from System.Drawing import Point, Size

# ----------------------------------------------------------------------------
# Environment
# ----------------------------------------------------------------------------

doc = DocumentManager.Instance.CurrentDBDocument
uiapp = DocumentManager.Instance.CurrentUIApplication
uidoc = uiapp.ActiveUIDocument if uiapp is not None else None

# What is being renumbered. Chosen in the settings dialog.
MODES = {
    "windows": {"bic": BuiltInCategory.OST_Windows, "noun": "window",
                "default_prefix": "W."},
    "doors":   {"bic": BuiltInCategory.OST_Doors, "noun": "door",
                "default_prefix": "D."},
}
MODE = dict(MODES["windows"])
MODE["cat_id"] = ElementId(MODE["bic"])


def set_mode(key):
    MODE.clear()
    MODE.update(MODES[key])
    MODE["cat_id"] = ElementId(MODE["bic"])

MARK_RE = re.compile(r'^(.*?)(\d+)$')

SCRIPT_VERSION = "v17 (Boolean flip fix)"

debug_info = ["Script version: " + SCRIPT_VERSION]


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


def is_window(element):
    """True for an element of the category being renumbered (windows or
    doors - the name is kept from the windows-only version)."""
    try:
        return (isinstance(element, FamilyInstance)
                and element.Category is not None
                and element.Category.Id.Equals(MODE["cat_id"]))
    except Exception:
        return False


def get_mark(element):
    p = element.get_Parameter(BuiltInParameter.ALL_MODEL_MARK)
    if p is None:
        return ""
    v = p.AsString()
    return v if v else ""


def natural_key(text):
    """Sort 'W.2' before 'W.10'."""
    parts = re.split(r'(\d+)', text or "")
    return [int(s) if s.isdigit() else s.lower() for s in parts]


def parse_int(text, default, lo, hi):
    try:
        n = int(str(text).strip())
    except Exception:
        return default
    return max(lo, min(hi, n))


def format_mark(prefix, number, digits):
    return "{0}{1}".format(prefix, str(number).zfill(digits))


def option_key(element):
    """Design Option id as an int, or None for the main model."""
    try:
        opt = element.DesignOption
    except Exception:
        opt = None
    return eid_to_int(opt.Id) if opt is not None else None


def options_clash(a, b):
    """Two windows with the same Mark only count as duplicates if they are
    in the same Design Option, or either one is in the main model. The same
    Mark in two different Design Options is fine."""
    return a is None or b is None or a == b


def window_from_picked(element):
    """Return (window, None) for a clicked window or window tag, else
    (None, reason)."""
    if element is None:
        return None, "nothing found"
    if is_window(element):
        return element, None
    if isinstance(element, IndependentTag):
        tagged_ids = []
        try:
            tagged_ids = list(element.GetTaggedLocalElementIds())   # Revit 2022+
        except AttributeError:
            try:
                tagged_ids = [element.TaggedLocalElementId]          # Revit < 2022
            except Exception:
                tagged_ids = []
        for tid in tagged_ids:
            tagged = doc.GetElement(tid)
            if is_window(tagged):
                return tagged, None
        return None, "that tag isn't tagging a {0} in this model (linked ones can't be renumbered)".format(MODE["noun"])
    cat = element.Category.Name if element.Category is not None else "element"
    return None, "that's a {0}, not a {1} or {1} tag".format(cat, MODE["noun"])


def collect_view_windows(view, mode_key=None):
    """Windows (or, in doors mode, doors) visible in the view.
    These are the ones checked for duplicates and numbered on with the
    "Number them after the clicked ones" option."""
    mode = MODES[mode_key] if mode_key else MODE
    cat_id = ElementId(mode["bic"])
    found = []
    for e in (FilteredElementCollector(doc, view.Id)
              .OfCategory(mode["bic"])
              .WhereElementIsNotElementType()
              .ToElements()):
        if not isinstance(e, FamilyInstance) or e.Category is None or not e.Category.Id.Equals(cat_id):
            continue
        found.append(e)
    return found


def guess_format(windows, default_prefix="W."):
    """Guess (prefix, digits) from the most common existing Mark pattern."""
    counts = {}
    for w in windows:
        m = MARK_RE.match(get_mark(w))
        if m:
            key = (m.group(1), len(m.group(2)))
            counts[key] = counts.get(key, 0) + 1
    if not counts:
        return default_prefix, 2
    return max(counts.items(), key=lambda kv: kv[1])[0]


# ----------------------------------------------------------------------------
# Settings dialog
# ----------------------------------------------------------------------------

class SettingsForm(Form):
    def __init__(self, guesses):
        """guesses: {"windows": (prefix, digits), "doors": (prefix, digits)}"""
        Form.__init__(self)
        self.guesses = guesses
        prefix, digits = guesses["windows"]
        self.Text = "Renumber Window / Door Marks by Clicking"
        self.FormBorderStyle = FormBorderStyle.FixedDialog
        self.StartPosition = FormStartPosition.CenterScreen
        self.MaximizeBox = False
        self.MinimizeBox = False
        self.TopMost = True
        self.ClientSize = Size(420, 405)

        y = 15
        lbl = Label()
        lbl.Text = "Renumber:"
        lbl.Location = Point(15, y + 3)
        lbl.Size = Size(130, 20)
        self.Controls.Add(lbl)
        self.windows_radio = RadioButton()
        self.windows_radio.Text = "Windows"
        self.windows_radio.Location = Point(150, y)
        self.windows_radio.Size = Size(85, 22)
        self.windows_radio.Checked = True
        self.Controls.Add(self.windows_radio)
        self.doors_radio = RadioButton()
        self.doors_radio.Text = "Doors"
        self.doors_radio.Location = Point(240, y)
        self.doors_radio.Size = Size(150, 22)
        self.Controls.Add(self.doors_radio)

        y += 35
        lbl = Label()
        lbl.Text = "Prefix:"
        lbl.Location = Point(15, y + 3)
        lbl.Size = Size(130, 20)
        self.Controls.Add(lbl)
        self.prefix_box = TextBox()
        self.prefix_box.Text = prefix
        self.prefix_box.Location = Point(150, y)
        self.prefix_box.Size = Size(100, 22)
        self.Controls.Add(self.prefix_box)

        y += 35
        lbl = Label()
        lbl.Text = "Start number:"
        lbl.Location = Point(15, y + 3)
        lbl.Size = Size(130, 20)
        self.Controls.Add(lbl)
        self.start_box = TextBox()
        self.start_box.Text = "1"
        self.start_box.Location = Point(150, y)
        self.start_box.Size = Size(100, 22)
        self.Controls.Add(self.start_box)

        y += 35
        lbl = Label()
        lbl.Text = "Digits (zero padding):"
        lbl.Location = Point(15, y + 3)
        lbl.Size = Size(130, 20)
        self.Controls.Add(lbl)
        self.digits_box = TextBox()
        self.digits_box.Text = str(digits)
        self.digits_box.Location = Point(150, y)
        self.digits_box.Size = Size(100, 22)
        self.Controls.Add(self.digits_box)

        self.example = Label()
        self.example.Location = Point(265, y - 32)
        self.example.Size = Size(140, 40)
        self.Controls.Add(self.example)
        self.prefix_box.TextChanged += self.update_example
        self.start_box.TextChanged += self.update_example
        self.digits_box.TextChanged += self.update_example
        self.update_example(None, None)

        y += 40
        self.others_group = grp = GroupBox()
        grp.Text = "Windows in this view that you DON'T click"
        grp.Location = Point(15, y)
        grp.Size = Size(390, 80)
        self.leave_radio = RadioButton()
        self.leave_radio.Text = "Leave them alone (report any duplicate Marks)"
        self.leave_radio.Location = Point(10, 22)
        self.leave_radio.Size = Size(370, 22)
        self.leave_radio.Checked = True
        self.continue_radio = RadioButton()
        self.continue_radio.Text = "Number them after the clicked ones (keep their order)"
        self.continue_radio.Location = Point(10, 48)
        self.continue_radio.Size = Size(370, 22)
        grp.Controls.Add(self.leave_radio)
        grp.Controls.Add(self.continue_radio)
        self.Controls.Add(grp)

        y += 92
        self.hint = hint = Label()
        hint.Text = ("Next: click window tags (or windows) one at a time, in order.\n"
                     "Click Finish (green tick) on the Options Bar when done.")
        hint.Location = Point(15, y)
        hint.Size = Size(390, 36)
        self.Controls.Add(hint)

        self.preview_check = CheckBox()
        self.preview_check.Text = "Show a preview before applying"
        self.preview_check.Location = Point(15, y + 40)
        self.preview_check.Size = Size(300, 22)
        self.preview_check.Checked = False
        self.Controls.Add(self.preview_check)

        ok = Button()
        ok.Text = "Start Picking"
        ok.Location = Point(210, 365)
        ok.Size = Size(100, 28)
        ok.DialogResult = DialogResult.OK
        self.Controls.Add(ok)
        self.AcceptButton = ok

        cancel = Button()
        cancel.Text = "Cancel"
        cancel.Location = Point(315, 365)
        cancel.Size = Size(90, 28)
        cancel.DialogResult = DialogResult.Cancel
        self.Controls.Add(cancel)
        self.CancelButton = cancel

        # Switching between windows and doors swaps the guessed prefix
        # (e.g. W. -> D.) and the wording.
        self.windows_radio.CheckedChanged += self.on_mode_changed

    def mode_key(self):
        return "doors" if self.doors_radio.Checked else "windows"

    def on_mode_changed(self, sender, args):
        key = self.mode_key()
        prefix, digits = self.guesses[key]
        self.prefix_box.Text = prefix
        self.digits_box.Text = str(digits)
        if key == "doors":
            self.others_group.Text = "Doors in this view that you DON'T click"
            self.hint.Text = ("Next: click door tags (or doors) one at a time, in order.\n"
                              "Click Finish (green tick) on the Options Bar when done.")
        else:
            self.others_group.Text = "Windows in this view that you DON'T click"
            self.hint.Text = ("Next: click window tags (or windows) one at a time, in order.\n"
                              "Click Finish (green tick) on the Options Bar when done.")

    def get_start(self):
        return parse_int(self.start_box.Text, 1, 0, 99999)

    def get_digits(self):
        return parse_int(self.digits_box.Text, 2, 1, 6)

    def update_example(self, sender, args):
        start = self.get_start()
        digits = self.get_digits()
        self.example.Text = "e.g. {0}, {1}, ...".format(
            format_mark(self.prefix_box.Text, start, digits),
            format_mark(self.prefix_box.Text, start + 1, digits))


class PreviewForm(Form):
    """Scrollable preview of the changes with OK / Cancel."""
    def __init__(self, heading, body):
        Form.__init__(self)
        self.Text = "Renumber Window Marks - Preview"
        self.FormBorderStyle = FormBorderStyle.FixedDialog
        self.StartPosition = FormStartPosition.CenterScreen
        self.MaximizeBox = False
        self.MinimizeBox = False
        self.TopMost = True
        self.ClientSize = Size(420, 460)

        lbl = Label()
        lbl.Text = heading
        lbl.Location = Point(15, 12)
        lbl.Size = Size(390, 36)
        self.Controls.Add(lbl)

        box = TextBox()
        box.Multiline = True
        box.ReadOnly = True
        box.ScrollBars = ScrollBars.Vertical
        box.Text = body.replace("\n", "\r\n")
        box.Location = Point(15, 50)
        box.Size = Size(390, 360)
        self.Controls.Add(box)

        ok = Button()
        ok.Text = "Apply"
        ok.Location = Point(210, 420)
        ok.Size = Size(100, 28)
        ok.DialogResult = DialogResult.OK
        self.Controls.Add(ok)
        self.AcceptButton = ok

        cancel = Button()
        cancel.Text = "Cancel"
        cancel.Location = Point(315, 420)
        cancel.Size = Size(90, 28)
        cancel.DialogResult = DialogResult.Cancel
        self.Controls.Add(cancel)
        self.CancelButton = cancel


# ----------------------------------------------------------------------------
# Picking
# ----------------------------------------------------------------------------

def highlight(ids):
    """Select (= highlight blue) the given ElementIds in Revit."""
    try:
        uidoc.Selection.SetElementIds(NetList[ElementId](ids))
    except Exception:
        pass


def make_selection_filter():
    """ISelectionFilter that only lets windows and window tags be picked.
    The class gets a unique .NET namespace each run, because PythonNet
    refuses to define the same .NET type twice in one Dynamo session.
    Returns None if the engine can't build it (picking still works, other
    elements are just ignored afterwards)."""
    try:
        def allow_element(self, element):
            try:
                return window_from_picked(element)[0] is not None
            except Exception:
                return False

        def allow_reference(self, reference, position):
            return False

        cls = type("WindowOrTagFilter", (ISelectionFilter,), {
            "__namespace__": "WindowMarkRenumber_" + uuid.uuid4().hex,
            "AllowElement": allow_element,
            "AllowReference": allow_reference,
        })
        return cls()
    except Exception:
        debug_info.append("Selection filter unavailable: " + traceback.format_exc())
        return None


def pick_windows_in_order():
    """Lets the user click windows/tags; Revit keeps them highlighted blue.
    Finish with the green tick (Finish) on the Options Bar.
    Returns (windows in click order, cancelled)."""
    prompt = ("Click {0} tags (or {0}s) one at a time in order, "
              "then click Finish (green tick) on the Options Bar").format(MODE["noun"])
    sel_filter = make_selection_filter()
    highlight([])
    try:
        refs = None
        if sel_filter is not None:
            try:
                refs = uidoc.Selection.PickObjects(ObjectType.Element, sel_filter, prompt)
            except OperationCanceledException:
                raise
            except Exception:
                debug_info.append("PickObjects with filter failed, retrying without: " + traceback.format_exc())
                refs = None
        if refs is None:
            refs = uidoc.Selection.PickObjects(ObjectType.Element, prompt)
    except OperationCanceledException:
        return [], True

    windows = []
    seen = set()
    # Revit hands back PickObjects results newest-first, so reverse them
    # to get the order they were clicked in.
    for ref in reversed(list(refs)):
        window, reason = window_from_picked(doc.GetElement(ref.ElementId))
        if window is None:
            debug_info.append("Ignored pick: " + reason)
            continue
        wid = eid_to_int(window.Id)
        if wid in seen:          # tag and its window both clicked
            continue
        seen.add(wid)
        windows.append(window)
    return windows, False


def arm_rerun():
    """Flip the Boolean wired into IN[0] so the next press of Run re-runs
    this node. Changing a node's value changes the graph for real, so
    Dynamo always re-runs on the next Run - but, unlike marking nodes as
    modified, it doesn't start a run on its own in Manual mode."""
    try:
        clr.AddReference('DynamoRevitDS')
        from Dynamo.Applications import DynamoRevit
        workspace = DynamoRevit.RevitDynamoModel.CurrentWorkspace
    except Exception:
        debug_info.append("Re-run: could not reach the Dynamo workspace: " + traceback.format_exc())
        return
    try:
        for node in workspace.Nodes:
            script = getattr(node, "Script", None)
            if not script or "RENUMBER-BY-CLICK-NODE" not in str(script):
                continue
            # PythonNet can't index Dynamo's port collections, so go via list().
            in_ports = list(node.InPorts)
            connectors = list(in_ports[0].Connectors) if in_ports else []
            if not connectors:
                debug_info.append("Re-run: nothing wired into IN[0] - wire a Boolean node there "
                                  "so Run works more than once.")
                return
            source = connectors[0].Start.Owner
            value = getattr(source, "Value", None)
            if not isinstance(value, bool):
                debug_info.append("Re-run: the node wired into IN[0] isn't a Boolean node.")
                return
            source.Value = not value
            debug_info.append("Re-run: OK - Boolean flipped to {0}; press Run to renumber again.".format(
                not value))
            return
        debug_info.append("Re-run: couldn't find this Python node in the graph.")
    except Exception:
        debug_info.append("Re-run: failed to flip the Boolean: " + traceback.format_exc())


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

renamed_windows = []
changed_count = 0
report_lines = []
status_message = ""

def run_one_renumber(view, form):
    """Pick + renumber once, using the settings in `form`. Returns a short
    status string. Changes are committed before returning, so the next
    round can pick again."""
    global changed_count
    view_windows = collect_view_windows(view)
    prefix = form.prefix_box.Text
    start = form.get_start()
    digits = form.get_digits()
    continue_others = bool(form.continue_radio.Checked)
    show_preview = bool(form.preview_check.Checked)

    picked, cancelled = pick_windows_in_order()
    # Keep the clicked windows highlighted until this round is done.
    highlight([w.Id for w in picked])
    try:
        if cancelled:
            return "Picking cancelled. No changes made."
        if not picked:
            return "No windows were clicked. No changes made."

        # Build the new numbering plan: clicked windows first, then
        # (optionally) the unclicked windows in their current order.
        picked_ints = set(eid_to_int(w.Id) for w in picked)
        others = [w for w in view_windows if eid_to_int(w.Id) not in picked_ints]
        plan = list(picked)
        if continue_others:
            plan += sorted(others, key=lambda w: (natural_key(get_mark(w)), eid_to_int(w.Id)))

        assignments = []   # (window, old, new)
        for i, w in enumerate(plan):
            assignments.append((w, get_mark(w), format_mark(prefix, start + i, digits)))

        # Duplicates against windows we're not renumbering.
        # Same Mark in two DIFFERENT Design Options is intentional
        # and is never reported or changed.
        new_mark_options = {}
        for (w, _, new) in assignments:
            new_mark_options.setdefault(new, []).append(option_key(w))
        planned_ints = set(eid_to_int(a[0].Id) for a in assignments)
        clashes = [w for w in view_windows
                   if eid_to_int(w.Id) not in planned_ints
                   and any(options_clash(option_key(w), k)
                           for k in new_mark_options.get(get_mark(w), []))]

        if show_preview:
            preview = ["{0:<10} -> {1}".format(old or "<blank>", new)
                       for (_, old, new) in assignments]
            summary = "{0} {1}(s) clicked".format(len(picked), MODE["noun"])
            if continue_others:
                summary += ", {0} other(s) numbered after them".format(len(plan) - len(picked))
            body = "\n".join(preview[:40])
            if len(preview) > 40:
                body += "\n... and {0} more".format(len(preview) - 40)
            if clashes:
                body += "\n\nWARNING - these unclicked {0}s already use one of the new Marks ".format(MODE["noun"]) + \
                        "and will become duplicates:\n" + \
                        "\n".join(sorted(set(get_mark(w) for w in clashes), key=natural_key))
            if PreviewForm(summary + ". Apply?", body).ShowDialog() != DialogResult.OK:
                return "Cancelled at preview. No changes made."

        changed_here = 0
        TransactionManager.Instance.EnsureInTransaction(doc)
        for w, old, new in assignments:
            p = w.get_Parameter(BuiltInParameter.ALL_MODEL_MARK)
            if p is None or p.IsReadOnly:
                report_lines.append("SKIPPED {0}: Mark is read-only (id {1})".format(old, eid_to_int(w.Id)))
                continue
            renamed_windows.append(w)
            if old == new:
                report_lines.append("{0} unchanged".format(old))
                continue
            p.Set(new)
            changed_here += 1
            report_lines.append("{0} -> {1}".format(old or "<blank>", new))
        TransactionManager.Instance.TransactionTaskDone()
        changed_count += changed_here

        for w in clashes:
            report_lines.append("DUPLICATE: unclicked {2} id {0} still has Mark {1}".format(
                eid_to_int(w.Id), get_mark(w), MODE["noun"]))

        return "{0} {2} Mark(s) changed{1}.".format(
            changed_here,
            "; {0} duplicate(s) left - see report".format(len(clashes)) if clashes else "",
            MODE["noun"])
    finally:
        highlight([])


try:
    # IN[0]'s value is ignored on purpose: the Boolean wired to it is only a
    # re-run trigger, flipped by arm_rerun() at the end of every run.
    if uidoc is None:
        status_message = "No active Revit document/UI found."
    else:
        view = uidoc.ActiveView
        debug_info.append("Active view: {0}".format(view.Name))
        guesses = {}
        for key in ("windows", "doors"):
            found = collect_view_windows(view, key)
            debug_info.append("{0} in active view: {1}".format(
                "Windows" if key == "windows" else "Doors", len(found)))
            guesses[key] = guess_format(found, MODES[key]["default_prefix"])

        form = SettingsForm(guesses)
        if form.ShowDialog() != DialogResult.OK:
            status_message = "Cancelled by user. No changes made."
        else:
            set_mode(form.mode_key())
            debug_info.append("Renumbering: " + form.mode_key())
            status_message = "Completed: " + run_one_renumber(view, form)

except Exception:
    status_message = "The tool encountered an error and stopped safely. See debug info for details."
    debug_info.append(traceback.format_exc())

# Finished (or cancelled): clear the blue highlight and arm the next Run.
highlight([])
arm_rerun()

OUT = (renamed_windows, changed_count, report_lines, status_message, debug_info)
