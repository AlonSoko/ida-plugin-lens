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
    what to give hcli), e.g. hcli.install_reference(...), or a tuple of
    arguments. For an install from a URL or file, name may be None (not known
    before hcli fetches it): what was installed is then told from the
    versions installed before and after.
    titles: {hcli name, or for name None the argument: the name to show}.

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
    operations = [(verb, name, args if isinstance(args, tuple) else (args,)) for verb, name, args in operations]

    def title(name, args):
        return titles.get(name if name is not None else args[-1], name if name is not None else args[-1])

    results = []
    entries = None
    ida_kernwin.show_wait_box(f"{_DOING[operations[0][0]]} plugins…")
    try:
        ida_kernwin.replace_wait_box("Checking the installed plugins…")
        before = _installed_versions()
        for i, (verb, name, args) in enumerate(operations, 1):
            if ida_kernwin.user_cancelled():
                break
            ida_kernwin.replace_wait_box(f"{_DOING[verb]} {title(name, args)} ({i}/{len(operations)})…")
            try:
                succeeded, output = hcli.run("plugin", verb, *args)
            except Exception as e:  # hcli not importable, or changed
                succeeded, output = False, f"{type(e).__name__}: {e}"
            ida_kernwin.msg(f"[{PLUGIN_NAME}] hcli plugin {verb} {' '.join(args)}\n{output}\n")
            results.append((verb, name, args, succeeded))
        ida_kernwin.replace_wait_box("Checking the installed plugins…")
        try:
            entries = hcli.status()
        except Exception:
            entries = None
    finally:
        ida_kernwin.hide_wait_box()

    after = _installed_versions(entries) if entries is not None else None
    changed, failed, unchanged = [], [], []
    for verb, name, args, succeeded in results:
        shown = title(name, args)
        if after is not None and verb == INSTALL and name is None:
            # which plugins appeared, or changed version
            new = sorted(n for n, v in after.items() if before.get(n) != v)
            for n in new:
                if before.get(n) is None:
                    changed.append(f"Installed {n} {after[n]}")
                    changed_this_session[n] = INSTALL
                else:
                    changed.append(f"Updated {n} {before[n]} → {after[n]}")
                    changed_this_session[n] = UPGRADE
            before.update({n: after[n] for n in new})  # for the next operation
            if not new:
                # hcli succeeds without a change when that version is already installed
                (unchanged if succeeded else failed).append(shown)
            continue
        if after is None:  # can't tell; trust hcli's answer
            ok = succeeded
            text = f"{_DONE[verb]} {shown}"
        elif verb == UPGRADE:
            ok = bool(after.get(name)) and after.get(name) != before.get(name)
            text = f"Updated {shown} {before.get(name)} → {after.get(name)}"
        elif verb == INSTALL:
            ok = bool(after.get(name)) and before.get(name) is None
            text = f"Installed {shown} {after.get(name)}"
        else:
            ok = name not in after
            text = f"Uninstalled {shown}"
        if ok:
            changed.append(text)
            if name is not None:
                changed_this_session[name] = verb
        else:
            failed.append(shown)
    skipped = [title(name, args) for _, name, args in operations[len(results):]]
    if on_status is not None:
        on_status(entries)

    lines = []
    if changed:
        lines += changed
    if failed:
        lines += ["", "Not done (see the Output window):"] + [f"  {name}" for name in failed]
    if unchanged:
        lines += ["", "Already installed, nothing changed:"] + [f"  {name}" for name in unchanged]
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
