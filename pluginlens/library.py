"""The plugin marketplace window: Browse (hcli's repositories) and Library (every installed plugin)."""

import os
import platform
import subprocess
import sys
from dataclasses import dataclass, field

import ida_diskio
import ida_kernwin

from . import disable, operations, plugin_list, plugin_settings, shortcuts
from .browse import BrowseTab, Catalog
from .settings_tab import SettingsTab
from .common import (
    BUILTIN, HCLI, HCLI_FILE, ICON_SIZE, MANUAL, PLUGIN_NAME, QtCore, QtGui, QtWidgets,
    badged_png, group_order, hcli_folders, hcli_repo_group, held, icon_image, icon_note, is_builtin, metadata,
    plugin_folder, plugin_paths, set_held,
)

TITLE = "Plugin marketplace"
UNINSTALLED = "Uninstalled"  # the source shown for plugins removed this session
COLUMNS = ["Plugin", "Version", "Source", "Status", "Location"]
def _from_repository(source):
    return source.startswith("hcli · ") and source.endswith(" repository")


SOURCE_FILTERS = [  # (text, test of a Row)
    ("All plugins", None),
    ("Built-in", lambda r: r.source == BUILTIN),
    ("Installed with hcli, from a repository", lambda r: _from_repository(r.source)),
    ("Installed with hcli, from a file", lambda r: r.source == HCLI_FILE),
    ("Installed manually", lambda r: r.source == MANUAL),
    ("Not loaded by IDA", lambda r: bool(r.problem)),
    ("Held at their version", lambda r: r.held),
    ("Disabled", lambda r: r.disabled_entry is not None),
]
ROW_ROLE = QtCore.Qt.ItemDataRole.UserRole
SORT_ROLE = QtCore.Qt.ItemDataRole.UserRole + 1


@dataclass
class Row:
    name: str  # as IDA shows it (Edit > Plugins), else hcli's name
    source: str  # BUILTIN, an hcli group, HCLI_FILE, HCLI or MANUAL
    location: str
    version: str = ""
    hcli_name: str = None  # set for plugins hcli manages
    latest: str = None  # a newer version hcli can install
    status: str = ""
    names: list = field(default_factory=list)  # all the plugin's entries in IDA
    plugin_file: str = None  # one of its files IDA loaded, for the icon
    held: bool = False  # the user holds it at its version
    problem: str = None  # why IDA didn't load it, if it didn't
    disabled_entry: str = None  # its name in plugin-lens/disabled/, if disabled

    @property
    def can_disable(self):
        return (self.disabled_entry is None and self.source not in (BUILTIN, UNINSTALLED)
                and self.hcli_name != "ida-plugin-lens" and disable.can_disable(self.location))

    @property
    def can_update(self):
        return bool(self.hcli_name and self.latest and not self.held)

    @property
    def can_hold(self):
        return bool(self.hcli_name) and _from_repository(self.source) and self.disabled_entry is None

    @property
    def can_uninstall(self):
        return bool(self.hcli_name) and self.disabled_entry is None


def _current_platform():
    """As in ida-plugin.json: macos-aarch64, linux-x86_64, windows-x86_64, ..."""
    system = {"darwin": "macos", "win32": "windows"}.get(sys.platform, "linux")
    machine = platform.machine().lower()
    return f"{system}-{'aarch64' if machine in ('arm64', 'aarch64') else 'x86_64'}"


def not_loaded_reason(folder):
    """Why IDA may not have loaded the plugin in folder, as far as its
    ida-plugin.json tells."""
    md = metadata(folder)
    platforms = md.get("platforms")
    if isinstance(platforms, list) and platforms and _current_platform() not in platforms:
        return f"It isn't made for this platform ({_current_platform()}): it's for {', '.join(platforms)}."
    entry = md.get("entryPoint")
    if isinstance(entry, str) and entry:
        path = os.path.join(folder, entry)
        if not os.path.splitext(entry)[1]:  # a native plugin: IDA adds the extension
            path += {"darwin": ".dylib", "win32": ".dll"}.get(sys.platform, ".so")
        if not os.path.exists(path):
            return f"Its entry point, {os.path.relpath(path, folder)}, is missing."
    return ("IDA didn't load it with this database. It may not apply to this file (some plugins load only "
            "for certain formats or processors), or it failed when IDA started: see the Output window.")


