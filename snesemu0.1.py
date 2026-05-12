#!/usr/bin/env python3
"""
AC's SNES emu — silhouette shell (hue logo) + **mewsnes** core.

PR (copy for GitHub)
-------------------
**Title:** AC's SNES emu — mewsnes shell, FILES=OFF

**Summary**
- UI shells the **mewsnes** cart loader / stub CPU: imports compiled ``snes_core``
  (Cython) when present, else ``snes_core_pure`` (Python).
- **FILES=OFF:** no pre-baked ROMs in the tree, no auto-load, no disk writes from the
  core. The only bytes on the bus are what you pick with **Load ROM…** (then kept
  in RAM for **Reload**).

**Test plan**
- ``python snesemu0.1.py`` → caption + header show **mewsnes**; viewport states FILES=OFF.
- Load a ``.sfc`` / ``.smc`` → title/map/PC update; **Reload** re-ingests last bytes.

**Build** (optional Cython speed-up, same folder):

  pip install pygame cython setuptools
  python setup_snes.py build_ext --inplace
  python snesemu0.1.py"""

from __future__ import annotations

import sys
import math
from pathlib import Path

try:
    import pygame
except ImportError:
    print("Error: pip install pygame", file=sys.stderr)
    sys.exit(1)

_CORE = None
try:
    from snes_core import SNESCore as _CythonCore

    _CORE = "cython"
except ImportError:
    try:
        from snes_core_pure import SNESCorePure as _CythonCore

        _CORE = "python"
    except ImportError as e:
        print("Error: need snes_core (mewsnes build) or snes_core_pure.py", e, file=sys.stderr)
        sys.exit(1)

try:
    import tkinter as tk
    from tkinter import filedialog

    _HAS_TK = True
except ImportError:
    _HAS_TK = False

# Product / core branding (UI only — modules stay ``snes_core`` / ``snes_core_pure``).
MEWSNES = "mewsnes"
FILES_OFF = True  # no pre-baked ROMs; no core disk I/O

# --- Silhouette palette ---
COLOR_VOID = (6, 6, 8)
COLOR_PANEL = (16, 16, 20)
COLOR_PANEL_INNER = (10, 10, 12)
COLOR_EDGE = (52, 52, 58)
COLOR_EDGE_HI = (88, 88, 96)
COLOR_TEXT = (210, 210, 218)
COLOR_TEXT_DIM = (110, 110, 120)
COLOR_ACCENT = (230, 230, 240)
COLOR_BTN_HOVER = (32, 32, 38)


def hsv_to_rgb(h: float, s: float, v: float) -> tuple[int, int, int]:
    """h in [0,360), s and v in [0,1]."""
    h = (h % 360.0) / 60.0
    i = int(math.floor(h))
    f = h - i
    p = v * (1 - s)
    q = v * (1 - s * f)
    t = v * (1 - s * (1 - f))
    if i == 0:
        r, g, b = v, t, p
    elif i == 1:
        r, g, b = q, v, p
    elif i == 2:
        r, g, b = p, v, t
    elif i == 3:
        r, g, b = p, q, v
    elif i == 4:
        r, g, b = t, p, v
    else:
        r, g, b = v, p, q
    return int(r * 255), int(g * 255), int(b * 255)


