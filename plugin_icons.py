"""Plugin Icons: icons and groups for IDA's Edit > Plugins menu.

Icons: every entry without an icon gets, in order,
1. an image you put in <IDA user directory>/plugin-icons/, named after the
   menu entry, e.g. "Diaphora.png" (an empty "Diaphora.none" leaves that
   entry without an icon);
2. the logo the plugin declares in its ida-plugin.json ("logoPath").
Icons a plugin sets itself are left alone.

Groups: the entries are sorted into IDA's own plugins, plugins installed with
hcli (those with an ida-plugin.json) and plugins installed manually, each
under a header. Plugins installed with hcli are split by where hcli got them
(a plugin repository, or a local file), as `hcli plugin status --json`
reports; that runs in the background and is cached for a day.

Settings, in <IDA user directory>/plugin-icons/settings.json:
  {"group_menu": false}     keep IDA's own order
  {"hcli_sources": false}   don't run hcli: one group for all hcli plugins
  {"hcli_path": "..."}      the hcli executable, if it isn't found
"""

import ctypes
import json
import os
import shutil
import subprocess
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

PLUGIN_NAME = "Plugin Icons"
MENU_PREFIX = "Edit/Plugins/"
USER_DIR = "plugin-icons"
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".bmp")
DISABLED_EXTENSION = ".none"
SETTINGS_FILE = "settings.json"
DEFAULT_SETTINGS = {"group_menu": True, "hcli_sources": True, "hcli_path": None}
HCLI_SOURCES_FILE = "hcli-sources.json"
HCLI_SOURCES_MAX_AGE = 24 * 60 * 60  # seconds
ICON_SIZE = 64  # rendered square; Qt scales it down to the menu's size

BUILTIN = "Built-in"
HCLI = "Installed with hcli"  # where from isn't known (yet)
HCLI_FILE = "hcli \u00b7 installed from a file"
MANUAL = "Installed manually"


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

    hcli_sources: {hcli plugin name: repository name, or None if installed
    from a file}, or None when not known.
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
    repo = hcli_sources[name]
    return _hcli_repo_group(repo) if repo else HCLI_FILE


def _hcli_sources_path():
    return os.path.join(ida_diskio.get_user_idadir(), USER_DIR, HCLI_SOURCES_FILE)


def _cached_hcli_sources():
    """(sources, updated time) from the cache, or (None, 0)."""
    try:
        with open(_hcli_sources_path(), encoding="utf-8") as f:
            cache = json.load(f)
        return dict(cache["plugins"]), float(cache["updated"])
    except (OSError, ValueError, KeyError, TypeError):
        return None, 0.0


def _hcli_executable(configured):
    if configured:
        return configured if os.path.isfile(configured) else None
    # IDA started from the Dock or Finder doesn't get the shell's PATH
    extra = [os.path.expanduser("~/.local/bin"), "/opt/homebrew/bin", "/usr/local/bin"]
    search = os.pathsep.join([os.environ.get("PATH", "")] + extra)
    return shutil.which("hcli", path=search)