def library_rows(entries, paths=None):
    """The rows for every installed plugin.

    entries: hcli's status (hcli.status()), or None if unknown; paths: IDA's
    plugins (plugin_paths()).
    """
    paths = plugin_paths() if paths is None else paths
    user_plugins = os.path.join(ida_diskio.get_user_idadir(), "plugins")
    folders = hcli_folders()  # hcli name -> folder

    # IDA's entries, by the plugin folder holding them
    by_folder, manual, builtin = {}, [], []
    for menu_name, path in sorted(paths.items(), key=lambda kv: kv[0].casefold()):
        if is_builtin(path):
            builtin.append((menu_name, path))
            continue
        folder = plugin_folder(path)
        if folder:
            by_folder.setdefault(os.path.realpath(folder), []).append((menu_name, path))
        else:
            manual.append((menu_name, path))

    rows = []
    hcli_known = set()
    held_names = held()
    for entry in entries or []:
        kind, name = entry.get("kind"), entry.get("name")
        if kind not in ("installed", "incompatible") or not name:
            continue
        hcli_known.add(name)
        folder = folders.get(name, "")
        in_ida = by_folder.get(os.path.realpath(folder), []) if folder else []
        if kind == "incompatible":
            source, status = HCLI, "Not compatible with this hcli"
        elif entry.get("in_repository") and entry.get("repo"):
            source = hcli_repo_group(entry["repo"])
            if name in held_names:
                status = f"Held · {entry['upgradable_to']} available" if entry.get("upgradable_to") else "Held"
            else:
                status = f"Update to {entry['upgradable_to']}" if entry.get("upgradable_to") else "Up to date"
        else:
            source, status = HCLI_FILE, ""
        changed = operations.changed_this_session.get(name)
        problem = None
        if changed == operations.UPGRADE:
            status = "Updated · restart IDA to load it"
        elif changed == operations.INSTALL:
            status = "Installed · restart IDA to load it"
        elif kind == "installed" and folder and not in_ida:
            problem = not_loaded_reason(folder)
            status = " · ".join(filter(None, ["Not loaded", status]))
        rows.append(Row(
            name=in_ida[0][0] if in_ida else name,
            source=source,
            location=folder,
            version=entry.get("version") or "",
            hcli_name=name,
            latest=entry.get("upgradable_to") if kind == "installed" and entry.get("repo") else None,
            status=status,
            names=[n for n, _ in in_ida],
            plugin_file=in_ida[0][1] if in_ida else None,
            held=name in held_names and source != HCLI_FILE,
            problem=problem,
        ))

    # hcli's folders it didn't report (e.g. hcli couldn't be asked)
    for name, folder in folders.items():
        if name in hcli_known:
            continue
        in_ida = by_folder.get(os.path.realpath(folder), [])
        rows.append(Row(
            name=in_ida[0][0] if in_ida else name,
            source=HCLI,
            location=folder,
            version=metadata(folder).get("version") or "",
            hcli_name=name if entries is not None else None,
            names=[n for n, _ in in_ida],
            plugin_file=in_ida[0][1] if in_ida else None,
        ))

    # plugins disabled: moved out of plugins/
    off = disable.disabled()
    off_from = [info.get("from") or "" for info in off.values()]
    for entry, info in sorted(off.items(), key=lambda kv: (kv[1].get("name") or kv[0]).casefold()):
        unload = disable.changed_this_session.get(info.get("from")) is True
        rows.append(Row(
            name=info.get("name") or entry, source=info.get("source") or MANUAL,
            location=os.path.join(disable.disabled_dir(), entry), hcli_name=info.get("hcli_name"),
            status="Disabled · restart IDA to unload it" if unload else "Disabled", disabled_entry=entry,
        ))

    for menu_name, path in manual:
        if any(f and (path == f or path.startswith(f + os.sep)) for f in off_from):
            continue  # disabled this session, still loaded: it has its row above
        if not os.path.exists(path):  # IDA still runs it, but it was removed (e.g. uninstalled)
            rows.append(Row(name=menu_name, source=UNINSTALLED, location=path, names=[menu_name],
                            status="Restart IDA to unload it"))
            continue
        rows.append(Row(name=menu_name, source=MANUAL, location=path, names=[menu_name], plugin_file=path))
    for menu_name, path in builtin:
        rows.append(Row(name=menu_name, source=BUILTIN, location=path, names=[menu_name], plugin_file=path))

    # enabled this session: loaded when IDA starts
    for row in rows:
        if disable.changed_this_session.get(os.path.abspath(row.location or "")) is False:
            row.problem, row.status = None, "Enabled · restart IDA to load it"

    # legacy single-file plugins hcli found that IDA didn't load
    loaded = {os.path.realpath(path) for _, path in manual}
    for entry in entries or []:
        if entry.get("kind") == "legacy" and entry.get("path"):
            path = os.path.join(user_plugins, entry["path"])
            if os.path.realpath(path) not in loaded:
                rows.append(Row(name=entry["name"], source=MANUAL, location=path, status="Not loaded by IDA"))
    return rows


