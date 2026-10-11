"""The marketplace's Settings tab: plugin-lens/settings.json, as checkboxes."""

import os

import ida_kernwin

from .common import QtCore, QtWidgets, save_settings, set_held, settings, settings_path

# (group, [(setting, text, tooltip)])
OPTIONS = [
    ("Edit › Plugins menu", [
        ("group_menu", "Group the plugins under headers: built-in, hcli (by repository), manual",
         "Off: IDA's own order. The order comes back when IDA restarts."),
        ("show_versions", "Show each plugin's version, tagged (OUT-DATED) or (MANUAL)", ""),
        ("update_action", "Show \"Update plugins (N)…\" when hcli has updates", ""),
        ("hcli_sources", "Ask hcli where each plugin comes from, and for updates (when IDA starts)",
         "Off: hcli is never asked; its plugins are in one group, without updates."),
    ]),
    ("Updates", [
        ("update_notice", "Say in the Output window which plugins have updates, once per session", ""),
    ]),
    ("Marketplace", [
        ("fetch_logos", "Download the logos of plugins that aren't installed, from GitHub",
         "Kept in plugin-lens/logos/; off: nothing is downloaded."),
    ]),
]


class SettingsTab(QtWidgets.QWidget):
    def __init__(self, hooks):
        super().__init__()
        self.hooks = hooks
        self.boxes = {}
        layout = QtWidgets.QVBoxLayout(self)
        for title, options in OPTIONS:
            group = QtWidgets.QGroupBox(title)
            group_layout = QtWidgets.QVBoxLayout(group)
            for key, text, tip in options:
                box = QtWidgets.QCheckBox(text)
                if tip:
                    box.setToolTip(tip)
                box.toggled.connect(lambda checked, key=key: self._changed(key, checked))
                group_layout.addWidget(box)
                self.boxes[key] = box
            layout.addWidget(group)

        held_group = QtWidgets.QGroupBox("Held at their version (not updated)")
        held_layout = QtWidgets.QHBoxLayout(held_group)
        self.held_list = QtWidgets.QListWidget()
        self.held_list.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.held_list.itemSelectionChanged.connect(self._held_selection)
        held_layout.addWidget(self.held_list, 1)
        side = QtWidgets.QVBoxLayout()
        self.allow_button = QtWidgets.QPushButton("Allow updates")
        self.allow_button.clicked.connect(self._allow)
        side.addWidget(self.allow_button)
        hint = QtWidgets.QLabel("Hold a plugin from the Library tab (right-click) or the Browse tab.")
        hint.setWordWrap(True)
        side.addWidget(hint)
        side.addStretch(1)
        held_layout.addLayout(side)
        layout.addWidget(held_group, 1)

        footer = QtWidgets.QHBoxLayout()
        where = QtWidgets.QLabel(f"Saved in {settings_path()}. Your own icons go in the same folder.")
        where.setWordWrap(True)
        where.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        footer.addWidget(where, 1)
        reveal = QtWidgets.QPushButton("Show the folder")
        reveal.clicked.connect(self._reveal)
        footer.addWidget(reveal)
        layout.addLayout(footer)

        hooks.listeners.append(self.reload)
        self.reload()

    def close_tab(self):
        if self.reload in self.hooks.listeners:
            self.hooks.listeners.remove(self.reload)

    def reload(self):
        current = settings()
        for key, box in self.boxes.items():
            box.blockSignals(True)
            box.setChecked(bool(current[key]))
            box.blockSignals(False)
        self.held_list.clear()
        from .menu import _titles  # the names IDA shows

        titles = _titles(self.hooks.status_entries)
        for name in sorted(current["held_plugins"], key=lambda n: titles.get(n, n).casefold()):
            title = titles.get(name, name)
            item = QtWidgets.QListWidgetItem(f"{title} ({name})" if title != name else name)
            item.setData(QtCore.Qt.ItemDataRole.UserRole, name)
            self.held_list.addItem(item)
        self._held_selection()

    def _changed(self, key, checked):
        if not save_settings(**{key: checked}):
            ida_kernwin.warning("Couldn't save plugin-lens/settings.json.")
        self.hooks.reload_settings()

    def _held_selection(self):
        self.allow_button.setEnabled(bool(self.held_list.selectedItems()))

    def _allow(self):
        names = [item.data(QtCore.Qt.ItemDataRole.UserRole) for item in self.held_list.selectedItems()]
        if not all(set_held(name, False) for name in names):
            ida_kernwin.warning("Couldn't save plugin-lens/settings.json.")
        self.hooks.reload_settings()

    def _reveal(self):
        from .library import _reveal  # (library imports this module)

        folder = os.path.dirname(settings_path())
        os.makedirs(folder, exist_ok=True)
        _reveal(folder)