def _query_hcli_sources(hcli):
    """{plugin name: repository or None} from `hcli plugin status --json`."""
    result = subprocess.run(
        [hcli, "plugin", "status", "--json"],
        capture_output=True, text=True, timeout=120,
        # no console window on Windows
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    plugins = json.loads(result.stdout)["plugins"]
    return {
        p["name"]: (p.get("repo") if p.get("in_repository") else None)
        for p in plugins
        if p.get("kind") == "installed" and p.get("name")
    }


def _settings():
    """DEFAULT_SETTINGS, updated from plugin-icons/settings.json if it exists."""
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


def _menu_name(action):
    return action.text().replace("&", "")


class _MenuIcons(ida_kernwin.UI_Hooks):
    def __init__(self):
        super().__init__()
        self._icons = {}  # image path -> icon id (None if it can't be loaded)
        self._set = {}  # action name -> icon id set by this plugin
        self._seen = None  # the plugin actions handled last time
        self._groups = {}  # menu name -> group, when grouping the menu
        self._plugin_paths = {}
        self._hcli_sources, self._hcli_updated = _cached_hcli_sources()
        self._hcli_running = False
        self._hcli_asked = False  # whether hcli was asked this session
        self._added = {}  # id(menu) -> the headers and separators added to it
        self._connected = set()  # ids of the menus regrouped before each opening
        self._menu_retries = 100  # updates to wait for the menu to exist

    def updated_actions(self):
        # called whenever IDA updates its actions: only act when the set of
        # plugin menu entries changed (e.g. when a database opens)
        actions = frozenset(
            name for name in ida_kernwin.get_registered_actions() if name.startswith(MENU_PREFIX)
        )
        if actions == self._seen:
            if self._groups and not self._added and self._menu_retries > 0:
                self._menu_retries -= 1
                self._group_menus()  # the menu didn't exist yet last time
            return
        self._seen = actions
        self.apply(actions)

    def apply(self, actions):
        plugin_paths = self._plugin_paths = _plugin_paths()
        settings = _settings()
        if settings["group_menu"] and plugin_paths:
            sources = self._hcli_sources if settings["hcli_sources"] else None
            self._update_groups(sources)
            if settings["hcli_sources"]:
                self._refresh_hcli_sources(settings["hcli_path"])

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
                image = plugin_file and _declared_logo(plugin_file)
            if not image:
                continue

            if image not in self._icons:
                data = _square_png(image)
                icon = ida_kernwin.load_custom_icon(data=data, format="png") if data else 0
                self._icons[image] = icon if icon and icon > 0 else None
            icon = self._icons[image]
            if icon is not None and ida_kernwin.update_action_icon(action, icon):
                self._set[action] = icon

    def _update_groups(self, hcli_sources):
        names = {action[len(MENU_PREFIX):] for action in (self._seen or ())}
        self._groups = {
            name: _group(self._plugin_paths[name], hcli_sources)
            for name in names
            if name in self._plugin_paths
        }
        self._group_menus()

    def _refresh_hcli_sources(self, hcli_path):
        """Ask hcli where its plugins come from, in the background, if needed."""
        # no cache counts as stale; a plugin missing from the cache (e.g.
        # installed since) is asked about once per session
        stale = time.time() - self._hcli_updated > HCLI_SOURCES_MAX_AGE
        unknown = HCLI in self._groups.values() and not self._hcli_asked
        if self._hcli_running or not (stale or unknown):
            return
        hcli = _hcli_executable(hcli_path)
        if hcli is None:
            return
        self._hcli_running = self._hcli_asked = True

        def worker():
            try:
                sources = _query_hcli_sources(hcli)
            except Exception:  # hcli missing, offline, unexpected output, ...
                sources = None
            if sources is not None:
                try:
                    os.makedirs(os.path.dirname(_hcli_sources_path()), exist_ok=True)
                    with open(_hcli_sources_path(), "w", encoding="utf-8") as f:
                        json.dump({"updated": time.time(), "plugins": sources}, f, indent=1, sort_keys=True)
                except OSError:
                    pass

            def done():
                self._hcli_running = False
                if sources is not None:
                    self._hcli_sources, self._hcli_updated = sources, time.time()
                    self._update_groups(sources)
                return 0

            # back on IDA's main thread, which owns the menus
            ida_kernwin.execute_sync(done, ida_kernwin.MFF_FAST)

        threading.Thread(target=worker, name="plugin-icons-hcli", daemon=True).start()

    def _group_menus(self):
        """Group the entries of every menu listing the plugins."""
        app = QtWidgets.QApplication.instance()
        if app is None or not self._groups:
            return
        for widget in app.allWidgets():
            try:
                if not isinstance(widget, QtWidgets.QMenu):
                    continue
                if not any(_menu_name(a) in self._groups for a in widget.actions()):
                    continue
                if id(widget) not in self._connected:
                    # IDA may rebuild the items: group again before each opening
                    self._connected.add(id(widget))
                    widget.aboutToShow.connect(lambda menu=widget: self._group(menu))
                self._group(widget)
            except RuntimeError:  # a widget Qt has deleted meanwhile
                continue

    def _group(self, menu):
        """Reorder menu's plugin entries into the groups, each under a header.

        Entries that aren't plugins (e.g. "Quick run plugins") stay first, in
        their order; the plugin entries follow, alphabetically per group.
        """
        try:
            for added in self._added.pop(id(menu), []):
                menu.removeAction(added)

            others, grouped = [], {}
            for action in menu.actions():
                group = self._groups.get(_menu_name(action))
                if group is None:
                    others.append(action)
                else:
                    grouped.setdefault(group, []).append(action)
            for action in menu.actions():
                if self._groups.get(_menu_name(action)):
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
                for action in sorted(grouped[group], key=lambda a: _menu_name(a).casefold()):
                    menu.addAction(action)
            self._added[id(menu)] = added
        except RuntimeError:  # the menu was deleted
            self._added.pop(id(menu), None)

    def free(self):
        for icon in self._icons.values():
            if icon is not None:
                ida_kernwin.free_custom_icon(icon)
        self._icons.clear()


class PluginIcons(ida_idaapi.plugin_t):
    flags = ida_idaapi.PLUGIN_FIX | ida_idaapi.PLUGIN_HIDE
    comment = "Icons and groups for the entries in Edit > Plugins"
    help = "Uses each plugin's logo, or your own images in <IDA user directory>/plugin-icons/"
    wanted_name = PLUGIN_NAME
    wanted_hotkey = ""

    def init(self):
        if not ida_kernwin.is_idaq():
            return ida_idaapi.PLUGIN_SKIP  # no menus without the GUI
        self.hooks = _MenuIcons()
        self.hooks.hook()
        return ida_idaapi.PLUGIN_KEEP

    def run(self, arg):
        pass

    def term(self):
        hooks = getattr(self, "hooks", None)
        if hooks is not None:
            hooks.unhook()
            hooks.free()


def PLUGIN_ENTRY():
    return PluginIcons()
