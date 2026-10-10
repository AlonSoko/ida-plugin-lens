"""Plugin Lens: icons, groups and versions in IDA's Edit > Plugins menu.

Icons: every entry without an icon gets, in order,
1. an image you put in <IDA user directory>/plugin-lens/, named after the
   menu entry, e.g. "Diaphora.png" (an empty "Diaphora.none" leaves that
   entry without an icon);
2. the logo the plugin declares in its ida-plugin.json ("logoPath");
3. an icon from this plugin's icons/ folder (IDA's built-in plugins, and a
   few plugins without a logo).
Icons a plugin sets itself are left alone.

Groups: the entries are sorted into IDA's own plugins, plugins installed with
hcli (those with an ida-plugin.json) and plugins installed manually, each
under a header. Plugins installed with hcli are split by where hcli got them
(a plugin repository, or a local file), as `hcli plugin status --json`
reports; that is asked in the background each time IDA starts, through
hcli's own Python code (so IDA must be able to import hcli), and its last
answer is kept for grouping the menu until then.

Settings, in <IDA user directory>/plugin-lens/settings.json:
  {"group_menu": false}     keep IDA's own order
  {"show_versions": false}  don't show the versions
  {"hcli_sources": false}   don't ask hcli: one group for all hcli plugins
  {"update_action": false}  no "Update plugins" entry when updates exist
"""

import ctypes
import json
import os
import sys
import threading
import time

import ida_diskio
import ida_idaapi
import ida_kernwin

try:
    from PySide6 import QtCore, QtGui, QtWidgets  # IDA 9.2+
except ImportError:
    from PyQt5 import QtCore, QtGui, QtWidgets  # IDA 9.0 and 9.1

PLUGIN_NAME = "Plugin Lens"
MENU_PREFIX = "Edit/Plugins/"
USER_DIR = "plugin-lens"
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".bmp")
DISABLED_EXTENSION = ".none"
SETTINGS_FILE = "settings.json"
DEFAULT_SETTINGS = {"group_menu": True, "show_versions": True, "hcli_sources": True, "update_action": True}
UPDATE_ACTION = "plugin_lens:update"
HCLI_SOURCES_FILE = "hcli-sources.json"
ICON_SIZE = 64  # rendered square; Qt scales it down to the menu's size

BUILTIN = "Built-in"
HCLI = "Installed with hcli"  # where from isn't known (yet)
HCLI_FILE = "hcli \u00b7 installed from a file"
MANUAL = "Installed manually"


# Unicode has superscript forms for the digits and most letters (not q)
_SUPERSCRIPT = str.maketrans(
    "0123456789abcdefghijklmnoprstuvwxyz.-+=()",
    "\u2070\u00b9\u00b2\u00b3\u2074\u2075\u2076\u2077\u2078\u2079"
    "\u1d43\u1d47\u1d9c\u1d48\u1d49\u1da0\u1d4d\u02b0\u2071\u02b2\u1d4f\u02e1\u1d50\u207f\u1d52\u1d56"
    "\u02b3\u02e2\u1d57\u1d58\u1d5b\u02b7\u02e3\u02b8\u1dbb"
    # "." -> U+0387: centred on the superscript digits in menu fonts, where
    # the usual middle dot (U+00B7) sits below them
    "\u0387\u207b\u207a\u207c\u207d\u207e",
)


def _superscript(text):
    """text in superscript characters, which look small next to normal text."""
    return text.lower().translate(_SUPERSCRIPT)


# the tags in superscript capitals: they all exist at one height, while the
# lowercase superscript l is short ("(manual)" read as "(manuaı)")
TAG_OUTDATED = "\u207d\u1d3c\u1d41\u1d40\u207b\u1d30\u1d2c\u1d40\u1d31\u1d30\u207e"  # (OUT-DATED)
TAG_MANUAL = "\u207d\u1d39\u1d2c\u1d3a\u1d41\u1d2c\u1d38\u207e"  # (MANUAL)


