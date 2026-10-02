# ============================================================================
# Revit Window Mark Renumber - by clicking in order
# ----------------------------------------------------------------------------
# Paste this entire file into a single Dynamo "Python Script" node.
#
# Engine: CPython3 / PythonNet3 (Dynamo 2.13+ / Revit 2022+). Also runs
# unmodified on the legacy IronPython2 engine.
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
# picking session restarts every time the graph is re-evaluated. At the end
# the script flags the graph as changed, so pressing Run again starts a new
# renumber straight away.
#
# IN[0] (optional): Boolean "Run" toggle. Defaults to True if not wired.
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

WINDOWS_CAT_ID = ElementId(BuiltInCategory.OST_Windows)
MARK_RE = re.compile(r'^(.*?)(\d+)$')

SCRIPT_VERSION = "v5 (green tick to finish, re-runnable)"

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
    try:
        return (isinstance(element, FamilyInstance)
                and element.Category is not None
                and element.Category.Id.Equals(WINDOWS_CAT_ID))
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
        return None, "that tag isn't tagging a window in this model (linked windows can't be renumbered)"
    cat = element.Category.Name if element.Category is not None else "element"
    return None, "that's a {0}, not a window or window tag".format(cat)


def collect_view_windows(view):
    return [w for w in FilteredElementCollector(doc, view.Id)
            .OfCategory(BuiltInCategory.OST_Windows)
            .WhereElementIsNotElementType()
            .ToElements() if is_window(w)]


def guess_format(windows):
    """Guess (prefix, digits) from the most common existing Mark pattern."""
    counts = {}
    for w in windows:
        m = MARK_RE.match(get_mark(w))
        if m:
            key = (m.group(1), len(m.group(2)))
            counts[key] = counts.get(key, 0) + 1
    if not counts:
        return "W.", 2
    return max(counts.items(), key=lambda kv: kv[1])[0]


# ----------------------------------------------------------------------------
# Settings dialog
# ----------------------------------------------------------------------------

class SettingsForm(Form):
    def __init__(self, prefix, digits):
        Form.__init__(self)
        self.Text = "Renumber Window Marks by Clicking"
        self.FormBorderStyle = FormBorderStyle.FixedDialog
        self.StartPosition = FormStartPosition.CenterScreen
        self.MaximizeBox = False
        self.MinimizeBox = False
        self.TopMost = True
        self.ClientSize = Size(420, 370)

        y = 15
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
        grp = GroupBox()
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
        hint = Label()
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
        ok.Location = Point(210, 330)
        ok.Size = Size(100, 28)
        ok.DialogResult = DialogResult.OK
        self.Controls.Add(ok)
        self.AcceptButton = ok

        cancel = Button()
        cancel.Text = "Cancel"
        cancel.Location = Point(315, 330)
        cancel.Size = Size(90, 28)
        cancel.DialogResult = DialogResult.Cancel
        self.Controls.Add(cancel)
        self.CancelButton = cancel

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
    prompt = ("Click window tags (or windows) one at a time in order, "
              "then click Finish (green tick) on the Options Bar")
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
    for ref in refs:
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


def mark_graph_for_rerun():
    """Flag this graph's nodes as modified so pressing Run in Dynamo
    (Manual mode) runs the script again, even though nothing changed."""
    try:
        clr.AddReference('DynamoRevitDS')
        from Dynamo.Applications import DynamoRevit
        workspace = DynamoRevit.RevitDynamoModel.CurrentWorkspace
        # In Automatic mode this would start the script again straight away.
        if "Manual" not in str(workspace.RunSettings.RunType):
            return False
        for node in workspace.Nodes:
            node.MarkNodeAsModified(True)
        return True
    except Exception:
        debug_info.append("Could not flag graph for re-run: " + traceback.format_exc())
        return False


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

renamed_windows = []
changed_count = 0
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
    elif uidoc is None:
        status_message = "No active Revit document/UI found."
    else:
        view = uidoc.ActiveView
        view_windows = collect_view_windows(view)
        debug_info.append("Active view: {0}".format(view.Name))
        debug_info.append("Windows visible in active view: {0}".format(len(view_windows)))

        guess_prefix, guess_digits = guess_format(view_windows)
        form = SettingsForm(guess_prefix, guess_digits)
        if form.ShowDialog() != DialogResult.OK:
            status_message = "Cancelled by user. No changes made."
        else:
            prefix = form.prefix_box.Text
            start = form.get_start()
            digits = form.get_digits()
            continue_others = bool(form.continue_radio.Checked)

            show_preview = bool(form.preview_check.Checked)

            picked, cancelled = pick_windows_in_order()
            # Keep the clicked windows highlighted until the script ends.
            highlight([w.Id for w in picked])

            if cancelled:
                status_message = "Picking cancelled. No changes made."
            elif not picked:
                status_message = "No windows were clicked. No changes made."
            else:
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

                # Duplicates against windows we're not renumbering
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

                preview = ["{0:<10} -> {1}".format(old or "<blank>", new)
                           for (_, old, new) in assignments]
                summary = "{0} window(s) clicked".format(len(picked))
                if continue_others:
                    summary += ", {0} other(s) numbered after them".format(len(plan) - len(picked))
                body = "\n".join(preview[:40])
                if len(preview) > 40:
                    body += "\n... and {0} more".format(len(preview) - 40)
                if clashes:
                    body += "\n\nWARNING - these unclicked windows already use one of the new Marks " \
                            "and will become duplicates:\n" + \
                            "\n".join(sorted(set(get_mark(w) for w in clashes), key=natural_key))

                if show_preview and PreviewForm(summary + ". Apply?", body).ShowDialog() != DialogResult.OK:
                    status_message = "Cancelled at preview. No changes made."
                else:
                    TransactionManager.Instance.EnsureInTransaction(doc)
                    for w, old, new in assignments:
                        p = w.get_Parameter(BuiltInParameter.ALL_MODEL_MARK)
                        if p is None or p.IsReadOnly:
                            report_lines.append("SKIPPED {0}: Mark is read-only (id {1})".format(old, eid_to_int(w.Id)))
                            continue
                        if old == new:
                            report_lines.append("{0} unchanged".format(old))
                            renamed_windows.append(w)
                            continue
                        p.Set(new)
                        renamed_windows.append(w)
                        changed_count += 1
                        report_lines.append("{0} -> {1}".format(old or "<blank>", new))
                    TransactionManager.Instance.TransactionTaskDone()

                    for w in clashes:
                        report_lines.append("DUPLICATE: unclicked window id {0} still has Mark {1}".format(
                            eid_to_int(w.Id), get_mark(w)))

                    status_message = "Completed: {0} window Mark(s) changed{1}.".format(
                        changed_count,
                        "; {0} duplicate(s) left - see report".format(len(clashes)) if clashes else "")

except Exception:
    status_message = "The tool encountered an error and stopped safely. See debug info for details."
    debug_info.append(traceback.format_exc())

# Finished (or cancelled): clear the blue highlight and get ready for the
# next Run.
highlight([])
mark_graph_for_rerun()

OUT = (renamed_windows, changed_count, report_lines, status_message, debug_info)