def draw_hue_logo(surf, font_big, font_tag, rect, tick_ms: int) -> None:
    """AC's SNES emu wordmark — per-glyph hue sweep + mewsnes core tag."""
    base_h = (tick_ms // 24) % 360
    lines = ["AC's", "SNES", "emu"]
    y = rect.y + 8
    for li, line in enumerate(lines):
        x = rect.x + 12
        for ci, ch in enumerate(line):
            hue = (base_h + li * 28 + ci * 18) % 360
            col = hsv_to_rgb(hue, 0.55, 0.95)
            glyph = font_big.render(ch, True, col)
            surf.blit(glyph, (x, y))
            x += glyph.get_width()
        y += font_big.get_height() - 2
    tag = font_tag.render(f"{MEWSNES} core · FILES=OFF", True, COLOR_TEXT_DIM)
    surf.blit(tag, (rect.x + 12, y + 4))


def draw_round_rect(surf, rect, radius, fill, border=None, bw=1):
    pygame.draw.rect(surf, fill, rect, border_radius=radius)
    if border is not None:
        pygame.draw.rect(surf, border, rect, bw, border_radius=radius)


def draw_button(surf, font, label, rect, mouse_pos):
    hover = rect.collidepoint(mouse_pos)
    bg = COLOR_BTN_HOVER if hover else COLOR_PANEL
    draw_round_rect(surf, rect, 6, bg, COLOR_EDGE if not hover else COLOR_EDGE_HI, 1)
    t = font.render(label, True, COLOR_TEXT if hover else COLOR_TEXT_DIM)
    surf.blit(t, (rect.centerx - t.get_width() // 2, rect.centery - t.get_height() // 2))
    return hover


def main():
    pygame.init()
    core_tag = f"{MEWSNES} · Cython" if _CORE == "cython" else f"{MEWSNES} · Python (fallback)"
    pygame.display.set_caption(f"AC's SNES emu — {core_tag}")

    w, h = 760, 500
    screen = pygame.display.set_mode((w, h))
    clock = pygame.time.Clock()

    try:
        font_title = pygame.font.SysFont("consolas", 18, bold=True)
        font_logo = pygame.font.SysFont("segoeui", 34, bold=True)
        if not pygame.font.match_font("segoeui"):
            font_logo = pygame.font.SysFont("consolas", 32, bold=True)
        try:
            font_tag = pygame.font.SysFont("segoeui", 13, italic=True)
        except TypeError:
            font_tag = pygame.font.SysFont("segoeui", 13)
        font_body = pygame.font.SysFont("consolas", 14)
        font_small = pygame.font.SysFont("consolas", 12)
    except Exception:
        font_title = pygame.font.Font(None, 20)
        font_logo = pygame.font.Font(None, 36)
        font_tag = pygame.font.Font(None, 14)
        font_body = pygame.font.Font(None, 17)
        font_small = pygame.font.Font(None, 15)

    core = _CythonCore()
    root = None
    if _HAS_TK:
        root = tk.Tk()
        root.withdraw()

    last_cart: bytes | None = None
    margin = 14
    header_h = 40
    viewport = pygame.Rect(margin, header_h + margin, 380, h - header_h - 2 * margin - 52)
    side = pygame.Rect(viewport.right + margin, viewport.y, w - viewport.right - 2 * margin, viewport.height)
    bar_y = h - 48
    btn_load = pygame.Rect(margin, bar_y, 120, 36)
    btn_step = pygame.Rect(margin + 130, bar_y, 90, 36)
    btn_run = pygame.Rect(margin + 230, bar_y, 110, 36)
    btn_boot = pygame.Rect(margin + 350, bar_y, 100, 36)
    btn_reset = pygame.Rect(margin + 460, bar_y, 100, 36)

    toast = ""
    toast_ticks = 0
    auto_run = False
    auto_timer = 0

    def show_t(msg, t=90):
        nonlocal toast, toast_ticks
        toast, toast_ticks = msg, t

    running = True
    while running:
        mouse = pygame.mouse.get_pos()
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                if btn_load.collidepoint(event.pos):
                    if not _HAS_TK:
                        show_t("tkinter missing — cannot pick ROM", 120)
                    else:
                        was = auto_run
                        auto_run = False
                        pygame.event.pump()
                        path = filedialog.askopenfilename(
                            parent=root,
                            title="AC's SNES emu — Open ROM",
                            filetypes=[
                                ("SNES ROM", "*.sfc *.smc *.SFC *.SMC"),
                                ("All", "*.*"),
                            ],
                        )
                        auto_run = was
                        if path:
                            try:
                                data = Path(path).read_bytes()
                            except OSError as e:
                                show_t(f"read error: {e}", 120)
                            else:
                                err = core.load_cart(data)
                                if err:
                                    show_t(err, 120)
                                else:
                                    last_cart = data
                                    cap = f"AC's SNES emu — {MEWSNES} — {core.cart_title()[:40]}"
                                    pygame.display.set_caption(cap)
                                    show_t(f"{MEWSNES}: cart ok · {len(data)} bytes · FILES=OFF", 90)
                elif btn_step.collidepoint(event.pos):
                    core.step()
                    auto_run = False
                elif btn_run.collidepoint(event.pos):
                    auto_run = not auto_run
                elif btn_boot.collidepoint(event.pos):
                    core.boot()
                    auto_run = False
                    show_t("soft reset (reset vector)", 60)
                elif btn_reset.collidepoint(event.pos):
                    if last_cart:
                        core.load_cart(last_cart)
                        show_t("reloaded cart", 60)
                    else:
                        core.boot()
                        show_t("boot vector (no ROM in RAM yet)", 60)

        if auto_run and not core.halted:
            auto_timer += 1
            if auto_timer >= 12:
                auto_timer = 0
                core.step()

        screen.fill(COLOR_VOID)

        hdr = pygame.Rect(0, 0, w, header_h)
        draw_round_rect(screen, hdr, 0, COLOR_PANEL, COLOR_EDGE, 1)
        mode = f"{MEWSNES} · Cython" if _CORE == "cython" else f"{MEWSNES} · pure Python"
        title = font_title.render(f"AC's SNES emu  ·  silhouette  ·  {mode}", True, COLOR_ACCENT)
        screen.blit(title, (margin, 10))

        draw_round_rect(screen, viewport, 10, COLOR_PANEL, COLOR_EDGE, 1)
        inner = viewport.inflate(-20, -20)
        draw_round_rect(screen, inner, 8, COLOR_PANEL_INNER, None)
        tick = pygame.time.get_ticks()
        logo_rect = pygame.Rect(inner.x, inner.y + 8, inner.width, 130)
        draw_hue_logo(screen, font_logo, font_tag, logo_rect, tick)
        lines = [
            "",
            f"FILES_OFF (no pre-baked ROM): {FILES_OFF}",
            f"Core: {MEWSNES} (cart map + reset vector + stub CPU).",
            "No pre-baked ROM in this app — use Load ROM….",
            "Cartridge mapper: LoROM / HiROM (heuristic).",
            "512-byte copier header stripped if present.",
            "",
            "Not a full SNES — no PPU/APU/DSP.",
            "CPU is a stub (NOP/BRK trace only).",
        ]
        ly = inner.y + 8 + 130
        for ln in lines:
            screen.blit(font_small.render(ln, True, COLOR_TEXT_DIM), (inner.x + 10, ly))
            ly += 18

        draw_round_rect(screen, side, 10, COLOR_PANEL, COLOR_EDGE, 1)
        y = side.y + 10
        title_s = core.cart_title() if hasattr(core, "cart_title") else "?"
        for lab in (
            "SNES header",
            f"  Title  {title_s[:34]}",
            f"  Map    {'HiROM' if core.hirom else 'LoROM'}",
            f"  PC     ${core.pc:04X}",
            f"  PB     ${core.pb:02X}",
            f"  Halt   {core.halted}",
            "",
            "Trace",
        ):
            col = COLOR_ACCENT if lab.endswith("header") or lab == "Trace" else COLOR_TEXT
            if lab.startswith("  "):
                col = COLOR_TEXT
            screen.blit(font_body.render(lab, True, col), (side.x + 10, y))
            y += 20
        for row in getattr(core, "trace", [])[-10:]:
            screen.blit(font_small.render(row, True, COLOR_TEXT_DIM), (side.x + 10, y))
            y += 16

        draw_round_rect(screen, pygame.Rect(0, bar_y - 8, w, 56), 0, COLOR_VOID, None)
        draw_button(screen, font_body, "Load ROM…", btn_load, mouse)
        draw_button(screen, font_body, "Step", btn_step, mouse)
        run_l = "Auto: ON" if auto_run else "Auto: OFF"
        draw_button(screen, font_body, run_l, btn_run, mouse)
        draw_button(screen, font_body, "Boot", btn_boot, mouse)
        draw_button(screen, font_body, "Reload", btn_reset, mouse)

        if toast_ticks > 0:
            toast_ticks -= 1
            surf = font_small.render(toast, True, (180, 220, 255))
            pygame.draw.rect(
                screen,
                (28, 28, 36),
                (8, h - surf.get_height() - 14, surf.get_width() + 12, surf.get_height() + 8),
            )
            screen.blit(surf, (14, h - surf.get_height() - 10))

        pygame.display.flip()
        clock.tick(60)

    pygame.quit()
    if root is not None:
        root.destroy()


if __name__ == "__main__":
    main()