def _label(name, plugin_file, group, hcli_sources):
    """The menu text for a plugin: its name, version and a tag, if any.

    Built-in plugins: the name. Installed from a repository: the version,
    and "(out-dated)" if hcli has a newer one. Installed from a file or
    manually: the version (if known) and "(manual)".
    """
    if group == BUILTIN:
        return name
    parts = [name]
    folder = _plugin_folder(plugin_file)
    version = folder and _metadata(folder).get("version")
    if isinstance(version, str) and version:
        parts.append(_superscript(version))
    if group in (HCLI_FILE, MANUAL):
        parts.append(TAG_MANUAL)
    elif group != HCLI and hcli_sources:  # from a repository
        plugin = hcli_sources.get(folder and _metadata(folder).get("name"), {})
        if plugin.get("outdated"):
            parts.append(TAG_OUTDATED)
    return "  ".join(parts)


def _hcli_repo_group(repo):
    return f"hcli \u00b7 {repo} repository"


def _group_order(group):
    """Sort key: built-in, hcli repositories (hexrays first), hcli files, manual."""
    if group == BUILTIN:
        return (0, "")
    if group.startswith("hcli \u00b7 ") and group.endswith(" repository"):
        repo = group[len("hcli \u00b7 "):-len(" repository")]
        return (1, "" if repo == "hexrays" else repo.casefold())
    return {HCLI_FILE: (2, ""), HCLI: (3, ""), MANUAL: (4, "")}.get(group, (5, group))


class _PluginInfo(ctypes.Structure):
    """The first fields of the SDK's plugin_info_t (loader.hpp)."""


_PluginInfo._fields_ = [
    ("next", ctypes.POINTER(_PluginInfo)),
    ("path", ctypes.c_char_p),
    ("org_name", ctypes.c_char_p),
    ("name", ctypes.c_char_p),
]


def _plugin_paths():
    """{menu name: plugin file} for the plugins IDA knows.

    IDAPython doesn't expose get_plugins(), so it is called in IDA's own
    library. Returns {} if that fails: user icons still work then.
    """
    library = {"darwin": "libida.dylib", "win32": "ida.dll"}.get(sys.platform, "libida.so")
    try:
        ida = ctypes.CDLL(os.path.join(ida_diskio.idadir(""), library))
        ida.get_plugins.restype = ctypes.POINTER(_PluginInfo)
        info = ida.get_plugins()
    except (OSError, AttributeError):
        return {}

    paths = {}
    while info:
        plugin = info.contents
        if plugin.name and plugin.path:
            paths[plugin.name.decode("utf-8", "replace")] = plugin.path.decode("utf-8", "replace")
        info = plugin.next
    return paths


def _plugin_folder(plugin_file):
    """The folder with the ida-plugin.json of the plugin holding plugin_file.

    Looks in the file's folder and the folders above it, up to the plugins
    folder itself. A plugin directly in the plugins folder (an old-style
    single-file plugin, or one of IDA's own) has none.
    """
    folder = os.path.dirname(os.path.abspath(plugin_file))
    for _ in range(4):
        if os.path.basename(folder) == "plugins":
            return None
        if os.path.isfile(os.path.join(folder, "ida-plugin.json")):
            return folder
        parent = os.path.dirname(folder)
        if parent == folder:
            return None
        folder = parent
    return None


def _metadata(folder):
    """The "plugin" object of folder/ida-plugin.json ({} if unreadable)."""
    try:
        with open(os.path.join(folder, "ida-plugin.json"), encoding="utf-8") as f:
            plugin = json.load(f).get("plugin", {})
    except (OSError, ValueError, AttributeError):
        return {}
    return plugin if isinstance(plugin, dict) else {}


def _declared_logo(plugin_file):
    """The logo ("logoPath") from the plugin's ida-plugin.json, if any."""
    folder = _plugin_folder(plugin_file)
    if folder is None:
        return None
    logo = _metadata(folder).get("logoPath")
    if not isinstance(logo, str) or not logo:
        return None
    path = os.path.normpath(os.path.join(folder, logo))
    # only files inside the plugin's own folder
    if os.path.commonpath([path, folder]) != folder or not os.path.isfile(path):
        return None
    return path


