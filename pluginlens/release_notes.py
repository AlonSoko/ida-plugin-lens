"""A plugin's release notes, as Markdown, fetched when asked for.

Tried in order: the changelog the plugin repository's site links to, the
GitHub releases (one request to GitHub's API, which allows 60 an hour
without signing in), and a CHANGELOG.md in the repository.
"""

import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request

import ida_kernwin

from .common import PLUGIN_NAME

TIMEOUT = 20
MAX_BYTES = 512 * 1024
RELEASES = 15  # the latest ones
CHANGELOG_FILES = ("CHANGELOG.md", "CHANGES.md", "HISTORY.md")
_GITHUB = re.compile(r"^https://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$", re.IGNORECASE)
_USER_AGENT = f"{PLUGIN_NAME} (IDA plugin)"


def _get(url, accept=None):
    headers = {"User-Agent": _USER_AGENT}
    if accept:
        headers["Accept"] = accept
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=TIMEOUT) as response:
        data = response.read(MAX_BYTES + 1)
    text = data[:MAX_BYTES].decode("utf-8", errors="replace")
    return text + ("\n\n*(cut: too long to show whole)*" if len(data) > MAX_BYTES else "")


def _version_key(text):
    """For comparing versions: v1.10.0 > 1.9.2."""
    parts = re.findall(r"\d+", text or "")
    return tuple(int(p) for p in parts[:4])


def fetch(host, changelog_url=None, default_branch=None, installed=None):
    """(markdown, where it comes from, or None) for a plugin; markdown is
    None when nothing was found (the second value then says what was tried).

    installed: the installed version, if any: newer releases are marked new.
    """
    if changelog_url:
        try:
            return _get(changelog_url), changelog_url
        except (urllib.error.URLError, OSError, ValueError):
            pass
    match = _GITHUB.match(host or "")
    if not match:
        return None, "The plugin isn't on GitHub, so its release notes can't be fetched."
    repo = f"{match.group(1)}/{match.group(2)}"
    limited = False
    try:
        releases = json.loads(_get(f"https://api.github.com/repos/{repo}/releases?per_page={RELEASES}",
                                   accept="application/vnd.github+json"))
    except urllib.error.HTTPError as e:
        releases, limited = [], e.code in (403, 429)
    except (urllib.error.URLError, OSError, ValueError):
        releases = []
    if isinstance(releases, list) and releases:
        return _releases_markdown(releases, installed), f"{host}/releases"
    for name in CHANGELOG_FILES:
        url = f"https://raw.githubusercontent.com/{repo}/{urllib.parse.quote(default_branch or 'HEAD')}/{name}"
        try:
            return _get(url), url
        except (urllib.error.URLError, OSError, ValueError):
            continue
    why = " GitHub's limit of 60 requests an hour without signing in was reached; try later." if limited else ""
    return None, f"No releases or changelog found in {host}.{why}"


def _releases_markdown(releases, installed):
    lines = []
    for release in releases:
        if not isinstance(release, dict) or release.get("draft"):
            continue
        tag = release.get("tag_name") or ""
        title = release.get("name") or tag
        date = (release.get("published_at") or "")[:10]
        new = installed and _version_key(tag) > _version_key(installed)
        heading = f"## {title}" + (f" ({date})" if date else "") + (" — new" if new else "")
        if release.get("prerelease"):
            heading += " — pre-release"
        body = (release.get("body") or "*(no notes)*").strip()
        if "\n" not in body and "\\n" in body:  # written with literal "\n"s
            body = body.replace("\\n", "\n")
        lines += [heading, "", body, ""]
    return "\n".join(lines)


class Fetcher:
    """Fetches release notes in daemon threads, once per plugin and session,
    and calls listeners(key) on IDA's main thread when they're ready."""

    def __init__(self):
        self.cache = {}  # key -> (markdown or None, source or reason)
        self.pending = set()
        self.listeners = []

    def get(self, key, host, changelog_url=None, default_branch=None, installed=None):
        """The cached result, or None after starting to fetch it."""
        if key in self.cache:
            return self.cache[key]
        if key not in self.pending:
            self.pending.add(key)

            def worker():
                try:
                    result = fetch(host, changelog_url, default_branch, installed)
                except Exception as e:  # never break IDA over release notes
                    result = (None, f"Couldn't fetch the release notes: {type(e).__name__}: {e}")

                def done():
                    self.cache[key] = result
                    self.pending.discard(key)
                    for listener in list(self.listeners):
                        try:
                            listener(key)
                        except RuntimeError:  # its window is gone
                            self.listeners.remove(listener)
                    return 0

                ida_kernwin.execute_sync(done, ida_kernwin.MFF_NOWAIT)

            threading.Thread(target=worker, name="plugin-lens-notes", daemon=True).start()
        return None


fetcher = Fetcher()
