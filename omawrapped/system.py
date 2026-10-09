"""What the card says about the system, read from Omarchy's own files."""

import configparser
import json
import os
import re
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

from . import PLUGIN_ID

HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
# A Chromium app window: chrome-<host>__<path>-<Profile>.
WEBAPP_CLASS = re.compile(r"^[a-z]+-(?P<host>[^_/]+)__.*-[^-]+$")


@dataclass
class Theme:
    name: str = ""
    background: str = "#101315"
    foreground: str = "#cacccc"
    accent: str = "#cacccc"


def _state_dir() -> Path:
    return Path.home() / ".local" / "state" / "omarchy" / "current"


def theme(directory=None) -> Theme:
    """The active Omarchy theme, or the shell's fallback colours without one.

    directory overrides where colors.toml is looked for (any theme folder).
    """
    found = Theme()
    if directory is not None:
        folders = [Path(directory)]
    else:
        # Omarchy 4 keeps the active theme under ~/.local/state; before
        # that it was a link in ~/.config.
        folders = [_state_dir() / "theme", Path.home() / ".config" / "omarchy" / "current" / "theme"]
    for folder in folders:
        try:
            colors = tomllib.loads((folder / "colors.toml").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue

        def pick(*keys):
            for key in keys:
                value = colors.get(key)
                if isinstance(value, str) and HEX.match(value):
                    return value.lower()
            return None

        # The same fallbacks the shell applies to a theme without the named keys.
        found.background = pick("background", "color0") or found.background
        found.foreground = pick("foreground", "color7") or found.foreground
        found.accent = pick("accent", "color4") or found.foreground
        break
    if directory is not None:
        found.name = _title(Path(directory).name)
        return found
    # The slug is written beside the theme; before that, `theme` was a link
    # to a folder named after it.
    for folder in folders:
        try:
            found.name = _title((folder.parent / "theme.name").read_text(encoding="utf-8").strip())
        except OSError:
            if folder.is_symlink():
                found.name = _title(folder.resolve().name)
        if found.name:
            break
    return found


def _title(slug: str) -> str:
    """"matte-black" -> "Matte Black", as `omarchy theme current` prints it."""
    return " ".join(part[:1].upper() + part[1:] for part in slug.split("-") if part)


def plugin_count() -> int:
    """Shell plugins installed under ~/.config/omarchy/plugins."""
    root = Path.home() / ".config" / "omarchy" / "plugins"
    try:
        return sum(1 for entry in root.iterdir() if (entry / "manifest.json").is_file())
    except OSError:
        return 0


def omarchy_version() -> str:
    root = os.environ.get("OMARCHY_PATH") or "/usr/share/omarchy"
    try:
        return (Path(root) / "version").read_text(encoding="utf-8").strip().splitlines()[0][:24]
    except (OSError, IndexError):
        return ""


def settings() -> dict:
    """This plugin's entry in the bar layout of shell.json, or {} without one."""
    try:
        config = json.loads((Path.home() / ".config" / "omarchy" / "shell.json").read_text(encoding="utf-8"))
        layout = config["bar"]["layout"]
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    if not isinstance(layout, dict):
        return {}
    for section in ("left", "center", "right"):
        entries = layout.get(section)
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, dict) and entry.get("id") == PLUGIN_ID:
                return entry
    return {}


def split_list(value) -> list:
    """"a, b ,c" (or a list) -> ["a", "b", "c"]."""
    parts = value if isinstance(value, list) else str(value or "").split(",")
    return [str(part).strip() for part in parts if str(part).strip()]


def pictures_dir() -> Path:
    """The user's Pictures folder: XDG_PICTURES_DIR, user-dirs.dirs, ~/Pictures."""
    value = os.environ.get("XDG_PICTURES_DIR", "")
    if not value:
        config = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
        try:
            text = (Path(config) / "user-dirs.dirs").read_text(encoding="utf-8")
        except OSError:
            text = ""
        match = re.search(r'^XDG_PICTURES_DIR="([^"]+)"', text, re.MULTILINE)
        value = match.group(1) if match else ""
    value = value.replace("$HOME", str(Path.home()))
    return Path(value) if value.startswith("/") else Path.home() / "Pictures"


def monospace_family() -> str:
    """The font family behind the `monospace` alias, which `omarchy font set` writes."""
    try:
        done = subprocess.run(
            ["fc-match", "-f", "%{family[0]}", "monospace"],
            capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL,
        )
        family = done.stdout.strip() if done.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        family = ""
    # The name goes into an SVG attribute; anything unusual falls back.
    return family if re.fullmatch(r"[\w .+-]{1,80}", family) else "monospace"


class AppNames:
    """App ids (window classes) to the names people know the apps by.

    Names come from the desktop entries on the system. Without an entry the
    id itself is tidied up: "com.mitchellh.ghostty" becomes "Ghostty".
    """

    def __init__(self, folders=None):
        self._by_id = {}
        self._by_host = {}
        self._cache = {}
        if folders is None:
            data = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
            home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
            folders = [Path(home) / "applications"] + [Path(d) / "applications" for d in data.split(":") if d]
        # Later folders have lower priority, so they never replace a name.
        for folder in folders:
            try:
                files = sorted(Path(folder).glob("*.desktop"))
            except OSError:
                continue
            for path in files:
                self._read(path)

    def _read(self, path):
        parser = configparser.RawConfigParser(strict=False, interpolation=None)
        parser.optionxform = str
        try:
            parser.read(path, encoding="utf-8")
            entry = parser["Desktop Entry"]
        except (configparser.Error, KeyError, OSError, UnicodeDecodeError):
            return
        name = entry.get("Name", "").strip()
        if not name or entry.get("NoDisplay", "").lower() == "true":
            return
        for key in (entry.get("StartupWMClass", "").strip(), path.stem):
            if key:
                self._by_id.setdefault(key.casefold(), name)
        # Omarchy web apps are a browser window on one site.
        match = re.search(r"(?:omarchy-launch-webapp\s+|--app=)[\"']?https?://([^/\s\"']+)", entry.get("Exec", ""))
        if match:
            self._by_host.setdefault(_bare_host(match.group(1)), name)

    def name(self, app: str) -> str:
        if app not in self._cache:
            self._cache[app] = self._resolve(app)
        return self._cache[app]

    def _resolve(self, app: str) -> str:
        known = self._by_id.get(app.casefold())
        if known:
            return known
        web = WEBAPP_CLASS.match(app)
        if web and "." in web.group("host"):
            host = _bare_host(web.group("host"))
            return self._by_host.get(host, host)
        # Reverse-DNS ids end in the app's name; plain ids are the name.
        last = app.rsplit(".", 1)[-1] if re.fullmatch(r"[\w-]+(\.[\w-]+){2,}", app) else app
        words = re.sub(r"[-_]+", " ", last).strip()
        return words[:1].upper() + words[1:] if words else app


def _bare_host(host: str) -> str:
    host = host.lower()
    return host[4:] if host.startswith("www.") else host