class _Filter(QtCore.QSortFilterProxyModel):
    def __init__(self):
        super().__init__()
        self.text = ""
        self.source_test = None
        self.updates_only = False

    def filterAcceptsRow(self, source_row, source_parent):
        row = self.sourceModel().index(source_row, 0, source_parent).data(ROW_ROLE)
        if row is None:
            return False
        if self.updates_only and not row.can_update:
            return False
        if self.source_test is not None and not self.source_test(row):
            return False
        if self.text:
            haystack = " ".join([row.name, row.hcli_name or "", row.source, row.location, *row.names]).casefold()
            if self.text.casefold() not in haystack:
                return False
        return True


def _titles(rows):
    """{hcli name: the name IDA shows}, for the rows hcli manages."""
    return {row.hcli_name: row.name for row in rows if row.hcli_name}


def _reveal(path):
    """Show path in the file manager."""
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path])
    elif sys.platform == "win32":
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
    else:
        subprocess.Popen(["xdg-open", os.path.dirname(path) if os.path.isfile(path) else path])


class MarketplaceForm(ida_kernwin.PluginForm):
    """A dockable window with three tabs: Browse, Library and Settings."""

    instance = None

    def __init__(self, hooks):
        super().__init__()
        self.hooks = hooks
        self.icons = {}  # image path -> QIcon
        blank = QtGui.QPixmap(ICON_SIZE, ICON_SIZE)  # as big as the icons, scaled alike
        blank.fill(QtCore.Qt.GlobalColor.transparent)
        self.blank = QtGui.QIcon(blank)

    @classmethod
    def open(cls, hooks):
        if cls.instance is not None and ida_kernwin.find_widget(TITLE):
            ida_kernwin.activate_widget(ida_kernwin.find_widget(TITLE), True)
            return
        cls.instance = cls(hooks)
        cls.instance.Show(TITLE, options=ida_kernwin.PluginForm.WOPN_TAB | ida_kernwin.PluginForm.WOPN_RESTORE)

    # -- building ----------------------------------------------------------
    def OnCreate(self, form):
        parent = self.FormToPyQtWidget(form)
        layout = QtWidgets.QVBoxLayout(parent)
        layout.setContentsMargins(6, 6, 6, 6)
        tabs = QtWidgets.QTabWidget()
        self.browse = BrowseTab(self.hooks)
        tabs.addTab(self.browse, "Browse")
        tabs.addTab(self._library_tab(), "Library")
        self.settings_tab = SettingsTab(self.hooks)
        tabs.addTab(self.settings_tab, "Settings")
        layout.addWidget(tabs)
        self.hooks.listeners.append(self.reload)
        self.reload()
        if self.hooks.status_entries is None:
            self.hooks.refresh_status()  # hcli's full status isn't known yet

    def _library_tab(self):
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)

        bar = QtWidgets.QHBoxLayout()
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("Search")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter_changed)
        self.search.setMinimumWidth(140)
        bar.addWidget(self.search, 2)
        self.source_box = QtWidgets.QComboBox()
        for text, _ in SOURCE_FILTERS:
            self.source_box.addItem(text)
        self.source_box.currentIndexChanged.connect(self._filter_changed)
        self.source_box.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.source_box.setMinimumContentsLength(12)
        bar.addWidget(self.source_box, 1)
        self.updates_box = QtWidgets.QCheckBox("Updates only")
        self.updates_box.toggled.connect(self._filter_changed)
        bar.addWidget(self.updates_box)
        refresh = QtWidgets.QPushButton("Refresh")
        refresh.setToolTip("Ask hcli again for the installed plugins and their updates")
        refresh.clicked.connect(self._refresh)
        bar.addWidget(refresh)
        export = QtWidgets.QPushButton("Export list…")
        export.setToolTip("Save the installed plugins to a file, to install them elsewhere with Import list…")
        export.clicked.connect(lambda: plugin_list.export_list(
            self.table, self.hooks.status_entries, self.rows, Catalog.data))
        bar.addWidget(export)
        import_button = QtWidgets.QPushButton("Import list…")
        import_button.setToolTip("Install the plugins of a list saved with Export list…")
        import_button.clicked.connect(lambda: plugin_list.import_list(
            self.table, self.hooks.status_entries, Catalog.data, self.hooks.set_status))
        bar.addWidget(import_button)
        shortcuts_button = QtWidgets.QPushButton("Shortcuts…")
        shortcuts_button.setToolTip("Keyboard shortcuts that plugins share with each other or with IDA")
        shortcuts_button.clicked.connect(lambda: shortcuts.show_conflicts(self.table, self._plugin_names()))
        bar.addWidget(shortcuts_button)
        layout.addLayout(bar)

        self.model = QtGui.QStandardItemModel(0, len(COLUMNS))
        self.model.setHorizontalHeaderLabels(COLUMNS)
        self.proxy = _Filter()
        self.proxy.setSourceModel(self.model)
        self.proxy.setSortRole(SORT_ROLE)
        self.table = QtWidgets.QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setIconSize(QtCore.QSize(18, 18))
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.selectionModel().selectionChanged.connect(self._selection_changed)
        self.table.doubleClicked.connect(lambda _: self._reveal())
        self.table.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        layout.addWidget(self.table, 1)

        buttons = QtWidgets.QHBoxLayout()
        self.summary = QtWidgets.QLabel()
        buttons.addWidget(self.summary, 1)
        self.update_button = QtWidgets.QPushButton("Update")
        self.update_button.clicked.connect(self._update_selected)
        self.update_all_button = QtWidgets.QPushButton("Update all")
        self.update_all_button.clicked.connect(self._update_all)
        self.uninstall_button = QtWidgets.QPushButton("Uninstall")
        self.uninstall_button.clicked.connect(self._uninstall)
        self.reveal_button = QtWidgets.QPushButton("Show in Finder" if sys.platform == "darwin" else "Show in folder")
        self.reveal_button.clicked.connect(self._reveal)
        for button in (self.update_button, self.update_all_button, self.uninstall_button, self.reveal_button):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        return tab

    def _icon(self, row):
        image = icon_image(row.name, row.plugin_file)
        if not image:
            return None
        if image not in self.icons:
            data = badged_png(image)  # badged if it isn't the plugin's own
            pixmap = QtGui.QPixmap()
            self.icons[image] = QtGui.QIcon(pixmap) if data and pixmap.loadFromData(data) else None
        return self.icons[image]

    # -- data --------------------------------------------------------------
    def reload(self):
        """Rebuild the rows from IDA's plugins and hcli's last status."""
        try:
            self.rows = library_rows(self.hooks.status_entries)
        except Exception as e:  # never break IDA over the window
            ida_kernwin.msg(f"[{PLUGIN_NAME}] library: {type(e).__name__}: {e}\n")
            self.rows = []
        self.table.setSortingEnabled(False)
        self.model.removeRows(0, self.model.rowCount())
        bold = QtGui.QFont()
        bold.setBold(True)
        for row in self.rows:
            items = []
            for column, text in enumerate([row.name, row.version, row.source, row.status, row.location]):
                item = QtGui.QStandardItem(text)
                item.setData(text.casefold(), SORT_ROLE)
                items.append(item)
            items[0].setData(row, ROW_ROLE)
            icon = self._icon(row)
            items[0].setIcon(icon if icon is not None else self.blank)  # blank: names aligned
            tip = [row.name]
            if row.hcli_name and row.hcli_name != row.name:
                tip.append(f"hcli: {row.hcli_name}")
            note = icon_note(icon_image(row.name, row.plugin_file))
            if note:
                tip.append(f"Icon: {note}")
            if len(tip) > 1:
                items[0].setToolTip("\n".join(tip))
            # by source as the menu groups them, then by name
            items[2].setData(f"{group_order(row.source)}|{row.name.casefold()}", SORT_ROLE)
            if row.can_update:
                items[3].setFont(bold)
            if row.problem:
                items[3].setToolTip(row.problem)
            self.model.appendRow(items)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(2, QtCore.Qt.SortOrder.AscendingOrder)
        self.table.resizeColumnsToContents()
        self._update_summary()
        self._selection_changed()

    def _refresh(self):
        self.summary.setText("Asking hcli…")
        self.hooks.refresh_status()

    def _update_summary(self):
        updates = sum(row.can_update for row in self.rows)
        hcli_note = "" if self.hooks.status_entries is not None else " · hcli's information not available yet"
        shown = self.proxy.rowCount()
        count = f"{len(self.rows)}" if shown == len(self.rows) else f"{shown} of {len(self.rows)}"
        self.summary.setText(f"{count} plugins · {updates} update{'s' if updates != 1 else ''}{hcli_note}")
        self.update_all_button.setText(f"Update all ({updates})" if updates else "Update all")
        self.update_all_button.setEnabled(bool(updates))

    def _filter_changed(self, *_):
        self.proxy.text = self.search.text().strip()
        self.proxy.source_test = SOURCE_FILTERS[self.source_box.currentIndex()][1]
        self.proxy.updates_only = self.updates_box.isChecked()
        self.proxy.invalidateFilter()
        self._update_summary()

    def _selected(self):
        rows = []
        for index in self.table.selectionModel().selectedRows(0):
            row = index.data(ROW_ROLE)
            if row is not None:
                rows.append(row)
        return rows

    def _selection_changed(self, *_):
        selected = self._selected()
        self.update_button.setEnabled(any(row.can_update for row in selected))
        self.uninstall_button.setEnabled(any(row.can_uninstall for row in selected))
        self.reveal_button.setEnabled(len(selected) == 1 and os.path.exists(selected[0].location))

    # -- actions -------------------------------------------------------------
    def _update_selected(self):
        self._update([row for row in self._selected() if row.can_update])

    def _update_all(self):
        self._update([row for row in self.rows if row.can_update])

    def _update(self, rows):
        if rows:
            operations.perform([(operations.UPGRADE, row.hcli_name) for row in rows],
                               on_status=self.hooks.set_status, titles=_titles(rows))

    def _uninstall(self):
        rows = [row for row in self._selected() if row.can_uninstall]
        if not rows:
            return
        names = "\n".join(f"  {row.name}" + (f" ({row.hcli_name})" if row.hcli_name != row.name else "") for row in rows)
        warning = ""
        if any(row.hcli_name == "ida-plugin-lens" for row in rows):
            warning = "\n\nThis includes Plugin Lens itself: this window and the menu changes go away after the restart."
        question = f"Uninstall with hcli:\n{names}{warning}\n\nThe files are removed now; IDA restarts afterwards."
        if ida_kernwin.ask_yn(ida_kernwin.ASKBTN_NO, question) != ida_kernwin.ASKBTN_YES:
            return
        operations.perform([(operations.UNINSTALL, row.hcli_name) for row in rows],
                           on_status=self.hooks.set_status, titles=_titles(rows))

    def _plugin_names(self):
        """The installed plugins' names (IDA's, hcli's, their folders'), to tell
        whose a shortcut is."""
        names = []
        for row in self.rows:
            if row.source == BUILTIN:
                continue
            names += [row.name, row.hcli_name or "", os.path.basename(row.location.rstrip("/"))]
        return [n for n in names if n]

    def _context_menu(self, position):
        selected = self._selected()
        if not selected:
            return
        menu = QtWidgets.QMenu(self.table)
        updatable = [row for row in selected if row.can_update]
        if updatable:
            menu.addAction("Update", lambda: self._update(updatable))
        holdable = [row for row in selected if row.can_hold]
        if holdable and all(row.held for row in holdable):
            menu.addAction("Allow updates", lambda: self._hold(holdable, False))
        elif holdable:
            menu.addAction("Hold at this version" if len(holdable) == 1 else "Hold at these versions",
                           lambda: self._hold([row for row in holdable if not row.held], True))
        if (len(selected) == 1 and selected[0].hcli_name and not selected[0].disabled_entry
                and plugin_settings.declared(selected[0].location)):
            menu.addAction("Settings…", lambda: self._plugin_settings(selected[0]))
        if any(row.can_uninstall for row in selected):
            menu.addAction("Uninstall…", self._uninstall)
        if any(row.can_disable for row in selected):
            menu.addAction("Disable…", lambda: self._disable([r for r in selected if r.can_disable], True))
        if any(row.disabled_entry for row in selected):
            menu.addAction("Enable…", lambda: self._disable([r for r in selected if r.disabled_entry], False))
        if len(selected) == 1 and os.path.exists(selected[0].location):
            menu.addSeparator()
            menu.addAction(self.reveal_button.text(), self._reveal)
        if menu.actions():
            menu.exec(self.table.viewport().mapToGlobal(position))

    def _disable(self, rows, off):
        """Disable (move out of plugins/) or enable the plugins, then offer to restart."""
        names = "\n".join(f"  {row.name}" for row in rows)
        if off:
            question = (f"Disable:\n{names}\n\nIDA won't load them from its next start. They stay installed, "
                        "in plugin-lens/disabled/, but can't be updated or uninstalled until you enable them.")
            if ida_kernwin.ask_yn(ida_kernwin.ASKBTN_NO, question) != ida_kernwin.ASKBTN_YES:
                return
        errors = []
        for row in rows:
            error = (disable.disable(row.location, row.name, row.hcli_name, row.source) if off
                     else disable.enable(row.disabled_entry))
            if error:
                errors.append(f"{row.name}: {error}")
        self.hooks.refresh_status()  # hcli's plugins changed
        self.reload()
        if errors:
            ida_kernwin.warning("\n\n".join(errors))
        if len(errors) < len(rows):
            done = "disabled" if off else "enabled"
            if ida_kernwin.ask_yn(ida_kernwin.ASKBTN_NO,
                                  f"Plugins {done}: IDA {'un' if off else ''}loads them when it starts.\n\n"
                                  "Restart IDA now?") == ida_kernwin.ASKBTN_YES:
                from .restart import restart_ida

                restart_ida()

    def _plugin_settings(self, row):
        if plugin_settings.edit_settings(self.table, row.name, row.hcli_name, row.location):
            if ida_kernwin.ask_yn(ida_kernwin.ASKBTN_NO, f"{row.name}'s settings are saved. The plugin reads them "
                                  "when it loads.\n\nRestart IDA now?") == ida_kernwin.ASKBTN_YES:
                from .restart import restart_ida

                restart_ida()

    def _hold(self, rows, hold):
        """Hold the plugins at their versions (no updates), or allow updates again."""
        if not all(set_held(row.hcli_name, hold) for row in rows):
            ida_kernwin.warning("Couldn't save plugin-lens/settings.json.")
        self.hooks.reload_settings()

    def _reveal(self):
        selected = self._selected()
        if len(selected) == 1 and os.path.exists(selected[0].location):
            _reveal(selected[0].location)

    def OnClose(self, form):
        self.browse.close_tab()
        self.settings_tab.close_tab()
        if self.reload in self.hooks.listeners:
            self.hooks.listeners.remove(self.reload)
        if MarketplaceForm.instance is self:
            MarketplaceForm.instance = None