def _group(plugin_file, hcli_sources):
    """The menu group of the plugin in plugin_file.

    hcli_sources: {hcli plugin name: {"repo": repository name, or None if
    installed from a file, "outdated": bool}}, or None when not known.
    """
    builtin = os.path.realpath(ida_diskio.idadir("plugins"))
    try:
        if os.path.commonpath([os.path.realpath(plugin_file), builtin]) == builtin:
            return BUILTIN
    except ValueError:  # e.g. on different drives
        pass
    # hcli installs plugins as folders described by an ida-plugin.json
    folder = _plugin_folder(plugin_file)
    if folder is None:
        return MANUAL
    name = _metadata(folder).get("name")
    if not hcli_sources or name not in hcli_sources:
        return HCLI
    repo = hcli_sources[name]["repo"]
    return _hcli_repo_group(repo) if repo else HCLI_FILE


def _hcli_sources_path():
    return os.path.join(ida_diskio.get_user_idadir(), USER_DIR, HCLI_SOURCES_FILE)


def _cached_hcli_sources():
    """The sources hcli reported last time (see _group()), or None."""
    try:
        with open(_hcli_sources_path(), encoding="utf-8") as f:
            plugins = dict(json.load(f)["plugins"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    # files from version 1.0 hold just the repository
    return {
        name: info if isinstance(info, dict) else {"repo": info, "outdated": False}
        for name, info in plugins.items()
    }


def _hcli_status_in_process():
    """What `hcli plugin status --json` lists, using hcli's own Python code.

    Works when IDA's Python can import hcli (not when hcli is installed in an
    isolated environment, e.g. with uv tool or pipx). collect_status_report()
    is part of hcli's command code rather than a stable API, so any failure
    just leaves the sources unknown.
    """
    from hcli.commands.plugin.status import collect_status_report
    from hcli.lib.ida import get_ida_config, get_plugin_repositories
    from hcli.lib.ida.plugin.repo.aggregate import AggregatePluginRepo

    repositories = get_plugin_repositories(get_ida_config())
    if not repositories:
        raise RuntimeError("no plugin repositories configured")
    aggregate = AggregatePluginRepo(repositories)
    report = collect_status_report(aggregate, (), False, repo_of=aggregate.repo_of)
    return report.model_dump(mode="json")["plugins"]


def _query_hcli_sources():
    """The sources (see _group()) that hcli reports, or None."""
    try:
        plugins = _hcli_status_in_process()
    except Exception:  # hcli not importable, an hcli version that differs, offline, ...
        return None
    return {
        p["name"]: {
            "repo": p.get("repo") if p.get("in_repository") else None,
            "outdated": bool(p.get("upgradable_to")),
            "version": p.get("version"),
            "latest": p.get("upgradable_to"),
        }
        for p in plugins
        if p.get("kind") == "installed" and p.get("name")
    }


def _save_hcli_sources(sources):
    try:
        os.makedirs(os.path.dirname(_hcli_sources_path()), exist_ok=True)
        with open(_hcli_sources_path(), "w", encoding="utf-8") as f:
            json.dump({"updated": time.time(), "plugins": sources}, f, indent=1, sort_keys=True)
    except OSError:
        pass


def _hcli_upgrade_in_process(name):
    """Run `hcli plugin upgrade <name>` through hcli's own Python code.

    Calling hcli's command (rather than its library functions) keeps all of
    its checks: staying with the repository the plugin came from, plugin
    suites, Python dependencies. Returns (succeeded, hcli's output).
    """
    import io

    from hcli.lib import console as hcli_console
    from hcli.main import cli

    output = io.StringIO()
    consoles = (hcli_console.console, hcli_console.stderr_console)
    saved = [c.file for c in consoles]
    for c in consoles:
        c.file = output  # hcli prints through these; keep its output for IDA's
    try:
        cli.main(
            ["--disable-updates", "plugin", "upgrade", name],
            prog_name="hcli",
            standalone_mode=False,  # return or raise, never exit
        )
        succeeded = True
    except BaseException as e:  # click's Abort, hcli errors, SystemExit
        if isinstance(e, KeyboardInterrupt):
            raise
        succeeded = False
        if str(e):
            output.write(f"\n{type(e).__name__}: {e}")
    finally:
        for c, file in zip(consoles, saved):
            c.file = file
    return succeeded, output.getvalue().strip()


class _UpdatesChooser(ida_kernwin.Choose):
    """The plugins hcli can update, all selected; the user picks which."""

    def __init__(self, rows):
        super().__init__(
            "Update plugins",
            [
                ["Plugin", 24 | ida_kernwin.Choose.CHCOL_PLAIN],
                ["Installed", 12 | ida_kernwin.Choose.CHCOL_PLAIN],
                ["Available", 12 | ida_kernwin.Choose.CHCOL_PLAIN],
            ],
            flags=ida_kernwin.Choose.CH_MULTI,
        )
        self.items = rows
        self.deflt = list(range(len(rows)))

    def OnGetSize(self):
        return len(self.items)

    def OnGetLine(self, n):
        return self.items[n]

    # Show() returns only one index; the selection comes from these. IDA
    # reports each change to it (not the final one on OK, and nothing if the
    # default selection is kept).
    def OnSelectionChange(self, sel):
        self.deflt = sel

    def OnSelectLine(self, sel):  # Enter or double-click
        self.deflt = sel
        return (ida_kernwin.Choose.NOTHING_CHANGED,)

    def selected(self):
        """The selected row indices, after Show()."""
        sel = self.deflt
        return [sel] if isinstance(sel, int) else list(sel)


class _UpdateHandler(ida_kernwin.action_handler_t):
    def __init__(self, hooks):
        super().__init__()
        self.hooks = hooks

    def activate(self, ctx):
        self.hooks.update_plugins()
        return 1

    def update(self, ctx):
        return ida_kernwin.AST_ENABLE_ALWAYS


def _settings():
    """DEFAULT_SETTINGS, updated from plugin-lens/settings.json if it exists."""
    settings = dict(DEFAULT_SETTINGS)
    path = os.path.join(ida_diskio.get_user_idadir(), USER_DIR, SETTINGS_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            settings.update({k: v for k, v in json.load(f).items() if k in DEFAULT_SETTINGS})
    except (OSError, ValueError, AttributeError):
        pass
    return settings


def _user_icon(menu_name):
    """(path, disabled) for the user's own icon for menu_name."""
    folder = os.path.join(ida_diskio.get_user_idadir(), USER_DIR)
    base = os.path.join(folder, menu_name.replace("/", "_").replace("\\", "_"))
    if os.path.exists(base + DISABLED_EXTENSION):
        return None, True
    for extension in IMAGE_EXTENSIONS:
        if os.path.isfile(base + extension):
            return base + extension, False
    return None, False


# icons shipped with this plugin: IDA's built-in plugins, and a few others
BUNDLED_ICONS_DIR = os.path.join(os.path.dirname(os.path.realpath(__file__)), "icons")


def _bundled_icon(menu_name):
    path = os.path.join(BUNDLED_ICONS_DIR, menu_name.replace("/", "_").replace("\\", "_") + ".png")
    return path if os.path.isfile(path) else None


def _square_png(path):
    """The image at path, centred on a transparent square, as PNG bytes."""
    image = QtGui.QImage(path)
    if image.isNull():
        return None

    # pad rather than crop, so a wide logo is shown whole
    side = max(image.width(), image.height())
    square = QtGui.QImage(side, side, QtGui.QImage.Format.Format_ARGB32)
    square.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(square)
    painter.drawImage((side - image.width()) // 2, (side - image.height()) // 2, image)
    painter.end()

    icon = square.scaled(
        ICON_SIZE,
        ICON_SIZE,
        QtCore.Qt.AspectRatioMode.KeepAspectRatio,
        QtCore.Qt.TransformationMode.SmoothTransformation,
    )
    data = QtCore.QByteArray()
    buffer = QtCore.QBuffer(data)
    buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
    icon.save(buffer, "PNG")
    buffer.close()
    return bytes(data)


class _MenuIcons(ida_kernwin.UI_Hooks):
    def __init__(self):
        super().__init__()
        self._icons = {}  # image path -> icon id (None if it can't be loaded)
        self._set = {}  # action name -> icon id set by this plugin
        self._seen = None  # the plugin actions handled last time
        self._plugin_paths = {}
        self._settings = dict(DEFAULT_SETTINGS)
        self._names = set()  # menu names of the plugins with an entry
        self._groups = {}  # menu name -> group, when grouping the menu
        self._labels = {}  # menu name -> text shown, when showing versions
        self._hcli_sources = _cached_hcli_sources()
        self._hcli_asked = False  # whether hcli was asked this session
        self._added = {}  # id(menu) -> the headers and separators added to it
        self._connected = set()  # ids of the menus styled again before each opening
        self._menu_retries = 100  # updates to wait for the menu to exist
        self._update_label = None  # the update entry's text, while it's shown

    def updated_actions(self):
        # called whenever IDA updates its actions: only act when the set of
        # plugin menu entries changed (e.g. when a database opens)
        actions = frozenset(
            name for name in ida_kernwin.get_registered_actions() if name.startswith(MENU_PREFIX)
        )
        if actions == self._seen:
            if (self._groups or self._labels or self._update_label) and not self._connected and self._menu_retries > 0:
                self._menu_retries -= 1
                self._style_menus()  # the menu didn't exist yet last time
            return
        self._seen = actions
        self.apply(actions)

    def apply(self, actions):
        plugin_paths = self._plugin_paths = _plugin_paths()
        settings = _settings()
        self._names = {action[len(MENU_PREFIX):] for action in actions} & set(plugin_paths)
        self._settings = settings
        self._update_menus()
        if (settings["group_menu"] or settings["show_versions"]) and settings["hcli_sources"]:
            self._refresh_hcli_sources()

        for action in sorted(actions):
            ok, current = ida_kernwin.get_action_icon(action)
            if ok and current not in (-1, 0, self._set.get(action)):
                continue  # the plugin set its own icon

            menu_name = action[len(MENU_PREFIX):]
            image, disabled = _user_icon(menu_name)
            if disabled:
                continue
            if image is None:
                plugin_file = plugin_paths.get(menu_name)
                image = (plugin_file and _declared_logo(plugin_file)) or _bundled_icon(menu_name)
            if not image:
                continue

            if image not in self._icons:
                data = _square_png(image)
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
                sources = _query_hcli_sources()
            except Exception:  # unexpected output
                sources = None
            if sources is not None:
                _save_hcli_sources(sources)

            def done():
                if sources is not None:
                    self._hcli_sources = sources
                    self._update_menus()
                return 0

            # back on IDA's main thread, which owns the menus
            ida_kernwin.execute_sync(done, ida_kernwin.MFF_FAST)

        threading.Thread(target=worker, name="plugin-lens-hcli", daemon=True).start()

    def _update_menus(self):
        """Work out each entry's group and text, and apply them to the menus."""
        settings = self._settings
        sources = self._hcli_sources if settings["hcli_sources"] else None
        groups = {name: _group(self._plugin_paths[name], sources) for name in self._names}
        self._groups = groups if settings["group_menu"] else {}
        self._labels = {}
        if settings["show_versions"]:
            for name, group in groups.items():
                label = _label(name, self._plugin_paths[name], group, sources)
                if label != name:
                    self._labels[name] = label
        self._update_action()
        self._style_menus()

    def _outdated(self):
        """[(hcli name, installed version, available version)] that hcli can update."""
        sources = self._hcli_sources if self._settings["hcli_sources"] else None
        return sorted(
            (name, info.get("version") or "", info["latest"])
            for name, info in (sources or {}).items()
            if info.get("outdated") and info.get("latest") and info.get("repo")
        )

    def _update_action(self):
        """Show "Update plugins (N)..." at the top of Edit > Plugins, if N > 0."""
        outdated = self._outdated() if self._settings["update_action"] else []
        if outdated:
            self._update_label = f"Update plugins ({len(outdated)})\u2026"
            ida_kernwin.update_action_label(UPDATE_ACTION, self._update_label)
            self._attach_update_entry()
        elif self._update_label is not None:
            self._update_label = None
            ida_kernwin.detach_action_from_menu(MENU_PREFIX, UPDATE_ACTION)

    def _attach_update_entry(self, menu=None):
        """Attach the update entry, unless menu already shows it.

        IDA replaces the Plugins menu (e.g. once a database is loaded), and
        actions attached to the old one are gone; so this is checked again
        each time the menu is styled.
        """
        if self._update_label is None:
            return
        if menu is not None:
            texts = {a.text().replace("&", "") for a in menu.actions()}
            if self._update_label in texts:
                return
        ida_kernwin.detach_action_from_menu(MENU_PREFIX, UPDATE_ACTION)
        ida_kernwin.attach_action_to_menu(MENU_PREFIX, UPDATE_ACTION, ida_kernwin.SETMENU_FIRST)

    def update_plugins(self):
        """Let the user pick plugins to update, and update them with hcli."""
        outdated = self._outdated()
        if not outdated:
            ida_kernwin.info("All plugins are up to date.")
            return
        chooser = _UpdatesChooser([list(row) for row in outdated])
        if chooser.Show(modal=True) < 0:
            return  # cancelled
        names = [outdated[i][0] for i in chooser.selected() if 0 <= i < len(outdated)]
        if not names:
            return

        results = []
        sources = None
        ida_kernwin.show_wait_box("Updating plugins\u2026")
        try:
            for i, name in enumerate(names, 1):
                if ida_kernwin.user_cancelled():
                    break
                ida_kernwin.replace_wait_box(f"Updating {name} ({i}/{len(names)})\u2026")
                try:
                    succeeded, output = _hcli_upgrade_in_process(name)
                except Exception as e:  # hcli not importable, or changed
                    succeeded, output = False, f"{type(e).__name__}: {e}"
                ida_kernwin.msg(f"[{PLUGIN_NAME}] hcli plugin upgrade {name}\n{output}\n")
                results.append((name, succeeded))
            ida_kernwin.replace_wait_box("Checking the installed versions\u2026")
            sources = _query_hcli_sources()
        finally:
            ida_kernwin.hide_wait_box()

        if sources is not None:
            self._hcli_sources = sources
            _save_hcli_sources(sources)
        self._update_menus()

        before = {name: version for name, version, _ in outdated}
        updated, failed = [], []
        for name, succeeded in results:
            now = (sources or {}).get(name, {}).get("version")
            if succeeded and now and now != before[name]:
                updated.append(f"{name} {before[name]} \u2192 {now}")
            else:
                failed.append(name)
        skipped = [name for name in names if name not in dict(results)]

        lines = []
        if updated:
            lines += ["Updated:"] + [f"  {line}" for line in updated] + ["", "Restart IDA to load the new versions."]
        if failed:
            lines += ["", "Not updated (see the Output window):"] + [f"  {name}" for name in failed]
        if skipped:
            lines += ["", "Cancelled before:"] + [f"  {name}" for name in skipped]
        ida_kernwin.info("\n".join(lines).strip() or "Nothing was updated.")

    def _name(self, action):
        """The plugin's menu name for an entry, whatever its text shows now."""
        text = action.text().replace("&&", "\0").replace("&", "").replace("\0", "&")
        if text in self._names:
            return text
        return self._label_names.get(text, text)

    @property
    def _label_names(self):
        return {label: name for name, label in self._labels.items()}

    def _style_menus(self):
        """Group and label the entries of every menu listing the plugins."""
        app = QtWidgets.QApplication.instance()
        if app is None or not (self._groups or self._labels or self._update_label):
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
            self._attach_update_entry(menu)
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
        for group in sorted(grouped, key=_group_order):
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

    def free(self):
        for icon in self._icons.values():
            if icon is not None:
                ida_kernwin.free_custom_icon(icon)
        self._icons.clear()


class PluginLens(ida_idaapi.plugin_t):
    flags = ida_idaapi.PLUGIN_FIX | ida_idaapi.PLUGIN_HIDE
    comment = "Icons, groups and versions for the entries in Edit > Plugins"
    help = "Uses each plugin's logo, or your own images in <IDA user directory>/plugin-lens/"
    wanted_name = PLUGIN_NAME
    wanted_hotkey = ""

    def init(self):
        if not ida_kernwin.is_idaq():
            return ida_idaapi.PLUGIN_SKIP  # no menus without the GUI
        self.hooks = _MenuIcons()
        ida_kernwin.register_action(ida_kernwin.action_desc_t(
            UPDATE_ACTION, "Update plugins\u2026", _UpdateHandler(self.hooks), "",
            "Update the plugins hcli has newer versions of", -1,
        ))
        self.hooks.hook()
        return ida_idaapi.PLUGIN_KEEP

    def run(self, arg):
        pass

    def term(self):
        hooks = getattr(self, "hooks", None)
        if hooks is not None:
            hooks.unhook()
            hooks.free()
        ida_kernwin.unregister_action(UPDATE_ACTION)


def PLUGIN_ENTRY():
    return PluginLens()
