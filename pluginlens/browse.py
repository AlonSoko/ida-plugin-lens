"""The marketplace's Browse tab: every plugin in hcli's repositories."""

import html
import json
import os
import threading
import time
from dataclasses import dataclass

import ida_diskio
import ida_kernwin

from . import hcli, logos, operations
from .install_source import install_from_source
from .common import (
    ICON_SIZE, PLUGIN_NAME, USER_DIR, QtCore, QtGui, QtWidgets, badged_png, declared_logo, hcli_folders, icon_note,
    metadata, settings,
)

CATALOG_FILE = "catalog.json"
COLUMNS = ["Plugin", "Version", "Status", "Repository", "Categories"]
CATEGORIES = {
    "disassembly-and-processor-modules": "Disassembly and processor modules",
    "file-parsers-and-loaders": "File parsers and loaders",
    "decompilation": "Decompilation",
    "debugging-and-tracing": "Debugging and tracing",
    "deobfuscation": "Deobfuscation",
    "collaboration-and-productivity": "Collaboration and productivity",
    "integration-with-third-parties-interoperability": "Integration with third parties",
    "api-scripting-and-automation": "API, scripting and automation",
    "ui-ux-and-visualization": "UI/UX and visualization",
    "malware-analysis": "Malware analysis",
    "vulnerability-research-and-exploit-development": "Vulnerability research and exploits",
    "other": "Other",
}
SHOW_FILTERS = [
    ("All plugins", None),
    ("Not installed", lambda e: not e.installed),
    ("Installed", lambda e: e.installed),
    ("Updates", lambda e: e.can_update),
]
_resource = QtGui.QTextDocument.ResourceType.ImageResource
_IMAGE_RESOURCE = int(getattr(_resource, "value", _resource))  # an int: PySide6 enum or PyQt5 int
ENTRY_ROLE = QtCore.Qt.ItemDataRole.UserRole
SORT_ROLE = QtCore.Qt.ItemDataRole.UserRole + 1


def _host_key(url):
    """A repository URL, normalized for comparing."""
    url = (url or "").strip().lower().rstrip("/")
    return url[:-4] if url.endswith(".git") else url


def _owner(host):
    """"owner/name" for a GitHub URL, else the URL."""
    prefix = "https://github.com/"
    return host[len(prefix):] if host.lower().startswith(prefix) else host


@dataclass
class Entry:
    name: str
    host: str
    repo: str
    versions: list  # [{"version", "compatible"}], newest first
    latest: str
    latest_compatible: str  # None if no version is compatible
    md: dict  # the ida-plugin.json "plugin" object
    archive: str = ""  # the URL of the version hcli would install
    folder: str = None  # where it's installed, when it is
    installed_version: str = None  # set when this plugin is installed
    update: str = None  # the version hcli can update it to
    changed: str = None  # what was done to it this session (operations.INSTALL, ...)
    name_taken: bool = False  # another plugin with this name is installed
    same_name: bool = False  # another plugin in the catalog has this name

    @property
    def key(self):
        return (self.repo, _host_key(self.host), self.name)

    @property
    def installed(self):
        return self.installed_version is not None

    @property
    def compatible(self):
        return self.latest_compatible is not None

    @property
    def can_install(self):
        return not self.installed and not self.name_taken and self.compatible and self.changed is None

    @property
    def can_update(self):
        return self.installed and bool(self.update) and self.changed != operations.UNINSTALL

    @property
    def can_uninstall(self):
        return self.installed and self.changed != operations.UNINSTALL

    @property
    def title(self):
        """The name, with the GitHub owner when another plugin has the same name."""
        return f"{self.name} ({_owner(self.host)})" if self.same_name else self.name

    @property
    def version(self):
        return self.latest_compatible or self.latest

    @property
    def categories(self):
        return [CATEGORIES.get(c, c) for c in self.md.get("categories") or []]

    @property
    def authors(self):
        people = self.md.get("authors") or self.md.get("maintainers") or []
        return [p.get("name") or p.get("email") or "" for p in people if isinstance(p, dict)]

    @property
    def status(self):
        if self.changed:
            done = {operations.INSTALL: "Installed", operations.UPGRADE: "Updated",
                    operations.UNINSTALL: "Uninstalled"}[self.changed]
            return f"{done} · restart IDA"
        if self.installed:
            if self.update:
                return f"Update to {self.update}"
            return "Installed" if self.installed_version == self.version else f"Installed {self.installed_version}"
        if self.name_taken:
            return "Same name as an installed plugin"
        return "" if self.compatible else "Not compatible"


