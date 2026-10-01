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
#      window itself. Clicked windows stay highlighted so you can see which
#      ones you've done. The Revit status bar (bottom-left) shows the Mark
#      the next click will receive.
#        - Click an already-clicked window again to remove it from the list
#          (every window after it moves up one number).
#        - Press Esc (or right-click > Cancel) when you're finished.
#   3. Shows a preview ("W.06 -> W.01", ...) and asks for confirmation.
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
# picking session restarts every time the graph is re-evaluated.
#
# IN[0] (optional): Boolean "Run" toggle. Defaults to True if not wired.
#
# OUT = (renamed_windows, changed_count, report_lines, status_message,
#        debug_info)
# ============================================================================

import clr
import re
import traceback

clr.AddReference('RevitAPI')
clr.AddReference('RevitAPIUI')
clr.AddReference('RevitServices')
clr.AddReference('System.Windows.Forms')
clr.AddReference('System.Drawing')

from Autodesk.Revit.DB import (
    FilteredElementCollector, BuiltInCategory, BuiltInParameter, ElementId,
    FamilyInstance, IndependentTag
)
from Autodesk.Revit.UI import TaskDialog, TaskDialogCommonButtons, TaskDialogResult
from Autodesk.Revit.UI.Selection import ObjectType
from Autodesk.Revit.Exceptions import OperationCanceledException
from RevitServices.Persistence import DocumentManager
from RevitServices.Transactions import TransactionManager

from System import Decimal
from System.Collections.Generic import List as NetList
from System.Windows.Forms import (
    Form, Label, TextBox, NumericUpDown, Button, RadioButton, GroupBox,
    DialogResult, FormStartPosition, FormBorderStyle
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
        self.ClientSize = Size(420, 330)

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
        self.start_box = NumericUpDown()
        self.start_box.Minimum = Decimal(0)
        self.start_box.Maximum = Decimal(99999)
        self.start_box.Value = Decimal(1)
        self.start_box.Location = Point(150, y)
        self.start_box.Size = Size(100, 22)
        self.Controls.Add(self.start_box)

        y += 35
        lbl = Label()
        lbl.Text = "Digits (zero padding):"
        lbl.Location = Point(15, y + 3)
        lbl.Size = Size(130, 20)
        self.Controls.Add(lbl)
        self.digits_box = NumericUpDown()
        self.digits_box.Minimum = Decimal(1)
        self.digits_box.Maximum = Decimal(6)
        self.digits_box.Value = Decimal(max(1, min(6, int(digits))))
        self.digits_box.Location = Point(150, y)
        self.digits_box.Size = Size(100, 22)
        self.Controls.Add(self.digits_box)

        self.example = Label()
        self.example.Location = Point(265, y - 32)
        self.example.Size = Size(140, 40)
        self.Controls.Add(self.example)
        self.prefix_box.TextChanged += self.update_example
        self.start_box.ValueChanged += self.update_example
        self.digits_box.ValueChanged += self.update_example
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
        hint.Text = ("Next: click window tags (or windows) in order.\n"
                     "Click one again to remove it. Press Esc when done.")
        hint.Location = Point(15, y)
        hint.Size = Size(390, 36)
        self.Controls.Add(hint)

        ok = Button()
        ok.Text = "Start Picking"
        ok.Location = Point(210, 290)
        ok.Size = Size(100, 28)
        ok.DialogResult = DialogResult.OK
        self.Controls.Add(ok)
        self.AcceptButton = ok

        cancel = Button()
        cancel.Text = "Cancel"
        cancel.Location = Point(315, 290)
        cancel.Size = Size(90, 28)
        cancel.DialogResult = DialogResult.Cancel
        self.Controls.Add(cancel)
        self.CancelButton = cancel

    def update_example(self, sender, args):
        start = Decimal.ToInt32(self.start_box.Value)
        digits = Decimal.ToInt32(self.digits_box.Value)
        self.example.Text = "e.g. {0}, {1}, ...".format(
            format_mark(self.prefix_box.Text, start, digits),
            format_mark(self.prefix_box.Text, start + 1, digits))


# ----------------------------------------------------------------------------
# Picking
# ----------------------------------------------------------------------------

def highlight(ids):
    try:
        uidoc.Selection.SetElementIds(NetList[ElementId](ids))
    except Exception:
        pass


def pick_windows_in_order(prefix, start, digits):
    """Returns the list of picked window ElementIds (as ints) in click order."""
    order = []          # ints, in click order
    by_int = {}         # int -> window element
    last_msg = ""
    highlight([])
    while True:
        next_mark = format_mark(prefix, start + len(order), digits)
        prompt = "[{0} picked] Click window/tag for {1}  -  click again to remove  -  Esc to finish".format(
            len(order), next_mark)
        if last_msg:
            prompt = last_msg + "  |  " + prompt
        try:
            ref = uidoc.Selection.PickObject(ObjectType.Element, prompt)
        except OperationCanceledException:
            break
        picked = doc.GetElement(ref.ElementId)
        window, reason = window_from_picked(picked)
        if window is None:
            last_msg = "Ignored: " + reason
            continue
        wid = eid_to_int(window.Id)
        if wid in by_int:
            pos = order.index(wid)
            order.remove(wid)
            del by_int[wid]
            last_msg = "Removed {0} (was #{1})".format(get_mark(window) or "<no mark>", pos + 1)
        else:
            order.append(wid)
            by_int[wid] = window
            last_msg = "{0} -> {1}".format(get_mark(window) or "<no mark>", next_mark)
        highlight([by_int[i].Id for i in order])
    return [by_int[i] for i in order]


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
            start = Decimal.ToInt32(form.start_box.Value)
            digits = Decimal.ToInt32(form.digits_box.Value)
            continue_others = bool(form.continue_radio.Checked)

            picked = pick_windows_in_order(prefix, start, digits)

            if not picked:
                status_message = "No windows were clicked. No changes made."
                highlight([])
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

                td = TaskDialog("Renumber Window Marks")
                td.MainInstruction = summary + ". Apply?"
                td.MainContent = body
                td.CommonButtons = TaskDialogCommonButtons.Ok | TaskDialogCommonButtons.Cancel
                td.DefaultButton = TaskDialogResult.Ok
                if td.Show() != TaskDialogResult.Ok:
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

                    status_message = "{0} window Mark(s) changed{1}.".format(
                        changed_count,
                        "; {0} duplicate(s) left - see report".format(len(clashes)) if clashes else "")

except Exception:
    status_message = "The tool encountered an error and stopped safely. See debug info for details."
    debug_info.append(traceback.format_exc())

OUT = (renamed_windows, changed_count, report_lines, status_message, debug_info)
