"""What the rest of Plugin Lens shares: settings, IDA's plugins, icons, groups."""

import ctypes
import json
import os
import sys

import ida_diskio

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
DEFAULT_SETTINGS = {
    "group_menu": True, "show_versions": True, "hcli_sources": True, "update_action": True, "fetch_logos": True,
}
ICON_SIZE = 64  # rendered square; Qt scales it down to the menu's size

BUILTIN = "Built-in"
HCLI = "Installed with hcli"  # where from isn't known (yet)
HCLI_FILE = "hcli · installed from a file"
MANUAL = "Installed manually"

# icons shipped with this plugin: IDA's built-in plugins, and a few others
BUNDLED_ICONS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "icons")


# Unicode has superscript forms for the digits and most letters (not q)
_SUPERSCRIPT = str.maketrans(
    "0123456789abcdefghijklmnoprstuvwxyz.-+=()",
    "⁰¹²³⁴⁵⁶⁷⁸⁹"
    "ᵃᵇᶜᵈᵉᶠᵍʰⁱʲᵏˡᵐⁿᵒᵖ"
    "ʳˢᵗᵘᵛʷˣʸᶻ"
    # "." -> U+0387: centred on the superscript digits in menu fonts, where
    # the usual middle dot (U+00B7) sits below them
    "·⁻⁺⁼⁽⁾",
)


def superscript(text):
    """text in superscript characters, which look small next to normal text."""
    return text.lower().translate(_SUPERSCRIPT)


# the tags in superscript capitals: they all exist at one height, while the
# lowercase superscript l is short ("(manual)" read as "(manuaı)")
TAG_OUTDATED = "⁽ᴼᵁᵀ⁻ᴰᴬᵀᴱᴰ⁾"  # (OUT-DATED)
TAG_MANUAL = "⁽ᴹᴬᴺᵁᴬᴸ⁾"  # (MANUAL)


