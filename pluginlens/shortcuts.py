"""Keyboard shortcuts that plugins share, with each other or with IDA."""

import collections
import os
import re

import ida_kernwin

from .common import MENU_PREFIX, QtCore, QtWidgets, is_builtin, plugin_paths

# action name prefixes ("prefix:action") of IDA itself; its built-in plugins'
# prefixes are their file names (golang, objc, ...)
IDA_PREFIXES = {
    "hx", "mv", "cv", "hexview", "navigator", "watch", "cli", "bpts", "strchoose", "commdbg", "msglist",
    "srcdbg", "data_format_t",
}


def _key(text):
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _owner(action, label, builtin_stems, names):
    """The plugin an action belongs to (a name to show), or None for IDA's own.

    names: {_key(name): name} of the installed plugins, to match prefixes
    (or the label, for a plugin's own menu entry under another prefix).
    """
    if action.startswith(MENU_PREFIX):
        return action[len(MENU_PREFIX):]
    if ":" not in action:
        return None
    prefix = action.split(":", 1)[0]
    if prefix in IDA_PREFIXES or prefix.lower() in builtin_stems:
        return None
    key = _key(prefix)
    for name_key, name in names.items():
        if key and (key == name_key or (len(key) >= 4 and (key in name_key or name_key in key))):
            return name
    return names.get(_key(label)) or f"{prefix}: (a plugin)"


def find_conflicts(plugin_names=()):
    """[(shortcut, between_plugins, [(action, label, owner or None)])]: the
    shortcuts used by more than one action, at least one of them a plugin's.

    between_plugins: whether two different plugins use it (else a plugin
    and IDA). The same plugin using one shortcut in several views isn't a
    conflict.
    """
    paths = plugin_paths()
    builtin_stems = {os.path.splitext(os.path.basename(p))[0].lower() for p in paths.values() if is_builtin(p)}
    names = {_key(n): n for n in plugin_names if n}
    by_shortcut = collections.defaultdict(list)
    for action in ida_kernwin.get_registered_actions():
        shortcut = ida_kernwin.get_action_shortcut(action)
        if shortcut:
            by_shortcut[shortcut.replace(" ", "").lower()].append(action)
    result = []
    for actions in by_shortcut.values():
        if len(actions) < 2:
            continue
        rows = []
        for action in actions:
            # without the version Plugin Lens adds to the menu's entries
            label = (ida_kernwin.get_action_label(action) or action).replace("~", "").split("  ")[0]
            rows.append((action, label, _owner(action, label, builtin_stems, names)))
        owners = {o for _, _, o in rows if o}
        if not owners or (len(owners) == 1 and all(o for _, _, o in rows)):
            continue  # IDA's own, or one plugin's actions in several views
        shortcut = ida_kernwin.get_action_shortcut(actions[0])
        result.append((shortcut, len(owners) > 1, sorted(rows, key=lambda r: (r[2] is None, r[2] or "", r[1]))))
    return sorted(result, key=lambda g: (not g[1], g[0].lower()))


class ShortcutsDialog(QtWidgets.QDialog):
    def __init__(self, conflicts, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Shortcuts plugins share")
        self.resize(760, 520)
        layout = QtWidgets.QVBoxLayout(self)
        between = sum(1 for _, b, _ in conflicts if b)
        intro = QtWidgets.QLabel(
            f"{between} shortcut{'s' if between != 1 else ''} used by more than one plugin, and "
            f"{len(conflicts) - between} a plugin shares with IDA. IDA runs one of them: often the one of the "
            "focused view (e.g. pseudocode), so some are harmless. Change one in IDA's shortcut editor.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        tree = QtWidgets.QTreeWidget()
        tree.setHeaderLabels(["Shortcut / action", "Plugin", "Action name"])
        tree.setRootIsDecorated(True)
        for shortcut, between_plugins, rows in conflicts:
            top = QtWidgets.QTreeWidgetItem([shortcut, "two plugins or more" if between_plugins else "a plugin and IDA"])
            font = top.font(0)
            font.setBold(between_plugins)
            top.setFont(0, font)
            top.setFont(1, font)
            for action, label, owner in rows:
                top.addChild(QtWidgets.QTreeWidgetItem([label, owner or "IDA", action]))
            tree.addTopLevelItem(top)
            top.setExpanded(between_plugins)
        tree.header().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        tree.resizeColumnToContents(1)
        layout.addWidget(tree, 1)
        buttons = QtWidgets.QHBoxLayout()
        buttons.addStretch(1)
        editor = QtWidgets.QPushButton("Open IDA's shortcut editor…")
        editor.clicked.connect(self._open_editor)
        buttons.addWidget(editor)
        close = QtWidgets.QPushButton("Close")
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        layout.addLayout(buttons)

    def _open_editor(self):
        self.accept()
        # once this dialog is gone
        QtCore.QTimer.singleShot(0, lambda: ida_kernwin.process_ui_action("ShortcutEditor"))


def show_conflicts(parent, plugin_names):
    conflicts = find_conflicts(plugin_names)
    if not conflicts:
        ida_kernwin.info("No plugin shares a keyboard shortcut with another plugin or with IDA.")
        return
    ShortcutsDialog(conflicts, parent).exec()
