import io
import math
from functools import cache
from pathlib import Path

import pygame as pg
import pygame.freetype

_FONTS_DIR = Path(__file__).parent / "fonts"

PLACEHOLDER_SIZE = (1280, 720)


@cache
def _placeholder_font_bytes() -> bytes:
    # The file is cached, not the Font: a Font does not survive pg.quit() and a later pg.init()
    return (_FONTS_DIR / "DejaVuSans.ttf").read_bytes()


def placeholder_surface(text: str) -> pg.Surface:
    """A dark surface with `text` (one line per newline) centred on it, shown in place of an image
    that could not be loaded. It widens to fit long lines; the viewer scales it like any image."""
    font = pg.freetype.Font(io.BytesIO(_placeholder_font_bytes()), 36)
    lines = text.splitlines() or [""]
    rects = [font.get_rect(line) for line in lines]
    line_height = font.get_sized_height(0) + 12
    margin = 2 * line_height
    width = max(PLACEHOLDER_SIZE[0], max(r.width for r in rects) + 2 * margin)
    height = max(PLACEHOLDER_SIZE[1], line_height * len(lines) + 2 * margin)
    surface = pg.Surface((width, height))
    surface.fill(pg.Color(24, 24, 24))
    y = (height - line_height * len(lines)) // 2
    for line, rect in zip(lines, rects):
        font.render_to(surface, ((width - rect.width) // 2, y), line, fgcolor=pg.Color(200, 200, 200))
        y += line_height
    return surface


def scale_percent(scale: float) -> int:
    """`scale` as an integer percent, truncated so a scale just under 1.0 never reads "100%"
    (the epsilon keeps float error, e.g. 0.29 * 100, from dropping a point)."""
    return math.floor(scale * 100 + 1e-9)


class ImageViewer:
    border: int = 50

    STATUS_COLORS = {
        "CLEAN": pg.Color(128, 255, 128),
        "DIRTY": pg.Color(255, 128, 128),
        "UNREVIEWED": pg.Color(128, 128, 128),
        "FLAGGED": pg.Color(255, 176, 64),
    }

    SCALE_WARNING_COLOR = pg.Color(192, 0, 0)

    def __init__(self):
        sizes = pg.display.get_desktop_sizes()
        best = max(range(len(sizes)), key=lambda i: sizes[i][0] * sizes[i][1])
        self._display_index = best
        w, h = sizes[best]
        self.screen = pg.display.set_mode((w, h), pg.NOFRAME | pg.RESIZABLE, display=best)
        pg.display.toggle_fullscreen()
        pg.mouse.set_visible(False)
        self.font = pg.freetype.Font(str(_FONTS_DIR / "DejaVuSans.ttf"), 36)
        self.font.fgcolor = pg.Color(64, 64, 64)
        self.font.strong = True
        self._image = None
        self._status = "UNREVIEWED"
        self._info = ""
        self._name = ""
        self._content = None
        self._offset = (0, 0)
        self._scale = 1.0  # displayed size / source size of the current image
        self._source_scale = 1.0  # the image's own scale vs. its source: a grid shrinks the images in it
        self._splash_font = pg.freetype.Font(str(_FONTS_DIR / "DejaVuSansMono.ttf"), 24)
        self._splash_font.fgcolor = pg.Color(200, 200, 200)
        self._joystick_count = 0
        self._todo_only = False

    def set_image(self, surface: pg.Surface, name: str, status: str, info: str, source_scale: float = 1.0) -> None:
        self._image = surface
        self._source_scale = source_scale
        self._name = name
        self._status = status
        self._info = info
        pg.display.set_caption(name)
        self.resize()

    def switch_display(self, display_index: int) -> bool:
        """Switch to the given display (0-based). Returns True if the display changed."""
        sizes = pg.display.get_desktop_sizes()
        if display_index < 0 or display_index >= len(sizes) or display_index == self._display_index:
            return False
        self._display_index = display_index
        w, h = sizes[display_index]
        self.screen = pg.display.set_mode((w, h), pg.NOFRAME | pg.RESIZABLE, display=display_index)
        pg.display.toggle_fullscreen()
        pg.mouse.set_visible(False)
        self.resize()
        return True

    def display_lines(self) -> list[str]:
        """Return lines describing available displays for the selection overlay."""
        sizes = pg.display.get_desktop_sizes()
        lines = ["Select display:"]
        for i, (w, h) in enumerate(sizes):
            marker = "  <--" if i == self._display_index else ""
            lines.append(f"  {i + 1}  {w}x{h}{marker}")
        return lines

    def set_joystick_count(self, count: int) -> None:
        self._joystick_count = count

    def set_todo_only(self, enabled: bool) -> None:
        self._todo_only = enabled

    def set_status(self, status: str) -> None:
        self._status = status

    def set_info(self, info: str) -> None:
        """Replace the centered status-bar text (e.g. with a short notice)."""
        self._info = info

    def resize(self) -> None:
        if self._image is None:
            return
        self.screen = pg.display.get_surface()  # a resize may have replaced the window surface
        screen_w, screen_h = self.screen.get_size()
        content_height = screen_h - self.border
        if content_height <= 0:
            return
        iw, ih = self._image.get_size()
        if iw == 0 or ih == 0:
            return
        scale = min(screen_w / iw, content_height / ih)
        self._scale = scale  # above 1.0 when a small image is enlarged to fit
        scaled_size = (round(iw * scale), round(ih * scale))
        self._content = pg.transform.smoothscale(self._image, scaled_size)
        cx, cy = self._content.get_size()
        self._offset = ((screen_w - cx) // 2, (content_height - cy) // 2)

    def refresh(self) -> None:
        self.screen = pg.display.get_surface()
        screen_w, screen_h = self.screen.get_size()
        self.screen.fill(pg.Color(64, 64, 64))
        bar_color = self.STATUS_COLORS[self._status]
        pg.draw.rect(self.screen, bar_color, pg.Rect(0, screen_h - self.border, screen_w, self.border))
        left_text = "(h)elp"
        left_text += " | todo-only" if self._todo_only else " | all"
        if self._joystick_count == 0:
            left_text += " | no gamepad"
        elif self._joystick_count == 1:
            left_text += " | gamepad connected"
        else:
            left_text += f" | {self._joystick_count} gamepads"
        self._bar_text(left_text, "left")
        name_inset = 0
        if self._content is not None:
            # Red below 100%: small text may be lost
            scale = self._scale * self._source_scale
            percent = f"{scale_percent(scale)}%"
            color = self.SCALE_WARNING_COLOR if scale < 1.0 else None
            self._bar_text(percent, "right", color=color)
            name_inset = self.font.get_rect(percent).width + self.font.get_rect(percent).height
        self._bar_text(self._name, "right", inset=name_inset)
        self._bar_text(self._info, "center")
        if self._content is not None:
            self.screen.blit(self._content, self._offset)
        pg.display.flip()

    def _bar_text(self, text: str, align: str, *, color: pg.Color | None = None, inset: int = 0) -> None:
        """Draw `text` in the status bar; `inset` moves right-aligned text left; `color` None is the font's."""
        bbox = self.font.get_rect(text)
        screen_w, screen_h = self.screen.get_size()
        y = int(screen_h - (self.border + bbox.height) / 2)
        margin = int(bbox.height / 2)
        if align == "left":
            x = margin
        elif align == "right":
            x = screen_w - margin - bbox.width - inset
        else:
            x = int((screen_w - bbox.width) / 2)
        self.font.render_to(self.screen, (x, y), text, fgcolor=color)

    HELP_LINES = [
        "Keyboard                 Controller",
        "  c        Mark CLEAN      B / East   Mark CLEAN",
        "  d        Mark DIRTY      Y / North  Mark DIRTY",
        "  z        Undo last mark",
        "  b        Next batch (end of list)",
        "  Left/Right  Navigate     D-pad      Navigate",
        "  n        Next todo",
        "  u        Todo only",
        "  Space    Autoplay        Start      Quit",
        "  s        Single mode",
        "  m        Grid mode (--rotate policy)",
        "  M        Grid mode (no rotation)",
        "  w        Select display",
        "  h        This help",
        "  q / Esc  Quit",
    ]

    def show_splash(self, lines: list[str], footer: str | list[str] = "Press [space] to continue") -> None:
        self.screen = pg.display.get_surface()
        screen_w, screen_h = self.screen.get_size()
        self.screen.fill(pg.Color(64, 64, 64))
        splash_font = self._splash_font
        line_height = splash_font.get_sized_height() + 6
        footer_lines = [footer] if isinstance(footer, str) else footer
        help_lines = self.HELP_LINES
        all_lines = lines + [""] + help_lines + [""] + footer_lines
        info_end = len(lines)
        bright = pg.Color(255, 255, 255)
        max_width = max(splash_font.get_rect(l).width for l in all_lines if l)
        total_height = line_height * len(all_lines)
        x_start = (screen_w - max_width) // 2
        y_start = (screen_h - total_height) // 2
        for i, line in enumerate(all_lines):
            if not line:
                continue
            color = bright if i < info_end else None
            splash_font.render_to(self.screen, (x_start, y_start + i * line_height), line, fgcolor=color)
        pg.display.flip()

    def show_message(self, text: str) -> None:
        self.screen = pg.display.get_surface()
        screen_w, screen_h = self.screen.get_size()
        self.screen.fill(pg.Color(64, 64, 64))
        bbox = self.font.get_rect(text)
        self.font.render_to(
            self.screen,
            (int((screen_w - bbox.width) / 2), int((screen_h - bbox.height) / 2)),
            text,
            fgcolor=pg.Color(200, 200, 200),
        )
        pg.display.flip()
