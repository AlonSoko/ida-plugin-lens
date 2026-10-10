"""Talking to hcli, through its own Python code (IDA must be able to import it).

Nothing here runs the hcli executable. collect_status_report() and hcli's
commands are part of hcli's command code rather than a stable API, so every
use is wrapped: a failure leaves the information unknown, or reports the
command as failed, and never breaks IDA.
"""

import contextlib
import io
import json
import logging
import os
import re
import time

import ida_diskio

from .common import USER_DIR

HCLI_SOURCES_FILE = "hcli-sources.json"
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")  # terminal codes (e.g. hcli's spinner)


@contextlib.contextmanager
def quiet_http_logs():
    """Hide the per-request INFO logs of hcli's HTTP library (httpx).

    Another plugin may have enabled INFO logging for everything, which would
    put a line per request in IDA's Output window. Warnings still show.
    """
    loggers = [logging.getLogger(name) for name in ("httpx", "httpcore")]
    levels = [logger.level for logger in loggers]
    for logger in loggers:
        logger.setLevel(max(logger.getEffectiveLevel(), logging.WARNING))
    try:
        yield
    finally:
        for logger, level in zip(loggers, levels):
            logger.setLevel(level)


def status():
    """What `hcli plugin status --json` lists: installed, legacy and
    incompatible plugins, with versions, repositories and updates."""
    from hcli.commands.plugin.status import collect_status_report
    from hcli.lib.ida import get_ida_config, get_plugin_repositories
    from hcli.lib.ida.plugin.repo.aggregate import AggregatePluginRepo

    repositories = get_plugin_repositories(get_ida_config())
    if not repositories:
        raise RuntimeError("no plugin repositories configured")
    aggregate = AggregatePluginRepo(repositories)
    with quiet_http_logs():
        report = collect_status_report(aggregate, (), False, repo_of=aggregate.repo_of)
    return report.model_dump(mode="json")["plugins"]


def catalog():
    """Every plugin in hcli's repositories, as plain data:

    {"ida_version", "platform": what hcli checks compatibility against,
     "default_repo", "notes": hcli's notes (e.g. a repository it couldn't
     reach), "plugins": [{"name", "host", "repo", "versions": [{"version",
     "compatible"}] newest first, "latest", "latest_compatible" (None if
     none), "metadata": the latest compatible version's ida-plugin.json
     "plugin" object (else the latest's), "archive": that version's URL}]}
    """
    from hcli.lib.ida import (
        find_current_ida_platform, find_current_ida_version,
        get_default_plugin_repository_name, get_ida_config, get_plugin_repositories,
    )
    from hcli.lib.ida.plugin import parse_plugin_version
    from hcli.lib.ida.plugin.repo import is_compatible_plugin_version
    from hcli.lib.ida.plugin.repo.aggregate import AggregatePluginRepo

    config = get_ida_config()
    repositories = get_plugin_repositories(config)
    if not repositories:
        raise RuntimeError("no plugin repositories configured")
    aggregate = AggregatePluginRepo(repositories)
    ida_version, platform = find_current_ida_version(), find_current_ida_platform()
    with quiet_http_logs():
        plugins = aggregate.get_plugins()
    result = []
    for plugin in plugins:
        versions = sorted(plugin.versions.items(), key=lambda kv: parse_plugin_version(kv[0]), reverse=True)
        if not versions:
            continue
        listed = [
            {"version": version, "compatible": is_compatible_plugin_version(locations, platform, ida_version)}
            for version, locations in versions
        ]
        compatible = next((v["version"] for v in listed if v["compatible"]), None)
        shown = plugin.versions[compatible or listed[0]["version"]][0]
        result.append({
            "name": plugin.name,
            "host": plugin.host,
            "repo": aggregate.repo_of(plugin),
            "versions": listed,
            "latest": listed[0]["version"],
            "latest_compatible": compatible,
            "metadata": shown.metadata.model_dump(mode="json")["plugin"],
            "archive": shown.url,
        })
    return {
        "ida_version": ida_version,
        "platform": platform,
        "default_repo": get_default_plugin_repository_name(config),
        "notes": aggregate.notes(),
        "plugins": result,
    }


def install_reference(name, repo=None, host=None, version=None):
    """What `hcli plugin install` takes for this exact plugin: [repo/]name[==version][@host]."""
    return f"{repo + '/' if repo else ''}{name}{'==' + version if version else ''}{'@' + host if host else ''}"


def sources_from_status(entries):
    """{hcli plugin name: {"repo", "outdated", "version", "latest"}} from status()."""
    return {
        p["name"]: {
            "repo": p.get("repo") if p.get("in_repository") else None,
            "outdated": bool(p.get("upgradable_to")),
            "version": p.get("version"),
            "latest": p.get("upgradable_to"),
        }
        for p in entries
        if p.get("kind") == "installed" and p.get("name")
    }


def query_sources():
    """The sources (see sources_from_status()) that hcli reports, or None."""
    try:
        return sources_from_status(status())
    except Exception:  # hcli not importable, an hcli version that differs, offline, ...
        return None


def sources_path():
    return os.path.join(ida_diskio.get_user_idadir(), USER_DIR, HCLI_SOURCES_FILE)


def cached_sources():
    """The sources hcli reported last time, or None."""
    try:
        with open(sources_path(), encoding="utf-8") as f:
            plugins = dict(json.load(f)["plugins"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    # files from version 1.0 hold just the repository
    return {
        name: info if isinstance(info, dict) else {"repo": info, "outdated": False}
        for name, info in plugins.items()
    }


def save_sources(sources):
    try:
        os.makedirs(os.path.dirname(sources_path()), exist_ok=True)
        with open(sources_path(), "w", encoding="utf-8") as f:
            json.dump({"updated": time.time(), "plugins": sources}, f, indent=1, sort_keys=True)
    except OSError:
        pass


def run(*args):
    """Run `hcli <args>` through hcli's own Python code.

    Calling hcli's commands (rather than its library functions) keeps all of
    their checks, e.g. an upgrade stays with the repository the plugin came
    from, and handles plugin suites and Python dependencies.
    Returns (succeeded, hcli's output).
    """
    from hcli.lib import console as hcli_console
    from hcli.main import cli

    output = io.StringIO()
    consoles = (hcli_console.console, hcli_console.stderr_console)
    saved = [c.file for c in consoles]
    for c in consoles:
        c.file = output  # hcli prints through these; keep its output for IDA's
    try:
        with quiet_http_logs():
            cli.main(
                ["--disable-updates", *args],
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
    return succeeded, _ANSI.sub("", output.getvalue()).strip()
