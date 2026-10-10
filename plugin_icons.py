"""Plugin Icons: give plugins' entries in IDA's Edit > Plugins menu an icon.

For every entry without an icon, the icon is, in order:
1. an image you put in <IDA user directory>/plugin-icons/, named after the
   menu entry, e.g. "Diaphora.png" (an empty "Diaphora.none" leaves that
   entry without an icon);
2. the logo the plugin declares in its ida-plugin.json ("logoPath").

Icons a plugin sets itself are left alone.
"""

import ctypes
import json
import os
import sys

import ida_diskio
import ida_idaapi
import ida_kernwin

try:
    from PySide6 import QtCore, QtGui  # IDA 9.2+
except ImportError:
    from PyQt5 import QtCore, QtGui  # IDA 9.0 and 9.1

PLUGIN_NAME = "Plugin Icons"
MENU_PREFIX = "Edit/Plugins/"
USER_ICONS_DIR = "plugin-icons"
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".bmp")
DISABLED_EXTENSION = ".none"
ICON_SIZE = 64  # rendered square; Qt scales it down to the menu's size


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


def _declared_logo(plugin_file):
    """The logo from the ida-plugin.json of the plugin folder holding plugin_file.

    Looks in the file's folder and the folders above it, up to the plugins
    folder itself. A plugin directly in the plugins folder (an old-style
    single-file plugin, or one of IDA's own) has no ida-plugin.json.
    """
    folder = os.path.dirname(os.path.abspath(plugin_file))
    for _ in range(4):
        if os.path.basename(folder) == "plugins":
            return None
        metadata = os.path.join(folder, "ida-plugin.json")
        if os.path.isfile(metadata):
            try:
                with open(metadata, encoding="utf-8") as f:
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
        parent = os.path.dirname(folder)
        if parent == folder:
            return None
        folder = parent
    return None


def _user_icon(menu_name):
    """(path, disabled) for the user's own icon for menu_name."""
    folder = os.path.join(ida_diskio.get_user_idadir(), USER_ICONS_DIR)
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


class _MenuIcons(ida_kernwin.UI_Hooks):
    def __init__(self):
        super().__init__()
        self._icons = {}  # image path -> icon id (None if it can't be loaded)
        self._set = {}  # action name -> icon id set by this plugin
        self._seen = None  # the plugin actions handled last time

    def updated_actions(self):
        # called whenever IDA updates its actions: only act when the set of
        # plugin menu entries changed (e.g. when a database opens)
        actions = frozenset(
            name for name in ida_kernwin.get_registered_actions() if name.startswith(MENU_PREFIX)
        )
        if actions == self._seen:
            return
        self._seen = actions
        self.apply(actions)

    def apply(self, actions):
        plugin_paths = None
        for action in sorted(actions):
            ok, current = ida_kernwin.get_action_icon(action)
            if ok and current not in (-1, 0, self._set.get(action)):
                continue  # the plugin set its own icon

            menu_name = action[len(MENU_PREFIX):]
            image, disabled = _user_icon(menu_name)
            if disabled:
                continue
            if image is None:
                if plugin_paths is None:
                    plugin_paths = _plugin_paths()
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

    def free(self):
        for icon in self._icons.values():
            if icon is not None:
                ida_kernwin.free_custom_icon(icon)
        self._icons.clear()


class PluginIcons(ida_idaapi.plugin_t):
    flags = ida_idaapi.PLUGIN_FIX | ida_idaapi.PLUGIN_HIDE
    comment = "Icons for the entries in Edit > Plugins"
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
