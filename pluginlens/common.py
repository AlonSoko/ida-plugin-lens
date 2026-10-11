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
    "update_notice": True,
    "held_plugins": [],  # hcli names of the plugins not to update
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


def settings_path():
    return os.path.join(ida_diskio.get_user_idadir(), USER_DIR, SETTINGS_FILE)


def _settings_file():
    """What plugin-lens/settings.json holds ({} if missing or unreadable)."""
    try:
        with open(settings_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def settings():
    """DEFAULT_SETTINGS, updated from plugin-lens/settings.json if it exists."""
    result = {k: list(v) if isinstance(v, list) else v for k, v in DEFAULT_SETTINGS.items()}
    for key, value in _settings_file().items():
        if key in DEFAULT_SETTINGS and type(value) is type(DEFAULT_SETTINGS[key]):
            result[key] = value
    result["held_plugins"] = [n for n in result["held_plugins"] if isinstance(n, str)]
    return result


def save_settings(**changes):
    """Write changes to plugin-lens/settings.json, keeping what else it holds.
    Returns whether it could be written."""
    data = _settings_file()
    data.update(changes)
    try:
        os.makedirs(os.path.dirname(settings_path()), exist_ok=True)
        with open(settings_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, sort_keys=True)
            f.write("\n")
    except OSError:
        return False
    return True


def held():
    """The hcli names of the plugins the user holds at their version."""
    return set(settings()["held_plugins"])


def set_held(name, hold):
    """Hold (or stop holding) a plugin at its version."""
    names = held()
    (names.add if hold else names.discard)(name)
    return save_settings(held_plugins=sorted(names))


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


def _in_folder(path, folder):
    return bool(path) and os.path.normcase(os.path.dirname(os.path.realpath(path))) == os.path.normcase(
        os.path.realpath(folder))


def is_user_icon(path):
    """Whether path is one of the user's own icons (in <IDA user directory>/plugin-lens/)."""
    return _in_folder(path, os.path.join(ida_diskio.get_user_idadir(), USER_DIR))


def is_bundled_icon(path):
    """Whether path is one of the icons that come with Plugin Lens (icons/)."""
    return _in_folder(path, BUNDLED_ICONS_DIR)


USER_BADGE, BUNDLED_BADGE = "user", "bundled"


def badge_of(path):
    """USER_BADGE, BUNDLED_BADGE or None: whether an icon isn't the plugin's own."""
    return USER_BADGE if is_user_icon(path) else BUNDLED_BADGE if is_bundled_icon(path) else None


def with_badge(png, badge):
    """png (square_png()'s bytes) with a small badge in its bottom-right
    corner: USER_BADGE, a person on a blue dot (the user's own icon), or
    BUNDLED_BADGE, a magnifying glass on a teal dot (an icon that comes with
    Plugin Lens)."""
    image = QtGui.QImage.fromData(png)
    if image.isNull() or badge not in (USER_BADGE, BUNDLED_BADGE):
        return png
    side = image.width()
    size = side * 0.5
    painter = QtGui.QPainter(image)
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
    # a dark ring keeps the dot visible on any icon
    painter.setPen(QtGui.QPen(QtGui.QColor(20, 20, 20, 200), side * 0.04))
    painter.setBrush(QtGui.QColor(37, 99, 235) if badge == USER_BADGE else QtGui.QColor(13, 148, 136))
    inset = side * 0.02
    dot = QtCore.QRectF(side - size, side - size, size, size).adjusted(inset, inset, -inset, -inset)
    painter.drawEllipse(dot)
    cx, cy, r = dot.center().x(), dot.center().y(), dot.width() / 2
    white = QtGui.QColor(255, 255, 255)
    if badge == USER_BADGE:
        # a head, and shoulders cut by the dot
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(white)
        clip = QtGui.QPainterPath()
        clip.addEllipse(dot)
        painter.setClipPath(clip)
        painter.drawEllipse(QtCore.QPointF(cx, cy - r * 0.22), r * 0.3, r * 0.3)
        painter.drawEllipse(QtCore.QPointF(cx, cy + r * 0.78), r * 0.62, r * 0.55)
    else:
        # a lens and its handle
        pen = QtGui.QPen(white, r * 0.2)
        pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        lens = QtCore.QPointF(cx - r * 0.12, cy - r * 0.12)
        painter.drawEllipse(lens, r * 0.33, r * 0.33)
        painter.drawLine(QtCore.QPointF(cx + r * 0.13, cy + r * 0.13), QtCore.QPointF(cx + r * 0.45, cy + r * 0.45))
    painter.end()
    data = QtCore.QByteArray()
    buffer = QtCore.QBuffer(data)
    buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    buffer.close()
    return bytes(data)


def icon_note(path):
    """Where an icon comes from, when it isn't the plugin's own; else None."""
    if is_user_icon(path):
        return f"your own, {os.path.join(USER_DIR, os.path.basename(path))}"
    if is_bundled_icon(path):
        return f"from {PLUGIN_NAME} (the plugin has no icon of its own)"
    return None


def badged_png(path):
    """The image at path as square_png(), with a badge (with_badge()) if it
    isn't the plugin's own icon; None if it can't be read."""
    data = square_png(path)
    badge = badge_of(path) if data else None
    return with_badge(data, badge) if badge else data
