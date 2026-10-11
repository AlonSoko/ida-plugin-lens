"""Edit > Plugins: icons, groups, versions, and the entries Plugin Lens adds."""

import threading

import ida_kernwin

from . import hcli, operations
from .common import (
    MENU_PREFIX, QtGui, QtWidgets,
    group_of, group_order, icon_image, label, plugin_paths, settings, square_png,
)

UPDATE_ACTION = "plugin_lens:update"
MARKETPLACE_ACTION = "plugin_lens:marketplace"
MARKETPLACE_LABEL = "Plugin marketplace…"


class _UpdatesChooser(ida_kernwin.Choose):
    """The plugins hcli can update, each with a checkbox, all checked."""

    def __init__(self, rows):
        super().__init__(
            "Update plugins",
            [
                ["Plugin", 24 | ida_kernwin.CHCOL_CHECKBOX],
                ["Installed", 12 | ida_kernwin.Choose.CHCOL_PLAIN],
                ["Available", 12 | ida_kernwin.Choose.CHCOL_PLAIN],
            ],
        )
        self.items = rows
        self.unchecked = set()

    def OnGetSize(self):
        return len(self.items)

    def OnGetLine(self, n):
        return self.items[n]

    def OnGetLineAttr(self, n):
        checked = n not in self.unchecked
        return [0xFFFFFFFF, ida_kernwin.CHITEM_CHKST_CHECKED if checked else ida_kernwin.CHITEM_CHKST_UNCHECKED]

    def OnCheckedLine(self, n, state):
        if state == ida_kernwin.CHITEM_CHKST_CHECKED:
            self.unchecked.discard(n)
        else:
            self.unchecked.add(n)
        return (ida_kernwin.Choose.ALL_CHANGED, n)

    def checked(self):
        """The checked row indices."""
        return [n for n in range(len(self.items)) if n not in self.unchecked]


class Handler(ida_kernwin.action_handler_t):
    def __init__(self, callback):
        super().__init__()
        self.callback = callback

    def activate(self, ctx):
        self.callback()
        return 1

    def update(self, ctx):
        return ida_kernwin.AST_ENABLE_ALWAYS


