from pathlib import Path

import pygame as pg
import pygame.freetype

_FONTS_DIR = Path(__file__).parent / "fonts"


class ImageViewer:
    border: int = 50

    STATUS_COLORS = {
        "CLEAN": pg.Color(128, 255, 128),
        "DIRTY": pg.Color(255, 128, 128),
        "UNREVIEWED": pg.Color(128, 128, 128),
    }

    def __init__(self):
        info = pg.display.Info()
        self.screen = pg.display.set_mode((info.current_w, info.current_h), pg.NOFRAME | pg.RESIZABLE)
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
        self._splash_font = pg.freetype.Font(str(_FONTS_DIR / "DejaVuSansMono.ttf"), 24)
        self._splash_font.fgcolor = pg.Color(200, 200, 200)
        self._joystick_count = 0
        self._todo_only = False

    def set_image(self, surface: pg.Surface, name: str, status: str, info: str) -> None:
        self._image = surface
        self._name = name
        self._status = status
        self._info = info
        pg.display.set_caption(name)
        self.resize()

    def set_joystick_count(self, count: int) -> None:
        self._joystick_count = count

    def set_todo_only(self, enabled: bool) -> None:
        self._todo_only = enabled

    def set_status(self, status: str) -> None:
        self._status = status

    def resize(self) -> None:
        if self._image is None:
            return
        screen_w, screen_h = self.screen.get_size()
        content_height = screen_h - self.border
        iw, ih = self._image.get_size()
        scale = min(screen_w / iw, content_height / ih)
        scaled_size = (round(iw * scale), round(ih * scale))
        self._content = pg.transform.smoothscale(self._image, scaled_size)
        cx, cy = self._content.get_size()
        self._offset = ((screen_w - cx) // 2, (content_height - cy) // 2)

    def refresh(self) -> None:
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
        self._text_left(left_text)
        self._text_right(self._name)
        self._text_center(self._info)
        if self._content is not None:
            self.screen.blit(self._content, self._offset)
        pg.display.flip()

    def _text_left(self, text: str) -> None:
        bbox = self.font.get_rect(text)
        _, screen_h = self.screen.get_size()
        self.font.render_to(
            self.screen,
            (int(bbox.height / 2), int(screen_h - (self.border + bbox.height) / 2)),
            text,
        )

    def _text_right(self, text: str) -> None:
        bbox = self.font.get_rect(text)
        screen_w, screen_h = self.screen.get_size()
        self.font.render_to(
            self.screen,
            (screen_w - int(bbox.height / 2) - bbox.width, int(screen_h - (self.border + bbox.height) / 2)),
            text,
        )

    def _text_center(self, text: str) -> None:
        bbox = self.font.get_rect(text)
        screen_w, screen_h = self.screen.get_size()
        self.font.render_to(
            self.screen,
            (int((screen_w - bbox.width) / 2), int(screen_h - (self.border + bbox.height) / 2)),
            text,
        )

    HELP_LINES = [
        "Keyboard                 Controller",
        "  c        Mark CLEAN      B / East   Mark CLEAN",
        "  d        Mark DIRTY      Y / North  Mark DIRTY",
        "  Left/Right  Navigate     D-pad      Navigate",
        "  n        Next todo",
        "  u        Todo only",
        "  Space    Autoplay        Start      Quit",
        "  m        Switch to {other_mode} mode",
        "  w        Fullscreen",
        "  h        This help",
        "  q / Esc  Quit",
    ]

    def _format_help(self, mode: str = "single") -> list[str]:
        other_mode = "grid" if mode == "single" else "single"
        return [l.format(other_mode=other_mode) for l in self.HELP_LINES]

    def show_splash(self, lines: list[str], footer: str | list[str] = "Press [space] to continue", mode: str = "single") -> None:
        screen_w, screen_h = self.screen.get_size()
        self.screen.fill(pg.Color(64, 64, 64))
        splash_font = self._splash_font
        line_height = splash_font.get_sized_height() + 6
        footer_lines = [footer] if isinstance(footer, str) else footer
        help_lines = self._format_help(mode)
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
