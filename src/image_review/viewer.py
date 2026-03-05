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
        self.screen = pg.display.set_mode((0, 0), pg.FULLSCREEN | pg.RESIZABLE)
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

    def set_image(self, surface: pg.Surface, name: str, status: str, info: str) -> None:
        self._image = surface
        self._name = name
        self._status = status
        self._info = info
        pg.display.set_caption(name)
        self.resize()

    def set_status(self, status: str) -> None:
        self._status = status

    def resize(self) -> None:
        if self._image is None:
            return
        X, Y = self.screen.get_size()
        content_height = Y - self.border
        iw, ih = self._image.get_size()
        scale = min(X / iw, content_height / ih)
        scaled_size = (round(iw * scale), round(ih * scale))
        self._content = pg.transform.smoothscale(self._image, scaled_size)
        cx, cy = self._content.get_size()
        self._offset = ((X - cx) // 2, (content_height - cy) // 2)

    def refresh(self) -> None:
        X, Y = self.screen.get_size()
        self.screen.fill(pg.Color(64, 64, 64))
        bar_color = self.STATUS_COLORS[self._status]
        pg.draw.rect(self.screen, bar_color, pg.Rect(0, Y - self.border, X, self.border))
        self._text_right(self._name)
        self._text_center(self._info)
        if self._content is not None:
            self.screen.blit(self._content, self._offset)
        pg.display.flip()

    def _text_right(self, text: str) -> None:
        bbox = self.font.get_rect(text)
        X, Y = self.screen.get_size()
        self.font.render_to(
            self.screen,
            (X - int(bbox.height / 2) - bbox.width, int(Y - (self.border + bbox.height) / 2)),
            text,
        )

    def _text_center(self, text: str) -> None:
        bbox = self.font.get_rect(text)
        X, Y = self.screen.get_size()
        self.font.render_to(
            self.screen,
            (int((X - bbox.width) / 2), int(Y - (self.border + bbox.height) / 2)),
            text,
        )

    def show_splash(self, lines: list[str], footer: str = "Press [space] to continue") -> None:
        X, Y = self.screen.get_size()
        self.screen.fill(pg.Color(64, 64, 64))
        splash_font = pg.freetype.Font(str(_FONTS_DIR / "DejaVuSansMono.ttf"), 24)
        splash_font.fgcolor = pg.Color(200, 200, 200)
        line_height = splash_font.get_sized_height() + 6
        all_lines = lines + ["", footer]
        max_width = max(splash_font.get_rect(l).width for l in all_lines if l)
        total_height = line_height * len(all_lines)
        x_start = (X - max_width) // 2
        y_start = (Y - total_height) // 2
        for i, line in enumerate(all_lines):
            if not line:
                continue
            splash_font.render_to(self.screen, (x_start, y_start + i * line_height), line)
        pg.display.flip()

    def show_help(self) -> None:
        help_lines = [
            "Keyboard                 Controller",
            "  c        Mark CLEAN      B / East   Mark CLEAN",
            "  d        Mark DIRTY      Y / North  Mark DIRTY",
            "  Left/Right  Navigate     D-pad      Navigate",
            "  Space    Autoplay        Start      Quit",
            "  w        Fullscreen",
            "  h        This help",
            "  q / Esc  Quit",
        ]
        X, Y = self.screen.get_size()
        self.screen.fill(pg.Color(64, 64, 64))
        help_font = pg.freetype.Font(str(_FONTS_DIR / "DejaVuSansMono.ttf"), 24)
        help_font.fgcolor = pg.Color(200, 200, 200)
        line_height = help_font.get_sized_height() + 6
        max_width = max(help_font.get_rect(l).width for l in help_lines)
        total_height = line_height * len(help_lines)
        x_start = (X - max_width) // 2
        y_start = (Y - total_height) // 2
        for i, line in enumerate(help_lines):
            help_font.render_to(self.screen, (x_start, y_start + i * line_height), line)
        pg.display.flip()

    def show_message(self, text: str) -> None:
        X, Y = self.screen.get_size()
        self.screen.fill(pg.Color(64, 64, 64))
        bbox = self.font.get_rect(text)
        self.font.render_to(
            self.screen,
            (int((X - bbox.width) / 2), int((Y - bbox.height) / 2)),
            text,
            fgcolor=pg.Color(200, 200, 200),
        )
        pg.display.flip()

    def cleanup(self) -> None:
        pass