def catalog_entries(catalog, status_entries):
    """The Entry for each plugin of catalog (hcli.catalog()), with what
    status_entries (hcli.status(), or None) says is installed."""
    folders = hcli_folders()  # name -> folder, of the plugins hcli installed
    installed = {}  # name -> (version, update)
    for p in status_entries or []:
        if p.get("kind") == "installed" and p.get("name"):
            installed[p["name"]] = (p.get("version") or "", p.get("upgradable_to"))
    if status_entries is None:  # hcli not asked yet: what's on disk
        for name, folder in folders.items():
            installed[name] = (metadata(folder).get("version") or "", None)
    hosts = {}  # name -> the installed plugin's repository URL
    for name, folder in folders.items():
        urls = metadata(folder).get("urls")
        if isinstance(urls, dict) and urls.get("repository"):
            hosts[name] = _host_key(urls["repository"])

    plugins = (catalog or {}).get("plugins") or []
    counts = {}
    for p in plugins:
        counts[p["name"]] = counts.get(p["name"], 0) + 1
    entries = []
    for p in plugins:
        entry = Entry(
            name=p["name"], host=p.get("host") or "", repo=p.get("repo") or "",
            versions=p.get("versions") or [], latest=p.get("latest") or "",
            latest_compatible=p.get("latest_compatible"), md=p.get("metadata") or {},
            archive=p.get("archive") or "", same_name=counts[p["name"]] > 1,
        )
        if entry.name in installed:
            host = hosts.get(entry.name)
            if host is None or host == _host_key(entry.host):
                entry.installed_version, entry.update = installed[entry.name]
                entry.folder = folders.get(entry.name)
            else:
                entry.name_taken = True
        changed = operations.changed_this_session.get(entry.name)
        # only while it still holds (e.g. not if uninstalled since, outside this window)
        if changed is not None and not entry.name_taken and entry.installed == (changed != operations.UNINSTALL):
            entry.changed = changed
        entries.append(entry)
    return entries


class Catalog:
    """hcli's catalog, shared by the windows: read from the cache file at
    first, then asked from hcli in the background once per session."""

    data = None  # hcli.catalog()'s result
    updated = None  # when it was asked from hcli (time.time())
    error = None  # the last failure, as text
    loading = False
    asked = False  # asked this session
    listeners = []  # called with no arguments when anything above changes

    @classmethod
    def path(cls):
        return os.path.join(ida_diskio.get_user_idadir(), USER_DIR, CATALOG_FILE)

    @classmethod
    def load(cls):
        """The cached catalog, and ask hcli if not asked this session."""
        if cls.data is None:
            try:
                with open(cls.path(), encoding="utf-8") as f:
                    cached = json.load(f)
                cls.data, cls.updated = cached["catalog"], cached["updated"]
            except (OSError, ValueError, KeyError, TypeError):
                pass
        if not cls.asked:
            cls.refresh()

    @classmethod
    def refresh(cls):
        if cls.loading:
            return
        cls.asked = cls.loading = True
        cls._notify()

        def worker():
            try:
                data, error = hcli.catalog(), None
            except Exception as e:  # hcli not importable, offline, ...
                data, error = None, f"{type(e).__name__}: {e}"

            def done():
                cls.loading = False
                cls.error = error
                if data is not None:
                    cls.data, cls.updated = data, time.time()
                    cls._save()
                cls._notify()
                return 0

            ida_kernwin.execute_sync(done, ida_kernwin.MFF_FAST)

        threading.Thread(target=worker, name="plugin-lens-catalog", daemon=True).start()

    @classmethod
    def _save(cls):
        try:
            os.makedirs(os.path.dirname(cls.path()), exist_ok=True)
            with open(cls.path(), "w", encoding="utf-8") as f:
                json.dump({"updated": cls.updated, "catalog": cls.data}, f)
        except OSError:
            pass

    @classmethod
    def _notify(cls):
        for listener in list(cls.listeners):
            try:
                listener()
            except RuntimeError:  # its window is gone
                cls.listeners.remove(listener)


