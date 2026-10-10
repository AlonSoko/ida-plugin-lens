"""Running hcli operations (install, update, uninstall) on plugins, one after another."""

import ida_kernwin

from . import hcli
from .common import PLUGIN_NAME
from .restart import ask_restart

INSTALL, UPGRADE, UNINSTALL = "install", "upgrade", "uninstall"
_DOING = {INSTALL: "Installing", UPGRADE: "Updating", UNINSTALL: "Uninstalling"}
_DONE = {INSTALL: "Installed", UPGRADE: "Updated", UNINSTALL: "Uninstalled"}

# what changed this session, until IDA restarts: hcli name -> INSTALL, UPGRADE or UNINSTALL
changed_this_session = {}


def perform(operations, on_status=None, titles=None):
    """Run [(verb, hcli plugin name)] with hcli, then report.

    verb is INSTALL, UPGRADE or UNINSTALL; an install may be (INSTALL, name,
    what to give hcli), e.g. hcli.install_reference(...).
    titles: {hcli name: the name to show}, e.g. the plugin's name in IDA.

    Shows IDA's wait box (cancellable between plugins), sends hcli's output
    to the Output window, asks hcli for the resulting status and passes it to
    on_status(entries) (None if hcli couldn't be asked), then shows a summary.
    What changed is kept in changed_this_session.
    If anything changed, the summary's only button restarts IDA, which loads
    plugins only when it starts.
    """
    if not operations:
        return
    titles = titles or {}
    operations = [(op[0], op[1], op[2] if len(op) > 2 else op[1]) for op in operations]
    results = []
    entries = None
    ida_kernwin.show_wait_box(f"{_DOING[operations[0][0]]} plugins…")
    try:
        ida_kernwin.replace_wait_box("Checking the installed plugins…")
        before = _installed_versions()
        for i, (verb, name, argument) in enumerate(operations, 1):
            if ida_kernwin.user_cancelled():
                break
            ida_kernwin.replace_wait_box(f"{_DOING[verb]} {titles.get(name, name)} ({i}/{len(operations)})…")
            try:
                succeeded, output = hcli.run("plugin", verb, argument)
            except Exception as e:  # hcli not importable, or changed
                succeeded, output = False, f"{type(e).__name__}: {e}"
            ida_kernwin.msg(f"[{PLUGIN_NAME}] hcli plugin {verb} {argument}\n{output}\n")
            results.append((verb, name, succeeded))
        ida_kernwin.replace_wait_box("Checking the installed plugins…")
        try:
            entries = hcli.status()
        except Exception:
            entries = None
    finally:
        ida_kernwin.hide_wait_box()

    after = _installed_versions(entries) if entries is not None else None
    changed, failed = [], []
    for verb, name, succeeded in results:
        title = titles.get(name, name)
        if after is None:  # can't tell; trust hcli's answer
            ok = succeeded
            text = f"{_DONE[verb]} {title}"
        elif verb == UPGRADE:
            ok = bool(after.get(name)) and after.get(name) != before.get(name)
            text = f"Updated {title} {before.get(name)} → {after.get(name)}"
        elif verb == INSTALL:
            ok = bool(after.get(name)) and before.get(name) is None
            text = f"Installed {title} {after.get(name)}"
        else:
            ok = name not in after
            text = f"Uninstalled {title}"
        if ok:
            changed.append(text)
            changed_this_session[name] = verb
        else:
            failed.append(title)
    done = {name for _, name, _ in results}
    skipped = [titles.get(name, name) for _, name, _ in operations if name not in done]
    if on_status is not None:
        on_status(entries)

    lines = []
    if changed:
        lines += changed
    if failed:
        lines += ["", "Not done (see the Output window):"] + [f"  {name}" for name in failed]
    if skipped:
        lines += ["", "Cancelled before:"] + [f"  {name}" for name in skipped]
    text = "\n".join(lines).strip() or "Nothing was changed."
    if not changed:
        ida_kernwin.info(text)
        return
    ask_restart(text + "\n\nIDA restarts to load the changes.")


def _installed_versions(entries=None):
    """{hcli plugin name: installed version}."""
    if entries is None:
        try:
            entries = hcli.status()
        except Exception:
            return {}
    return {p["name"]: p.get("version") for p in entries if p.get("kind") == "installed" and p.get("name")}