class MenuHooks(ida_kernwin.UI_Hooks):
    def __init__(self):
        super().__init__()
        self._icons = {}  # image path -> icon id (None if it can't be loaded)
        self._set = {}  # action name -> icon id set by this plugin
        self._seen = None  # the plugin actions handled last time
        self._plugin_paths = {}
        self._settings = settings()
        self._names = set()  # menu names of the plugins with an entry
        self._groups = {}  # menu name -> group, when grouping the menu
        self._labels = {}  # menu name -> text shown, when showing versions
        self.hcli_sources = hcli.cached_sources()
        self._hcli_asked = False  # whether hcli was asked this session
        self._added = {}  # id(menu) -> the headers and separators added to it
        self._connected = set()  # ids of the menus styled again before each opening
        self._menu_retries = 100  # updates to wait for the menu to exist
        self._update_label = None  # the update entry's text, while it's shown
        self.status_entries = None  # hcli's last full status (hcli.status()), this session
        self.listeners = []  # called with no arguments when the hcli information changes

    def updated_actions(self):
        # called whenever IDA updates its actions: only act when the set of
        # plugin menu entries changed (e.g. when a database opens)
        actions = frozenset(
            name for name in ida_kernwin.get_registered_actions() if name.startswith(MENU_PREFIX)
        )
        if actions == self._seen:
            if not self._connected and self._menu_retries > 0:
                self._menu_retries -= 1
                self._style_menus()  # the menu didn't exist yet last time
            return
        self._seen = actions
        self.apply(actions)

    def apply(self, actions):
        paths = self._plugin_paths = plugin_paths()
        current = self._settings = settings()
        self._names = {action[len(MENU_PREFIX):] for action in actions} & set(paths)
        self._update_menus()
        if current["hcli_sources"]:
            self._refresh_hcli_sources()

        for action in sorted(actions):
            ok, icon_id = ida_kernwin.get_action_icon(action)
            if ok and icon_id not in (-1, 0, self._set.get(action)):
                continue  # the plugin set its own icon
            menu_name = action[len(MENU_PREFIX):]
            image = icon_image(menu_name, paths.get(menu_name))
            if not image:
                continue
            if image not in self._icons:
                data = square_png(image)
                icon = ida_kernwin.load_custom_icon(data=data, format="png") if data else 0
                self._icons[image] = icon if icon and icon > 0 else None
            icon = self._icons[image]
            if icon is not None and ida_kernwin.update_action_icon(action, icon):
                self._set[action] = icon

    def _refresh_hcli_sources(self):
        """Ask hcli where its plugins come from, once per session, in the background."""
        if self._hcli_asked:
            return
        self._hcli_asked = True

        def worker():
            try:
                entries = hcli.status()
            except Exception:  # hcli not importable, an hcli version that differs, offline, ...
                entries = None

            def done():
                self.set_status(entries)
                return 0

            # back on IDA's main thread, which owns the menus
            ida_kernwin.execute_sync(done, ida_kernwin.MFF_FAST)

        threading.Thread(target=worker, name="plugin-lens-hcli", daemon=True).start()

    def set_status(self, entries):
        """Take a fresh hcli status (from hcli.status(); None if unknown)."""
        if entries is None:
            return
        self.status_entries = entries
        sources = hcli.sources_from_status(entries)
        hcli.save_sources(sources)
        self.set_sources(sources)

    def refresh_status(self):
        """Ask hcli again, in the background (e.g. the marketplace's Refresh)."""
        self._hcli_asked = False
        self._refresh_hcli_sources()

    def set_sources(self, sources):
        self.hcli_sources = sources
        self._update_menus()
        for listener in list(self.listeners):
            listener()

    def _update_menus(self):
        """Work out each entry's group and text, and apply them to the menus."""
        current = self._settings
        sources = self.hcli_sources if current["hcli_sources"] else None
        groups = {name: group_of(self._plugin_paths[name], sources) for name in self._names}
        self._groups = groups if current["group_menu"] else {}
        self._labels = {}
        if current["show_versions"]:
            for name, group in groups.items():
                text = label(name, self._plugin_paths[name], group, sources)
                if text != name:
                    self._labels[name] = text
        self._update_action()
        self._style_menus()

    def outdated(self):
        """[(hcli name, installed version, available version)] that hcli can update."""
        sources = self.hcli_sources if self._settings["hcli_sources"] else None
        return sorted(
            (name, info.get("version") or "", info["latest"])
            for name, info in (sources or {}).items()
            if info.get("outdated") and info.get("latest") and info.get("repo")
        )

    def _update_action(self):
        """Show "Update plugins (N)..." at the top of Edit > Plugins, if N > 0."""
        outdated = self.outdated() if self._settings["update_action"] else []
        if outdated:
            self._update_label = f"Update plugins ({len(outdated)})…"
            ida_kernwin.update_action_label(UPDATE_ACTION, self._update_label)
        elif self._update_label is not None:
            self._update_label = None
            ida_kernwin.detach_action_from_menu(MENU_PREFIX, UPDATE_ACTION)
        self._attach_entries()

    def _attach_entries(self, menu=None):
        """Put the marketplace entry, and the update entry if shown, at the top.

        IDA replaces the Plugins menu (e.g. once a database is loaded), and
        actions attached to the old one are gone; so this is checked again
        each time the menu is styled.
        """
        wanted = [MARKETPLACE_LABEL] + ([self._update_label] if self._update_label else [])
        if menu is not None:
            texts = [a.text().replace("&", "") for a in menu.actions()]
            if all(text in texts for text in wanted):
                return
        for action in (UPDATE_ACTION, MARKETPLACE_ACTION):
            ida_kernwin.detach_action_from_menu(MENU_PREFIX, action)
        # SETMENU_FIRST: the one attached last ends up first
        if self._update_label:
            ida_kernwin.attach_action_to_menu(MENU_PREFIX, UPDATE_ACTION, ida_kernwin.SETMENU_FIRST)
        ida_kernwin.attach_action_to_menu(MENU_PREFIX, MARKETPLACE_ACTION, ida_kernwin.SETMENU_FIRST)

    def update_plugins(self):
        """Let the user pick plugins to update, and update them with hcli."""
        outdated = self.outdated()
        if not outdated:
            ida_kernwin.info("All plugins are up to date.")
            return
        from .library import library_rows  # the names IDA shows

        try:
            titles = {row.hcli_name: row.name for row in library_rows(self.status_entries) if row.hcli_name}
        except Exception:
            titles = {}
        chooser = _UpdatesChooser([[titles.get(name, name), *versions] for name, *versions in outdated])
        if chooser.Show(modal=True) < 0:
            return  # cancelled
        names = [outdated[i][0] for i in chooser.checked()]
        if not names:
            ida_kernwin.info("No plugin was checked, so nothing was updated.")
            return
        operations.perform([(operations.UPGRADE, name) for name in names],
                           on_status=self.set_status, titles=titles)

    def _name(self, action):
        """The plugin's menu name for an entry, whatever its text shows now."""
        text = action.text().replace("&&", "\0").replace("&", "").replace("\0", "&")
        if text in self._names:
            return text
        return {shown: name for name, shown in self._labels.items()}.get(text, text)

    def _style_menus(self):
        """Group and label the entries of every menu listing the plugins."""
        app = QtWidgets.QApplication.instance()
        if app is None:
            return
        for widget in app.allWidgets():
            try:
                if not isinstance(widget, QtWidgets.QMenu):
                    continue
                if not any(self._name(a) in self._names for a in widget.actions()):
                    continue
                if id(widget) not in self._connected:
                    # IDA may rebuild the items: style them again before each opening
                    self._connected.add(id(widget))
                    widget.aboutToShow.connect(lambda menu=widget: self._style(menu))
                self._style(widget)
            except RuntimeError:  # a widget Qt has deleted meanwhile
                continue

    def _style(self, menu):
        try:
            self._attach_entries(menu)
            self._label(menu)
            self._group(menu)
        except RuntimeError:  # the menu was deleted
            self._added.pop(id(menu), None)

    def _label(self, menu):
        """Show each plugin's version after its name (or just the name)."""
        for action in menu.actions():
            name = self._name(action)
            if name not in self._names:
                continue
            text = self._labels.get(name, name).replace("&", "&&")
            if action.text() != text:
                action.setText(text)

    def _group(self, menu):
        """Reorder menu's plugin entries into the groups, each under a header.

        Entries that aren't plugins (e.g. "Quick run plugins") stay first, in
        their order; the plugin entries follow, alphabetically per group.
        """
        for added in self._added.pop(id(menu), []):
            menu.removeAction(added)
        if not self._groups:
            return

        others, grouped = [], {}
        for action in menu.actions():
            group = self._groups.get(self._name(action))
            if group is None:
                others.append(action)
            else:
                grouped.setdefault(group, []).append(action)
        for actions in grouped.values():
            for action in actions:
                menu.removeAction(action)

        # a separator before each header, reusing one the menu already ends with
        separate = bool(others) and not others[-1].isSeparator()
        added = []
        for group in sorted(grouped, key=group_order):
            if separate:
                added.append(menu.addSeparator())
            separate = True
            header = QtGui.QAction(group, menu) if hasattr(QtGui, "QAction") else QtWidgets.QAction(group, menu)
            header.setEnabled(False)
            menu.addAction(header)
            added.append(header)
            for action in sorted(grouped[group], key=lambda a: self._name(a).casefold()):
                menu.addAction(action)
        self._added[id(menu)] = added