def settings():
    """DEFAULT_SETTINGS, updated from plugin-lens/settings.json if it exists."""
    result = dict(DEFAULT_SETTINGS)
    path = os.path.join(ida_diskio.get_user_idadir(), USER_DIR, SETTINGS_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            result.update({k: v for k, v in json.load(f).items() if k in DEFAULT_SETTINGS})
    except (OSError, ValueError, AttributeError):
        pass
    return result


class _PluginInfo(ctypes.Structure):
    """The first fields of the SDK's plugin_info_t (loader.hpp)."""


_PluginInfo._fields_ = [
    ("next", ctypes.POINTER(_PluginInfo)),
    ("path", ctypes.c_char_p),
    ("org_name", ctypes.c_char_p),
    ("name", ctypes.c_char_p),
]


def plugin_paths():
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


def plugin_folder(plugin_file):
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


def metadata(folder):
    """The "plugin" object of folder/ida-plugin.json ({} if unreadable)."""
    try:
        with open(os.path.join(folder, "ida-plugin.json"), encoding="utf-8") as f:
            plugin = json.load(f).get("plugin", {})
    except (OSError, ValueError, AttributeError):
        return {}
    return plugin if isinstance(plugin, dict) else {}


def hcli_folders():
    """{plugin name: folder}: hcli installs each plugin as a folder, with an
    ida-plugin.json, in the user's plugins folder."""
    user_plugins = os.path.join(ida_diskio.get_user_idadir(), "plugins")
    folders = {}
    if os.path.isdir(user_plugins):
        for entry in sorted(os.listdir(user_plugins)):
            folder = os.path.join(user_plugins, entry)
            if os.path.isfile(os.path.join(folder, "ida-plugin.json")):
                name = metadata(folder).get("name")
                if name:
                    folders[name] = folder
    return folders


def declared_logo(folder):
    """The logo ("logoPath") from folder/ida-plugin.json, if any."""
    if folder is None:
        return None
    logo = metadata(folder).get("logoPath")
    if not isinstance(logo, str) or not logo:
        return None
    path = os.path.normpath(os.path.join(folder, logo))
    # only files inside the plugin's own folder
    if os.path.commonpath([path, folder]) != folder or not os.path.isfile(path):
        return None
    return path


def is_builtin(plugin_file):
    """Whether plugin_file is one of IDA's own plugins."""
    builtin = os.path.realpath(ida_diskio.idadir("plugins"))
    try:
        return os.path.commonpath([os.path.realpath(plugin_file), builtin]) == builtin
    except ValueError:  # e.g. on different drives
        return False


def hcli_repo_group(repo):
    return f"hcli · {repo} repository"


def group_order(group):
    """Sort key: built-in, hcli repositories (hexrays first), hcli files, manual."""
    if group == BUILTIN:
        return (0, "")
    if group.startswith("hcli · ") and group.endswith(" repository"):
        repo = group[len("hcli · "):-len(" repository")]
        return (1, "" if repo == "hexrays" else repo.casefold())
    return {HCLI_FILE: (2, ""), HCLI: (3, ""), MANUAL: (4, "")}.get(group, (5, group))


def group_of(plugin_file, hcli_sources):
    """The group of the plugin in plugin_file.

    hcli_sources: {hcli plugin name: {"repo": repository name, or None if
    installed from a file, "outdated": bool, ...}}, or None when not known.
    """
    if is_builtin(plugin_file):
        return BUILTIN
    # hcli installs plugins as folders described by an ida-plugin.json
    folder = plugin_folder(plugin_file)
    if folder is None:
        return MANUAL
    return hcli_group(metadata(folder).get("name"), hcli_sources)


def hcli_group(name, hcli_sources):
    """The group of the hcli plugin called name."""
    if not hcli_sources or name not in hcli_sources:
        return HCLI
    repo = hcli_sources[name]["repo"]
    return hcli_repo_group(repo) if repo else HCLI_FILE


def label(name, plugin_file, group, hcli_sources):
    """The menu text for a plugin: its name, version and a tag, if any.

    Built-in plugins: the name. Installed from a repository: the version,
    and "(out-dated)" if hcli has a newer one. Installed from a file or
    manually: the version (if known) and "(manual)".
    """
    if group == BUILTIN:
        return name
    parts = [name]
    folder = plugin_folder(plugin_file)
    version = folder and metadata(folder).get("version")
    if isinstance(version, str) and version:
        parts.append(superscript(version))
    if group in (HCLI_FILE, MANUAL):
        parts.append(TAG_MANUAL)
    elif group != HCLI and hcli_sources:  # from a repository
        plugin = hcli_sources.get(folder and metadata(folder).get("name"), {})
        if plugin.get("outdated"):
            parts.append(TAG_OUTDATED)
    return "  ".join(parts)


def user_icon(menu_name):
    """(path, disabled) for the user's own icon for menu_name."""
    folder = os.path.join(ida_diskio.get_user_idadir(), USER_DIR)
    base = os.path.join(folder, menu_name.replace("/", "_").replace("\\", "_"))
    if os.path.exists(base + DISABLED_EXTENSION):
        return None, True
    for extension in IMAGE_EXTENSIONS:
        if os.path.isfile(base + extension):
            return base + extension, False
    return None, False


def bundled_icon(menu_name):
    path = os.path.join(BUNDLED_ICONS_DIR, menu_name.replace("/", "_").replace("\\", "_") + ".png")
    return path if os.path.isfile(path) else None


def icon_image(menu_name, plugin_file):
    """The image to show for a plugin: the user's, its logo, or a bundled one."""
    image, disabled = user_icon(menu_name)
    if disabled:
        return None
    if image is None:
        logo = plugin_file and declared_logo(plugin_folder(plugin_file))
        image = logo or bundled_icon(menu_name)
    return image


def square_png(path):
    """The image at path (or image data, as bytes), centred on a transparent
    square, as PNG bytes; None if it isn't an image Qt can read."""
    image = QtGui.QImage.fromData(path) if isinstance(path, bytes) else QtGui.QImage(path)
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
