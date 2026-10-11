"""Exporting the list of installed plugins to a file, and installing from one."""

import json
import os
import time

import ida_kernwin

from . import hcli, operations
from .common import MANUAL, PLUGIN_NAME, QtWidgets, hcli_folders, held, metadata, set_held
from .menu import CheckChooser

FORMAT = "plugin-lens-list"
FORMAT_VERSION = 1


def _host_key(url):
    url = (url or "").strip().lower().rstrip("/")
    return url[:-4] if url.endswith(".git") else url


def build_list(status_entries, library_rows, catalog=None):
    """The data to export: hcli's plugins from a repository (installable from
    the list), and the others (listed only)."""
    folders = hcli_folders()
    held_names = held()
    catalog_plugins = (catalog or {}).get("plugins") or []
    plugins = []
    for entry in status_entries or []:
        if entry.get("kind") != "installed" or not entry.get("name"):
            continue
        name = entry["name"]
        item = {"name": name, "version": entry.get("version") or ""}
        if entry.get("in_repository") and entry.get("repo"):
            item.update(source="repository", repo=entry["repo"])
            # the GitHub host, when the repository has two plugins of this name
            same = [p for p in catalog_plugins if p.get("name") == name and p.get("repo") == entry["repo"]]
            urls = metadata(folders[name]).get("urls") if name in folders else None
            host = _host_key(urls.get("repository")) if isinstance(urls, dict) else ""
            if len(same) > 1:
                item["host"] = next((p["host"] for p in same if _host_key(p.get("host")) == host), "")
            if name in held_names:
                item["held"] = True
        else:
            item["source"] = "file"
        plugins.append(item)
    for row in library_rows:
        if row.source == MANUAL and row.location:
            plugins.append({"name": row.name, "source": "manual", "file": os.path.basename(row.location)})
    return {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "exported": time.strftime("%Y-%m-%d %H:%M"),
        "ida_version": (catalog or {}).get("ida_version"),
        "plugins": sorted(plugins, key=lambda p: (p["source"] != "repository", p["name"].casefold())),
    }


def export_list(parent, status_entries, library_rows, catalog=None):
    if status_entries is None:
        ida_kernwin.warning("hcli's list of installed plugins isn't known yet: try again in a moment.")
        return
    data = build_list(status_entries, library_rows, catalog)
    default = os.path.join(os.path.expanduser("~"), f"ida-plugins-{time.strftime('%Y-%m-%d')}.json")
    path, _ = QtWidgets.QFileDialog.getSaveFileName(parent, "Export the plugin list", default, "JSON (*.json)")
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, ensure_ascii=False)
            f.write("\n")
    except OSError as e:
        ida_kernwin.warning(f"Couldn't save the list: {e}")
        return
    counts = {s: sum(p["source"] == s for p in data["plugins"]) for s in ("repository", "file", "manual")}
    ida_kernwin.info(
        f"Saved {len(data['plugins'])} plugins to {path}:\n"
        f"  {counts['repository']} from a repository (Import list… installs them),\n"
        f"  {counts['file']} installed from a file and {counts['manual']} installed manually (listed only)."
    )


def import_plan(data, status_entries, catalog=None):
    """(to_install, already, listed_only) for a list read from a file.

    to_install: [(name, version to install, note, hcli reference)];
    already: names installed already; listed_only: [text] of the plugins
    that the list can't install (from a file, manual).
    """
    installed = {p.get("name") for p in status_entries or [] if p.get("kind") == "installed"}
    catalog_plugins = (catalog or {}).get("plugins")
    to_install, already, listed_only = [], [], []
    for item in data.get("plugins") or []:
        name = item.get("name")
        if not name:
            continue
        if item.get("source") != "repository":
            where = "installed from a file" if item.get("source") == "file" else f"installed manually ({item.get('file')})"
            listed_only.append(f"{name}: {where}")
            continue
        if name in installed:
            already.append(name)
            continue
        version, note = item.get("version") or None, ""
        if catalog_plugins is not None:
            matches = [p for p in catalog_plugins if p.get("name") == name and p.get("repo") == item.get("repo")
                       and (not item.get("host") or _host_key(p.get("host")) == _host_key(item["host"]))]
            if not matches:
                listed_only.append(f"{name}: not in the {item.get('repo')} repository any more")
                continue
            compatible = {v["version"] for v in matches[0].get("versions") or [] if v.get("compatible")}
            if version not in compatible:
                latest = matches[0].get("latest_compatible")
                if not latest:
                    listed_only.append(f"{name}: no version for this IDA")
                    continue
                note = f"exported {version}, not available here" if version else ""
                version = latest
        reference = hcli.install_reference(name, item.get("repo"), item.get("host") or None, version)
        to_install.append((name, version or "latest", note, reference))
    return to_install, already, listed_only


def import_list(parent, status_entries, catalog, on_status):
    path, _ = QtWidgets.QFileDialog.getOpenFileName(parent, "Import a plugin list", os.path.expanduser("~"),
                                                    "JSON (*.json)")
    if not path:
        return
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        ida_kernwin.warning(f"Couldn't read {path}: {e}")
        return
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        ida_kernwin.warning(f"{os.path.basename(path)} isn't a plugin list exported by {PLUGIN_NAME}.")
        return
    to_install, already, listed_only = import_plan(data, status_entries, catalog)
    extra = []
    if already:
        extra.append(f"Already installed: {len(already)}")
    if listed_only:
        extra += ["", "The list can't install these, install them yourself:"] + [f"  {t}" for t in listed_only]
    if not to_install:
        ida_kernwin.info("\n".join(["Nothing to install from this list."] + ([""] + extra if extra else [])))
        return
    if extra:
        ida_kernwin.info("\n".join([f"{len(to_install)} plugins can be installed from this list.", ""] + extra))
    chooser = CheckChooser(
        f"Install from {os.path.basename(path)}", [("Plugin", 26), ("Version", 12), ("Note", 30)],
        [[name, version, note] for name, version, note, _ in to_install],
    )
    if chooser.Show(modal=True) < 0:
        return
    picked = [to_install[i] for i in chooser.checked()]
    if not picked:
        ida_kernwin.info("No plugin was checked, so nothing was installed.")
        return
    # held at their version in the list: held here too
    held_in_list = {p.get("name") for p in data.get("plugins") or [] if p.get("held")}
    for name, _, _, _ in picked:
        if name in held_in_list:
            set_held(name, True)
    operations.perform([(operations.INSTALL, name, reference) for name, _, _, reference in picked],
                       on_status=on_status)
