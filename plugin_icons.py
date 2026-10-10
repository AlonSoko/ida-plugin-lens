"""Plugin Icons: icons and groups for IDA's Edit > Plugins menu.

Icons: every entry without an icon gets, in order,
1. an image you put in <IDA user directory>/plugin-icons/, named after the
   menu entry, e.g. "Diaphora.png" (an empty "Diaphora.none" leaves that
   entry without an icon);
2. the logo the plugin declares in its ida-plugin.json ("logoPath").
Icons a plugin sets itself are left alone.

Groups: the entries are sorted into IDA's own plugins, plugins installed with
hcli (those with an ida-plugin.json) and plugins installed manually, each
under a header. Turn this off with {"group_menu": false} in
<IDA user directory>/plugin-icons/settings.json.
"""

import ctypes
import json
import os
import sys

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
DEFAULT_SETTINGS = {"group_menu": True}
ICON_SIZE = 64  # rendered square; Qt scales it down to the menu's size

BUILTIN, HCLI, MANUAL = "Built-in", "Installed with hcli", "Installed manually"
GROUPS = (BUILTIN, HCLI, MANUAL)


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


def _declared_logo(plugin_file):
    """The logo ("logoPath") from the plugin's ida-plugin.json, if any."""
    folder = _plugin_folder(plugin_file)
    if folder is None:
        return None
    try:
        with open(os.path.join(folder, "ida-plugin.json"), encoding="utf-8") as f:
            logo = json.load(f).get("plugin", {}).get("logoPath")
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(logo, str) or not logo:
        return None
    path = os.path.normpath(os.path.join(folder, logo))
    # only files inside the plugin's own folder
    if os.path.commonpath([path, folder]) != folder or not os.path.isfile(path):
        return None
    return path


def _group(plugin_file):
    """BUILTIN, HCLI or MANUAL for the plugin in plugin_file."""
    builtin = os.path.realpath(ida_diskio.idadir("plugins"))
    try:
        if os.path.commonpath([os.path.realpath(plugin_file), builtin]) == builtin:
            return BUILTIN
    except ValueError:  # e.g. on different drives
        pass
    # hcli installs plugins as folders described by an ida-plugin.json
    return HCLI if _plugin_folder(plugin_file) else MANUAL


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
        plugin_paths = _plugin_paths()
        names = {action[len(MENU_PREFIX):] for action in actions}
        if _settings()["group_menu"] and plugin_paths:
            self._groups = {name: _group(plugin_paths[name]) for name in names if name in plugin_paths}
            self._group_menus()

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

            others, grouped = [], {group: [] for group in GROUPS}
            for action in menu.actions():
                group = self._groups.get(_menu_name(action))
                if group is None:
                    others.append(action)
                else:
                    grouped[group].append(action)
            for action in menu.actions():
                if self._groups.get(_menu_name(action)):
                    menu.removeAction(action)

            # a separator before each header, reusing one the menu already ends with
            separate = bool(others) and not others[-1].isSeparator()
            added = []
            for group in GROUPS:
                if not grouped[group]:
                    continue
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