def _local_icons(status_entries):
    """{hcli name: image} for the installed plugins, as Edit > Plugins shows them."""
    from .common import icon_image
    from .library import library_rows  # (library imports this module)

    return {
        row.hcli_name: image
        for row in library_rows(status_entries)
        if row.hcli_name and (image := icon_image(row.name, row.plugin_file))
    }


def _ago(when):
    seconds = max(0, time.time() - when)
    if seconds < 90:
        return "just now"
    if seconds < 90 * 60:
        return f"{round(seconds / 60)} minutes ago"
    if seconds < 36 * 3600:
        return f"{round(seconds / 3600)} hours ago"
    return f"{round(seconds / 86400)} days ago"


def _ida_versions(versions):
    if not versions:
        return ""
    return ", ".join(versions) if len(versions) <= 4 else f"{versions[0]} – {versions[-1]}"


class _Filter(QtCore.QSortFilterProxyModel):
    def __init__(self):
        super().__init__()
        self.text = ""
        self.repo = None
        self.category = None
        self.show_test = None
        self.compatible_only = True

    def filterAcceptsRow(self, source_row, source_parent):
        entry = self.sourceModel().index(source_row, 0, source_parent).data(ENTRY_ROLE)
        if entry is None:
            return False
        if self.compatible_only and not entry.compatible and not entry.installed:
            return False
        if self.repo is not None and entry.repo != self.repo:
            return False
        if self.category is not None and self.category not in (entry.md.get("categories") or []):
            return False
        if self.show_test is not None and not self.show_test(entry):
            return False
        if self.text:
            haystack = " ".join([
                entry.name, entry.host, entry.repo, entry.md.get("description") or "",
                *entry.authors, *entry.categories, *(entry.md.get("keywords") or []),
            ]).casefold()
            if not all(word in haystack for word in self.text.casefold().split()):
                return False
        return True


