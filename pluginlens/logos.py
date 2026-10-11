"""The logos the Browse tab shows, fetched from GitHub once and cached.

A plugin's ida-plugin.json may name a logo ("logoPath"), relative to the
plugin's folder. For a plugin that isn't installed, the logo is fetched from
its GitHub repository: at the commit or tag the archive URL hcli would
install names, if it does (else the default branch); if the plugin is in a
subfolder of the repository, one request to GitHub's API finds it. Logos are kept, as small PNGs, in
<IDA user directory>/plugin-lens/logos/, and misses are remembered for a week.
Set "fetch_logos": false in plugin-lens/settings.json to fetch nothing.
"""

import json
import os
import queue
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import ida_diskio
import ida_kernwin

from .common import PLUGIN_NAME, USER_DIR, square_png

LOGOS_DIR = "logos"
MISSES_FILE = "misses.json"
RETRY_AFTER = 7 * 24 * 3600  # seconds before asking again for a logo that wasn't found
MAX_BYTES = 5 * 1024 * 1024  # bigger files aren't logos worth downloading
WORKERS = 4
TIMEOUT = 15
_GITHUB = re.compile(r"^https://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$", re.IGNORECASE)
_ARCHIVE_REFS = [
    # https://codeload.github.com/o/r/legacy.zip/<ref>, .../zip/refs/tags/<ref>
    re.compile(r"^https://codeload\.github\.com/[^/]+/[^/]+/(?:legacy\.)?(?:zip|tar\.gz)/(?:refs/(?:heads|tags)/)?(.+)$"),
    # https://github.com/o/r/archive/[refs/tags/]<ref>.zip
    re.compile(r"^https://github\.com/[^/]+/[^/]+/archive/(?:refs/(?:heads|tags)/)?(.+?)\.(?:zip|tar\.gz)$"),
]
_USER_AGENT = f"{PLUGIN_NAME} (IDA plugin)"


def logos_dir():
    return os.path.join(ida_diskio.get_user_idadir(), USER_DIR, LOGOS_DIR)


def key_of(host, name):
    """The cache file's name, without extension, for a plugin."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", f"{host.rstrip('/').split('github.com/')[-1]}__{name}")


def cached(key):
    """The cached logo's path, or None."""
    path = os.path.join(logos_dir(), key + ".png")
    return path if os.path.isfile(path) else None


def _ref(archive):
    """The commit or tag an archive URL is of, or None."""
    for pattern in _ARCHIVE_REFS:
        match = pattern.match(archive or "")
        if match:
            return urllib.parse.unquote(match.group(1))
    return None


def _get(url, limit=MAX_BYTES):
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("too big")
    return data


def _download(host, logo_path, archive):
    """The logo's bytes, from GitHub, or None if it can't be found."""
    match = _GITHUB.match(host or "")
    if not match or not logo_path:
        return None
    repo = f"{match.group(1)}/{match.group(2)}"
    path = logo_path[2:] if logo_path.startswith("./") else logo_path
    path = path.lstrip("/")
    if ".." in path.split("/"):
        return None
    ref = _ref(archive) or "HEAD"
    raw = f"https://raw.githubusercontent.com/{repo}/{urllib.parse.quote(ref, safe='')}/"
    try:
        return _get(raw + urllib.parse.quote(path))
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    # the plugin may be in a subfolder of its repository: look for the path
    tree = json.loads(_get(
        f"https://api.github.com/repos/{repo}/git/trees/{urllib.parse.quote(ref, safe='')}?recursive=1",
        limit=20 * 1024 * 1024,
    ))
    found = sorted(
        (item["path"] for item in tree.get("tree", [])
         if item.get("type") == "blob" and item.get("path", "").endswith("/" + path)),
        key=len,
    )
    return _get(raw + urllib.parse.quote(found[0])) if found else None


class Fetcher:
    """Fetches the logos asked for, a few at a time, in daemon threads (which
    never keep IDA from exiting), and calls on_ready(key) on IDA's main
    thread for each one saved."""

    def __init__(self):
        self._queue = queue.Queue()
        self._pending = set()
        self._failed = set()  # failed this session (e.g. offline): asked again next session
        self._lock = threading.Lock()
        self._started = False
        self.listeners = []  # called with the key of each new logo
        self._misses = None

    def _misses_path(self):
        return os.path.join(logos_dir(), MISSES_FILE)

    def _load_misses(self):
        if self._misses is None:
            try:
                with open(self._misses_path(), encoding="utf-8") as f:
                    self._misses = dict(json.load(f))
            except (OSError, ValueError, TypeError):
                self._misses = {}
        return self._misses

    def _missed(self, key):
        with self._lock:
            misses = self._load_misses()
            misses[key] = time.time()
            try:
                os.makedirs(logos_dir(), exist_ok=True)
                with open(self._misses_path(), "w", encoding="utf-8") as f:
                    json.dump(misses, f, indent=1, sort_keys=True)
            except OSError:
                pass

    def want(self, key, host, logo_path, archive):
        """Ask for a logo, unless it's cached, pending, or missed recently."""
        if cached(key):
            return
        with self._lock:
            if key in self._pending or key in self._failed:
                return
            if time.time() - self._load_misses().get(key, 0) < RETRY_AFTER:
                return
            self._pending.add(key)
        self._queue.put((key, host, logo_path, archive))
        if not self._started:
            self._started = True
            for i in range(WORKERS):
                threading.Thread(target=self._work, name=f"plugin-lens-logos-{i}", daemon=True).start()

    def _work(self):
        while True:
            key, host, logo_path, archive = self._queue.get()
            try:
                data = _download(host, logo_path, archive)
                png = square_png(data) if data else None
            except Exception:  # offline, rate-limited, ...
                png, failed = None, True
            else:
                failed = False
            if png:
                try:
                    os.makedirs(logos_dir(), exist_ok=True)
                    with open(os.path.join(logos_dir(), key + ".png"), "wb") as f:
                        f.write(png)
                except OSError:
                    png = None
            elif not failed:
                self._missed(key)  # there's no such logo (errors are retried next session)
            with self._lock:
                self._pending.discard(key)
                if failed:
                    self._failed.add(key)
            if png:
                self._ready(key)

    def _ready(self, key):
        def notify():
            for listener in list(self.listeners):
                try:
                    listener(key)
                except RuntimeError:  # its window is gone
                    self.listeners.remove(listener)
            return 0

        # without waiting: a worker never blocks on IDA's main thread
        ida_kernwin.execute_sync(notify, ida_kernwin.MFF_NOWAIT)


fetcher = Fetcher()
