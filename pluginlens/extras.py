"""What the Hex-Rays plugin repository's site knows beyond hcli's index:
GitHub stars, dates, badges, the logo's URL and a changelog's URL.

The site (plugins.hex-rays.com) is built from combined.json, which the
plugin repository publishes on GitHub Pages and regenerates daily.
"""

import json
import urllib.request

from .common import PLUGIN_NAME

COMBINED_URL = "https://hexrayssa.github.io/plugin-repository/plugins/combined.json"
TIMEOUT = 30
MAX_BYTES = 32 * 1024 * 1024
BADGES = {  # tag -> what the site calls it
    "favourite": "Hex-Rays favourite",
    "first_place": "Plugin contest: first place",
    "second_place": "Plugin contest: second place",
    "third_place": "Plugin contest: third place",
}


def host_key(url):
    """A repository URL, normalized for comparing."""
    url = (url or "").strip().lower().rstrip("/")
    return url[:-4] if url.endswith(".git") else url


def fetch():
    """{host_key(GitHub URL): {"stars", "forks", "updated", "created", "added",
    "changelog_url", "logo_url", "badges"}} from combined.json; raises if it
    can't be fetched or read."""
    request = urllib.request.Request(COMBINED_URL, headers={"User-Agent": f"{PLUGIN_NAME} (IDA plugin)"})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        data = response.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("combined.json is too big")
    return parse(json.loads(data))


def parse(combined):
    result = {}
    for plugin in combined.get("plugins") or []:
        if not isinstance(plugin, dict) or not plugin.get("host"):
            continue
        md = plugin.get("metadata") or {}
        dynamic = md.get("dynamic_metadata") or {}
        manifest = (md.get("idaplugin_json") or {}).get("plugin") or {}
        tags = md.get("tags") or []
        contest = next((t for t in tags if t.startswith("plugin_contest_")), None)
        badges = []
        for tag in tags:
            if tag in BADGES:
                text = BADGES[tag]
                if contest and tag.endswith("_place"):
                    text = text.replace("Plugin contest", f"Plugin contest {contest[len('plugin_contest_'):]}")
                badges.append(text)
        result[host_key(plugin["host"])] = {
            "stars": dynamic.get("stars") if isinstance(dynamic.get("stars"), int) else None,
            "forks": dynamic.get("forks") if isinstance(dynamic.get("forks"), int) else None,
            "updated": (dynamic.get("latest_update") or "")[:10] or None,
            "created": (dynamic.get("created_at") or "")[:10] or None,
            "added": (dynamic.get("added_at") or "")[:10] or None,  # to the plugin repository
            "changelog_url": md.get("changelog_url") or None,
            "logo_url": manifest.get("absoluteLogoUrl") or None,
            "default_branch": dynamic.get("default_branch") or None,
            "badges": badges,
        }
    return result