class BrowseTab(QtWidgets.QWidget):
    def __init__(self, hooks):
        super().__init__()
        self.hooks = hooks
        self.entries = []
        self.icons = {}  # image path -> QIcon (None if unreadable)
        self._local = {}  # hcli name -> the icon Edit > Plugins shows
        blank = QtGui.QPixmap(ICON_SIZE, ICON_SIZE)  # as big as the icons, scaled alike
        blank.fill(QtCore.Qt.GlobalColor.transparent)
        self._blank = QtGui.QIcon(blank)
        self._fetch_logos = True
        self._build()
        Catalog.listeners.append(self.reload)
        hooks.listeners.append(self.reload)
        logos.fetcher.listeners.append(self._logo_ready)
        Catalog.load()
        self.reload()

    def close_tab(self):
        for listeners in (Catalog.listeners, self.hooks.listeners):
            if self.reload in listeners:
                listeners.remove(self.reload)
        if self._logo_ready in logos.fetcher.listeners:
            logos.fetcher.listeners.remove(self._logo_ready)

    # -- logos -------------------------------------------------------------
    def _logo(self, entry, fetch=True):
        """The image to show for entry, or None (asking GitHub for it if fetch)."""
        if entry.installed:  # as Edit > Plugins shows it, else its own copy
            image = self._local.get(entry.name) or (declared_logo(entry.folder) if entry.folder else None)
            if image:
                return image
        if not entry.md.get("logoPath"):
            return None
        key = logos.key_of(entry.host, entry.name)
        image = logos.cached(key)
        if image is None and fetch and self._fetch_logos:
            logos.fetcher.want(key, entry.host, entry.md["logoPath"], entry.archive)
        return image

    def _icon(self, image):
        if image not in self.icons:
            data = badged_png(image)
            pixmap = QtGui.QPixmap()
            self.icons[image] = QtGui.QIcon(pixmap) if data and pixmap.loadFromData(data) else None
        return self.icons[image]

    def _logo_ready(self, key):
        """A logo was fetched: show it."""
        for row in range(self.model.rowCount()):
            item = self.model.item(row, 0)
            entry = item.data(ENTRY_ROLE)
            if entry is not None and logos.key_of(entry.host, entry.name) == key:
                image = self._logo(entry, fetch=False)
                icon = self._icon(image) if image else None
                if icon is not None:
                    item.setIcon(icon)
        selected = self._selected()
        if selected and logos.key_of(selected[0].host, selected[0].name) == key:
            self._selection_changed()

    # -- building ----------------------------------------------------------
    def _build(self):
        layout = QtWidgets.QVBoxLayout(self)

        bar = QtWidgets.QHBoxLayout()
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("Search names, descriptions, keywords, authors")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(160)
        self.search.textChanged.connect(self._filter_changed)
        bar.addWidget(self.search, 3)
        self.repo_box = QtWidgets.QComboBox()
        self.repo_box.currentIndexChanged.connect(self._filter_changed)
        bar.addWidget(self.repo_box, 1)
        self.category_box = QtWidgets.QComboBox()
        self.category_box.addItem("All categories", None)
        for slug, text in sorted(CATEGORIES.items(), key=lambda kv: (kv[0] == "other", kv[1])):
            self.category_box.addItem(text, slug)
        self.category_box.currentIndexChanged.connect(self._filter_changed)
        bar.addWidget(self.category_box, 1)
        self.show_box = QtWidgets.QComboBox()
        for text, _ in SHOW_FILTERS:
            self.show_box.addItem(text)
        self.show_box.currentIndexChanged.connect(self._filter_changed)
        bar.addWidget(self.show_box)
        for box in (self.repo_box, self.category_box, self.show_box):
            # narrower than their longest item when the window is narrow
            box.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            box.setMinimumContentsLength(10)
        self.compatible_box = QtWidgets.QCheckBox("Compatible only")
        self.compatible_box.setChecked(True)
        self.compatible_box.setToolTip("Hide the plugins without a version for this IDA and platform")
        self.compatible_box.toggled.connect(self._filter_changed)
        bar.addWidget(self.compatible_box)
        self.refresh_button = QtWidgets.QPushButton("Refresh")
        self.refresh_button.setToolTip("Ask hcli's repositories again")
        self.refresh_button.clicked.connect(Catalog.refresh)
        bar.addWidget(self.refresh_button)

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
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setWordWrap(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # the categories take the width left, elided
        self.table.horizontalHeader().setSectionResizeMode(len(COLUMNS) - 1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.selectionModel().selectionChanged.connect(self._selection_changed)

        details = QtWidgets.QWidget()
        details_layout = QtWidgets.QVBoxLayout(details)
        details_layout.setContentsMargins(0, 0, 0, 0)
        self.details = QtWidgets.QTextBrowser()
        self.details.setOpenExternalLinks(True)
        details_layout.addWidget(self.details, 1)
        actions = QtWidgets.QHBoxLayout()
        self.version_box = QtWidgets.QComboBox()
        self.version_box.setToolTip("The version to install")
        actions.addWidget(self.version_box, 1)
        self.install_button = QtWidgets.QPushButton("Install")
        self.install_button.clicked.connect(self._install)
        self.update_button = QtWidgets.QPushButton("Update")
        self.update_button.clicked.connect(self._update)
        self.uninstall_button = QtWidgets.QPushButton("Uninstall")
        self.uninstall_button.clicked.connect(self._uninstall)
        for button in (self.install_button, self.update_button, self.uninstall_button):
            actions.addWidget(button)
        details_layout.addLayout(actions)

        splitter = QtWidgets.QSplitter()
        splitter.addWidget(self.table)
        splitter.addWidget(details)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([680, 320])  # in proportion
        layout.addWidget(splitter, 1)

        footer = QtWidgets.QHBoxLayout()
        self.summary = QtWidgets.QLabel()
        footer.addWidget(self.summary, 1)
        self.notes = QtWidgets.QLabel()
        self.notes.setWordWrap(True)
        footer.addWidget(self.notes, 2)
        from_source = QtWidgets.QPushButton("Install from URL or file…")
        from_source.setToolTip("Install a plugin that isn't in the repositories: from GitHub, a .zip, or a folder")
        from_source.clicked.connect(lambda: install_from_source(self, self.hooks.set_status))
        footer.addWidget(from_source)
        layout.addLayout(footer)

    # -- data --------------------------------------------------------------
    def reload(self):
        """Rebuild the rows from the catalog and hcli's last status."""
        selected = self._selected()
        try:
            self._fetch_logos = settings()["fetch_logos"]
            self._local = _local_icons(self.hooks.status_entries)
            self.entries = catalog_entries(Catalog.data, self.hooks.status_entries)
        except Exception as e:  # never break IDA over the window
            ida_kernwin.msg(f"[{PLUGIN_NAME}] browse: {type(e).__name__}: {e}\n")
            self.entries = []

        repos = sorted({e.repo for e in self.entries if e.repo})
        current = self.repo_box.currentData()
        self.repo_box.blockSignals(True)
        self.repo_box.clear()
        self.repo_box.addItem("All repositories", None)
        for repo in repos:
            self.repo_box.addItem(f"{repo} repository", repo)
        self.repo_box.setCurrentIndex(max(0, self.repo_box.findData(current)))
        self.repo_box.blockSignals(False)

        self.table.setSortingEnabled(False)
        self.model.removeRows(0, self.model.rowCount())
        bold = QtGui.QFont()
        bold.setBold(True)
        for entry in self.entries:
            texts = [entry.title, entry.version, entry.status, entry.repo, ", ".join(entry.categories)]
            items = []
            for text in texts:
                item = QtGui.QStandardItem(text)
                item.setData(text.casefold(), SORT_ROLE)
                items.append(item)
            items[0].setData(entry, ENTRY_ROLE)
            image = self._logo(entry)
            icon = self._icon(image) if image else None
            items[0].setIcon(icon if icon is not None else self._blank)  # blank: names aligned
            tip = entry.md.get("description") or entry.name
            if icon_note(image):
                tip += f"\n\nIcon: {icon_note(image)}"
            items[0].setToolTip(tip)
            for item in items[2:]:
                item.setToolTip(item.text())
            items[1].setData(_version_key(entry.version), SORT_ROLE)
            # installed and updates first, then the rest
            rank = 0 if entry.can_update else 1 if entry.installed else 2 if entry.status else 3
            items[2].setData(f"{rank}|{entry.status.casefold()}", SORT_ROLE)
            if entry.can_update or entry.changed:
                items[2].setFont(bold)
            if not entry.compatible:
                grey = self.palette().color(QtGui.QPalette.ColorRole.PlaceholderText)
                for item in items:
                    item.setForeground(grey)
            self.model.appendRow(items)
        self.table.setSortingEnabled(True)
        if self.table.horizontalHeader().sortIndicatorSection() >= len(COLUMNS) or not self._sorted_once:
            self.table.sortByColumn(0, QtCore.Qt.SortOrder.AscendingOrder)
            self._sorted_once = True
        self.table.resizeColumnsToContents()
        for column, widest in ((0, 260), (2, 170)):
            self.table.setColumnWidth(column, min(self.table.columnWidth(column), widest))
        self._select(selected[0].key if selected else None)
        self._update_footer()

    _sorted_once = False

    def _select(self, key):
        for row in range(self.proxy.rowCount()):
            entry = self.proxy.index(row, 0).data(ENTRY_ROLE)
            if entry is not None and entry.key == key:
                self.table.selectRow(row)
                return
        self._selection_changed()

    def _update_footer(self):
        catalog = Catalog.data or {}
        shown, total = self.proxy.rowCount(), len(self.entries)
        parts = [f"{shown} of {total} plugins" if shown != total else f"{total} plugins"]
        if catalog.get("ida_version"):
            parts.append(f"for IDA {catalog['ida_version']} on {catalog.get('platform')}")
        if Catalog.loading:
            parts.append("asking hcli…")
        elif Catalog.updated:
            parts.append(f"updated {_ago(Catalog.updated)}")
        self.summary.setText(" · ".join(parts))
        notes = list(catalog.get("notes") or [])
        if Catalog.error:
            notes.insert(0, f"Couldn't ask hcli: {Catalog.error}")
        self.notes.setText("\n".join(notes))
        self.refresh_button.setEnabled(not Catalog.loading)

    def _filter_changed(self, *_):
        self.proxy.text = self.search.text().strip()
        self.proxy.repo = self.repo_box.currentData()
        self.proxy.category = self.category_box.currentData()
        self.proxy.show_test = SHOW_FILTERS[self.show_box.currentIndex()][1]
        self.proxy.compatible_only = self.compatible_box.isChecked()
        self.proxy.invalidateFilter()
        self._update_footer()

    def _selected(self):
        try:
            indexes = self.table.selectionModel().selectedRows(0)
        except RuntimeError:
            return []
        return [i.data(ENTRY_ROLE) for i in indexes if i.data(ENTRY_ROLE) is not None]

    def _selection_changed(self, *_):
        selected = self._selected()
        entry = selected[0] if selected else None
        self.details.setHtml(self._details_html(entry) if entry else
                             "<p>Select a plugin to see its details.</p>")
        self.version_box.clear()
        if entry is not None and entry.can_install:
            for v in entry.versions:
                text = v["version"] + ("" if v["compatible"] else " (not compatible)")
                self.version_box.addItem(text, v["version"])
                if not v["compatible"]:
                    item = self.version_box.model().item(self.version_box.count() - 1)
                    item.setEnabled(False)
            self.version_box.setCurrentIndex(max(0, self.version_box.findData(entry.latest_compatible)))
        self.version_box.setVisible(entry is not None and entry.can_install)
        self.install_button.setVisible(entry is None or not entry.installed)
        self.install_button.setEnabled(entry is not None and entry.can_install)
        self.update_button.setVisible(entry is not None and entry.installed)
        self.update_button.setEnabled(entry is not None and entry.can_update)
        self.update_button.setText(f"Update to {entry.update}" if entry is not None and entry.can_update else "Update")
        self.uninstall_button.setVisible(entry is not None and entry.installed)
        self.uninstall_button.setEnabled(entry is not None and entry.can_uninstall)

    def _details_html(self, entry):
        md = entry.md
        image = self._logo(entry, fetch=False)
        e = html.escape
        catalog = Catalog.data or {}

        def link(url):
            return f'<a href="{e(url)}">{e(url)}</a>'

        rows = []
        if entry.installed and entry.installed_version != entry.version:
            rows.append(("Installed", e(entry.installed_version or "?")))
        version = e(entry.version)
        if not entry.compatible:
            version += (f" — no version for IDA {e(catalog.get('ida_version') or '?')}"
                        f" on {e(catalog.get('platform') or '?')}")
        elif entry.latest != entry.latest_compatible:
            version += f" (the latest, {e(entry.latest)}, isn't compatible)"
        rows.append(("Latest version", version))
        rows.append(("Repository", e(entry.repo)))
        rows.append(("Source", link(entry.host)))
        urls = md.get("urls") if isinstance(md.get("urls"), dict) else {}
        if urls.get("homepage") and _host_key(urls["homepage"]) != _host_key(entry.host):
            rows.append(("Homepage", link(urls["homepage"])))
        if entry.authors:
            rows.append(("Authors", e(", ".join(entry.authors))))
        if md.get("license"):
            rows.append(("License", e(md["license"])))
        if entry.categories:
            rows.append(("Categories", e(", ".join(entry.categories))))
        if md.get("keywords"):
            rows.append(("Keywords", e(", ".join(md["keywords"]))))
        rows.append(("IDA versions", e(_ida_versions(md.get("idaVersions") or []))))
        rows.append(("Platforms", e(", ".join(md.get("platforms") or []))))
        if icon_note(image):
            rows.append(("Icon", e(icon_note(image))))
        if md.get("requiresPython"):
            rows.append(("Python", e(md["requiresPython"])))
        if md.get("pythonDependencies"):
            rows.append(("Python packages", e(", ".join(map(str, md["pythonDependencies"])))))
        if md.get("dependencies"):
            rows.append(("Plugins it needs", e(", ".join(map(str, md["dependencies"])))))
        table = "".join(
            f'<tr><td style="padding-right:12px; white-space:nowrap; vertical-align:top"><b>{k}</b></td>'
            f"<td>{v}</td></tr>" for k, v in rows
        )
        status = f"<p><i>{e(entry.status)}</i></p>" if entry.status else ""
        title = f"<h2>{e(entry.name)}</h2>"
        data = badged_png(image) if image else None
        if data:
            src = "plugin-lens-logo:" + e(entry.name)
            self.details.document().addResource(
                _IMAGE_RESOURCE, QtCore.QUrl(src), QtGui.QImage.fromData(data))
            title = (f'<table><tr><td style="padding-right:10px; vertical-align:middle">'
                     f'<img src="{src}" width="48" height="48"></td>'
                     f'<td style="vertical-align:middle">{title}</td></tr></table>')
        return (f"{title}{status}<p>{e(md.get('description') or '')}</p>"
                f'<table cellspacing="2">{table}</table>')

    # -- actions -------------------------------------------------------------
    def _install(self):
        selected = self._selected()
        if not selected or not selected[0].can_install:
            return
        entry = selected[0]
        version = self.version_box.currentData() or entry.latest_compatible
        md = entry.md
        lines = [f"Install {entry.name} {version} with hcli?", "",
                 f"From: {entry.host}", f"Repository: {entry.repo}"]
        if entry.authors:
            lines.append(f"Authors: {', '.join(entry.authors)}")
        if md.get("pythonDependencies"):
            lines.append(f"Python packages pip installs: {', '.join(map(str, md['pythonDependencies']))}")
        if md.get("dependencies"):
            lines.append(f"Plugins it needs, installed too: {', '.join(map(str, md['dependencies']))}")
        lines += ["", "A plugin runs inside IDA with access to your files: install only the ones you trust.",
                  "IDA restarts afterwards to load it."]
        if ida_kernwin.ask_yn(ida_kernwin.ASKBTN_NO, "\n".join(lines)) != ida_kernwin.ASKBTN_YES:
            return
        reference = hcli.install_reference(entry.name, entry.repo, entry.host, version)
        operations.perform([(operations.INSTALL, entry.name, reference)], on_status=self.hooks.set_status)

    def _update(self):
        selected = self._selected()
        if selected and selected[0].can_update:
            operations.perform([(operations.UPGRADE, selected[0].name)], on_status=self.hooks.set_status)

    def _uninstall(self):
        selected = self._selected()
        if not selected or not selected[0].can_uninstall:
            return
        entry = selected[0]
        warning = ("\n\nThis is Plugin Lens itself: this window and the menu changes go away after the restart."
                   if entry.name == "ida-plugin-lens" else "")
        question = f"Uninstall {entry.name} with hcli?{warning}\n\nThe files are removed now; IDA restarts afterwards."
        if ida_kernwin.ask_yn(ida_kernwin.ASKBTN_NO, question) != ida_kernwin.ASKBTN_YES:
            return
        operations.perform([(operations.UNINSTALL, entry.name)], on_status=self.hooks.set_status)


def _version_key(version):
    """Sorts versions numerically: "1.10.0" after "1.9.2"."""
    parts = []
    for part in version.replace("-", ".").split("."):
        parts.append(f"{int(part):08d}" if part.isdigit() else part)
    return ".".join(parts)
