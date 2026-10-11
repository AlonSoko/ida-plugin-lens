"""Disabling plugins without uninstalling them.

IDA loads every plugin in its plugins folders, and hcli finds the plugins it
installed by their folder there. A disabled plugin is moved, whole, out of
<IDA user directory>/plugins/ into <IDA user directory>/plugin-lens/disabled/,
and moved back when enabled. Neither IDA nor hcli sees it meanwhile: it can't
be updated or uninstalled until it's enabled. It's unloaded, or loaded, when
IDA next starts.
"""

import json
import os
import shutil
import time

import ida_diskio

from .common import USER_DIR

DISABLED_DIR = "disabled"
MANIFEST = "disabled.json"

# what was disabled or enabled this session: path in plugins/ -> True (disabled) or False
changed_this_session = {}


def plugins_dir():
    return os.path.join(ida_diskio.get_user_idadir(), "plugins")


def disabled_dir():
    return os.path.join(ida_diskio.get_user_idadir(), USER_DIR, DISABLED_DIR)


def _manifest_path():
    return os.path.join(disabled_dir(), MANIFEST)


def disabled():
    """{entry name in disabled/: {"name", "hcli_name", "source", "from", "disabled"}},
    for the entries that are there."""
    try:
        with open(_manifest_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items()
            if isinstance(v, dict) and os.path.lexists(os.path.join(disabled_dir(), k))}


def _save(manifest):
    os.makedirs(disabled_dir(), exist_ok=True)
    with open(_manifest_path(), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1, sort_keys=True)
        f.write("\n")


def can_disable(path):
    """Whether path is an entry of the user's plugins folder (not IDA's)."""
    if not path or not os.path.lexists(path):
        return False
    return os.path.dirname(os.path.abspath(path.rstrip(os.sep))) == os.path.abspath(plugins_dir())


def disable(path, name, hcli_name=None, source=""):
    """Move the plugin entry at path (a folder or a file in plugins/) to
    disabled/. Returns None, or what went wrong."""
    if not can_disable(path):
        return f"{path} isn't in {plugins_dir()}."
    entry = os.path.basename(path.rstrip(os.sep))
    target = os.path.join(disabled_dir(), entry)
    if os.path.lexists(target):
        return f"{target} already exists: remove it first."
    manifest = disabled()
    try:
        os.makedirs(disabled_dir(), exist_ok=True)
        shutil.move(path, target)
    except OSError as e:  # e.g. a file IDA keeps open, on Windows
        return f"Couldn't move {path}: {e}"
    manifest[entry] = {"name": name, "hcli_name": hcli_name, "source": source,
                       "from": os.path.abspath(path), "disabled": time.strftime("%Y-%m-%d %H:%M")}
    try:
        _save(manifest)
    except OSError as e:
        shutil.move(target, path)  # undo: nothing would remember it
        return f"Couldn't save {_manifest_path()}: {e}"
    _changed(os.path.abspath(path), True)
    return None


def enable(entry):
    """Move a disabled entry back to plugins/. Returns None, or what went wrong."""
    manifest = disabled()
    info = manifest.get(entry)
    if info is None:
        return f"{entry} isn't disabled."
    source = os.path.join(disabled_dir(), entry)
    target = os.path.join(plugins_dir(), entry)
    if os.path.lexists(target):
        return (f"{target} exists (installed again meanwhile?): remove one of the two, "
                f"e.g. {source}, before enabling it.")
    try:
        shutil.move(source, target)
    except OSError as e:
        return f"Couldn't move {source}: {e}"
    del manifest[entry]
    try:
        _save(manifest)
    except OSError:
        pass  # the entry is back in plugins/; disabled() ignores what's gone
    _changed(os.path.abspath(target), False)
    return None


def _changed(path, off):
    """Remember a change until IDA restarts; undoing one made this session
    leaves the plugin as IDA loaded it."""
    if changed_this_session.get(path) is (not off):
        del changed_this_session[path]
    else:
        changed_this_session[path] = off
