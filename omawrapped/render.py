"""Text widths for the layout, and the SVG-to-PNG step.

The card is drawn as an SVG and turned into a PNG by rsvg-convert, which
lays text out with Pango. Widths are therefore asked of Pango too, so a
label that is measured to fit does fit. Without Pango's Python bindings the
width of a monospace font is estimated instead.
"""

import os
import shutil
import subprocess
import tempfile
import unicodedata
from pathlib import Path

ELLIPSIS = "…"
# Advance of a monospace glyph in ems, for the estimate. JetBrains Mono,
# Omarchy's default, is exactly this wide; most others are narrower.
MONO_ADVANCE = 0.6
# No label on the card comes near this; it bounds the work for one that is absurd.
MAX_LABEL = 200


class RenderError(Exception):
    pass


class Metrics:
    def __init__(self, family: str):
        self.family = family
        self.exact = False
        self._pango = None
        self._context = None
        try:
            import gi

            gi.require_version("Pango", "1.0")
            gi.require_version("PangoCairo", "1.0")
            from gi.repository import Pango, PangoCairo

            self._pango = Pango
            self._context = PangoCairo.FontMap.get_default().create_context()
            self.exact = True
        except Exception:  # no bindings, no typelib, no font map: estimate
            pass

    def width(self, text: str, size: float, bold: bool = False, spacing: float = 0) -> float:
        """Width in px of text at a font size; spacing is SVG letter-spacing."""
        if not text:
            return 0.0
        extra = spacing * len(text)
        if not self.exact:
            cells = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)
            return cells * MONO_ADVANCE * size + extra
        pango = self._pango
        description = pango.FontDescription()
        description.set_family(self.family)
        description.set_absolute_size(size * pango.SCALE)
        description.set_weight(pango.Weight.BOLD if bold else pango.Weight.NORMAL)
        layout = pango.Layout.new(self._context)
        layout.set_font_description(description)
        layout.set_text(text, -1)
        return layout.get_size()[0] / pango.SCALE + extra

    def fit(self, text: str, size: float, max_width: float, bold: bool = False) -> str:
        """text, shortened with an ellipsis until it is at most max_width wide."""
        if len(text) > MAX_LABEL:
            text = text[:MAX_LABEL] + ELLIPSIS
        if self.width(text, size, bold) <= max_width:
            return text
        for end in range(len(text) - 1, 0, -1):
            candidate = text[:end].rstrip() + ELLIPSIS
            if self.width(candidate, size, bold) <= max_width:
                return candidate
        return ELLIPSIS

    def shrink(self, text: str, size: float, max_width: float, bold: bool = False) -> float:
        """The font size, at most `size`, at which text is at most max_width wide."""
        width = self.width(text, size, bold)
        # Glyphs are placed on whole pixels, so width does not scale exactly
        # with size: the size that fits is approached, not computed.
        while width > max_width and size > 1:
            size = max(1, min(size * max_width / width, size - 0.25))
            width = self.width(text, size, bold)
        return size


def have_renderer() -> bool:
    return shutil.which("rsvg-convert") is not None


def _write_private(path: Path, data: bytes) -> None:
    """Writes data to path, replacing what is there only once all of it is written. The file is the user's alone.

    The data goes to a new file next to path first, made by mkstemp: a name nobody can guess, never a file that
    was there before, and mode 0600 whatever the umask. That file is what replaces path, so a card is never
    readable by other users, not even for a moment, and never half written.
    """
    descriptor, partial = tempfile.mkstemp(dir=path.parent, prefix="." + path.name + ".", suffix=".part")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        os.replace(partial, path)
    finally:
        # Gone already when it was moved into place; otherwise nothing may be left behind.
        try:
            os.unlink(partial)
        except OSError:
            pass


def write_png(svg: str, path: Path) -> None:
    """Renders svg to a PNG at path, replacing it only once the render is whole.

    The drawing goes to rsvg-convert on its standard input and the picture comes back on its standard output, so
    that no path, which says where the user keeps things, is among the arguments that every user can read.
    """
    if not have_renderer():
        raise RenderError("rsvg-convert was not found. It is part of the librsvg package, which Omarchy ships.")
    try:
        done = subprocess.run(["rsvg-convert", "--format=png"], input=svg.encode("utf-8"), capture_output=True,
                              timeout=60)
        if done.returncode != 0 or not done.stdout:
            detail = done.stderr.decode("utf-8", "replace").strip().splitlines()
            raise RenderError("rsvg-convert could not render the card" + (": " + detail[-1] if detail else "."))
        _write_private(path, done.stdout)
    except (OSError, subprocess.SubprocessError) as error:
        raise RenderError("The card could not be written to %s: %s" % (path, error)) from error


def write_svg(svg: str, path: Path) -> None:
    """Saves the drawing itself at path, replacing it only once it is whole."""
    try:
        _write_private(path, svg.encode("utf-8"))
    except OSError as error:
        raise RenderError("The card could not be written to %s: %s" % (path, error)) from error
