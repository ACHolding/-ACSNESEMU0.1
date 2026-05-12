#!/usr/bin/env python3
"""
AC's SNES emu — silhouette shell (hue logo) with the **mewsnes0.1** core
pre-baked into this single .py file.

Pre-baked here means: the core class lives in this file (no external
``snes_core`` / ``snes_core_pure`` / Cython build required).
It does NOT mean a ROM is embedded — that is still **FILES=OFF / #nobake**:
the only cart bytes on the bus come from **Load ROM…** at runtime.

PR (copy for GitHub)
--------------------
**Title:** AC's SNES emu — mewsnes0.1 core baked in, FILES=OFF (#nobake)

**Summary**
- mewsnes0.1 cart loader / stub CPU is inlined into snesemu0.1.py.
- Removes ``snes_core`` / ``snes_core_pure`` / ``setup_snes.py`` runtime deps.
- No pre-baked ROM bytes in the tree; user picks ``.sfc`` / ``.smc`` via dialog.

**Test plan**
- ``python snesemu0.1.py`` → caption + header show **mewsnes0.1 (baked)**.
- Click **Load ROM…**, pick a ``.sfc`` / ``.smc`` → title / map / PC update.
- **Reload** re-ingests the last picked bytes (kept in RAM only).

Requirements
------------
  pip install pygame
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

try:
    import pygame
except ImportError:
    print("Error: pip install pygame", file=sys.stderr)
    sys.exit(1)

try:
    import tkinter as tk
    from tkinter import filedialog

    _HAS_TK = True
except ImportError:
    _HAS_TK = False

# Product / core branding.
MEWSNES_CORE = "mewsnes0.1"  # core baked into this file
FILES_OFF = True             # no pre-baked ROMs; no core disk I/O


# =====================================================================
# mewsnes0.1 — cart loader + stub CPU (pre-baked into this .py file)
# =====================================================================
#
# Scope (honest): LoROM / HiROM heuristic, 512-byte copier header strip,
# header read (title, map, reset vector), simple ROM/WRAM address mapping,
# and a tiny stub CPU (NOP / BRK / "unknown" trace). This is not a full
# 65816 + PPU + APU; commercial games will *load and identify*, not run.

def _ms_normalize_cart(data: bytes) -> bytes:
    """Strip an optional 512-byte copier header (size == 512 mod 1024)."""
    if len(data) >= 512 and len(data) % 1024 == 512:
        return data[512:]
    return data


def _ms_score_title(rom: bytes, base: int) -> int:
    if base + 21 > len(rom):
        return -1
    score = 0
    for b in rom[base : base + 21]:
        if 32 <= b < 127:
            score += 2
        elif b in (0, 0x20):
            score += 1
        else:
            score -= 1
    return score


def _ms_detect_hirom(rom: bytes) -> bool:
    """Compare candidate header blocks at $7FC0 (LoROM) vs $FFC0 (HiROM)."""
    if len(rom) < 0x10000:
        return False
    lo = _ms_score_title(rom, 0x7FC0)
    hi = _ms_score_title(rom, 0xFFC0)
    if hi > lo + 2:
        return True
    if len(rom) >= 0xFFE0:
        csum = rom[0xFFDC] | (rom[0xFFDD] << 8)
        comp = rom[0xFFDE] | (rom[0xFFDF] << 8)
        if (csum ^ comp) == 0xFFFF and 0 < csum < 0xFFFF:
            return True
    return False


def _ms_reset_vector_offset(rom: bytes, hirom: bool) -> int:
    n = len(rom)
    if hirom:
        off = n - 0x10000 + 0xFFFC
    else:
        off = n - 0x8000 + 0x7FFC
    if off < 0 or off + 1 >= n:
        return -1
    return off


def _ms_read_title(rom: bytes, hirom: bool) -> str:
    base = 0xFFC0 if hirom else 0x7FC0
    if len(rom) < base + 21:
        return "?"
    raw = rom[base : base + 21]
    return raw.decode("latin-1", errors="replace").strip("\x00 ").strip() or "?"


@dataclass
class MewSNES01:
    """Baked mewsnes0.1 core. FILES=OFF — caller passes bytes from a dialog."""

    rom: bytes = b""
    hirom: bool = False
    wram: bytearray = field(default_factory=lambda: bytearray(0x20000))
    pb: int = 0
    pc: int = 0x8000
    a: int = 0
    x: int = 0
    y: int = 0
    sp: int = 0x1FF
    db: int = 0
    d: int = 0
    p: int = 0x34
    halted: bool = False
    trace: List[str] = field(default_factory=list)
    last_error: str = ""

    name: str = MEWSNES_CORE

    def _log(self, msg: str) -> None:
        self.trace.append(msg)
        if len(self.trace) > 24:
            self.trace = self.trace[-24:]

    def load_cart(self, data: bytes) -> str:
        """Ingest user-picked cart bytes. Returns '' on success, else error text."""
        self.last_error = ""
        if not data or len(data) < 0x8000:
            self.last_error = "ROM too small (<32 KiB)"
            self.rom = b""
            return self.last_error
        self.rom = _ms_normalize_cart(bytes(data))
        self.hirom = _ms_detect_hirom(self.rom)
        self.wram = bytearray(0x20000)
        self.halted = False
        self.trace.clear()
        off = _ms_reset_vector_offset(self.rom, self.hirom)
        if off < 0:
            self.last_error = "Could not locate reset vector"
            return self.last_error
        vec = self.rom[off] | (self.rom[off + 1] << 8)
        self.pb = 0
        self.pc = vec & 0xFFFF
        title = _ms_read_title(self.rom, self.hirom)
        self._log(f"[cart] size={len(self.rom)} {'HiROM' if self.hirom else 'LoROM'} title={title!r}")
        self._log(f"[boot] reset vector $00:{vec:04X} (file off {off:#x})")
        return ""

    def cart_title(self) -> str:
        return _ms_read_title(self.rom, self.hirom) if self.rom else "(no ROM)"

    def _rom_offset(self, addr24: int) -> int:
        bank = (addr24 >> 16) & 0xFF
        addr = addr24 & 0xFFFF
        n = len(self.rom)
        if n == 0:
            return -1
        if self.hirom:
            if addr >= 0x8000:
                off = ((bank & 0x3F) << 16) | addr
            else:
                off = ((bank & 0x3F) << 16) | (addr + 0x8000)
        else:
            if addr >= 0x8000:
                off = ((bank & 0x7F) << 15) | (addr & 0x7FFF)
            else:
                return -1
        return off if 0 <= off < n else -1

    def read8(self, addr24: int) -> int:
        bank = (addr24 >> 16) & 0xFF
        addr = addr24 & 0xFFFF
        if bank in (0x7E, 0x7F):
            woff = ((bank & 1) << 16) | addr
            return self.wram[woff] if woff < len(self.wram) else 0
        if bank == 0x00 or 0x80 <= bank <= 0xFF:
            off = self._rom_offset(addr24)
            return self.rom[off] if off >= 0 else 0
        return 0

    def boot(self) -> None:
        if not self.rom:
            return
        off = _ms_reset_vector_offset(self.rom, self.hirom)
        if off < 0:
            return
        vec = self.rom[off] | (self.rom[off + 1] << 8)
        self.pb = 0
        self.pc = vec & 0xFFFF
        self.halted = False
        self._log(f"[boot] PB=00 PC={self.pc:04X}")

    def step(self) -> None:
        """Single-byte stub advance. Honest about not running real code."""
        if self.halted or not self.rom:
            return
        addr24 = (self.pb << 16) | self.pc
        op = self.read8(addr24)
        self.pc = (self.pc + 1) & 0xFFFF
        if op == 0x00:
            self.halted = True
            self._log("[cpu] BRK / stub halt")
            return
        if op == 0xEA:
            self._log("[cpu] NOP")
            return
        self._log(f"[cpu] stub op ${op:02X} @ ${addr24:06X} (full CPU not implemented)")


# =====================================================================
# Silhouette UI
# =====================================================================

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
    base_h = (tick_ms // 24) % 360
    lines = ["AC's", "SNES", "emu"]
    y = rect.y + 8
    for li, line in enumerate(lines):
        x = rect.x + 12
        for ci, ch in enumerate(line):
            hue = (base_h + li * 28 + ci * 18) % 360
            glyph = font_big.render(ch, True, hsv_to_rgb(hue, 0.55, 0.95))
            surf.blit(glyph, (x, y))
            x += glyph.get_width()
        y += font_big.get_height() - 2
    tag = font_tag.render(f"{MEWSNES_CORE} (baked) · FILES=OFF · #nobake", True, COLOR_TEXT_DIM)
    surf.blit(tag, (rect.x + 12, y + 4))


def draw_round_rect(surf, rect, radius, fill, border=None, bw=1):
    pygame.draw.rect(surf, fill, rect, border_radius=radius)
    if border is not None:
        pygame.draw.rect(surf, border, rect, bw, border_radius=radius)


def draw_button(surf, font, label, rect, mouse_pos):
    hover = rect.collidepoint(mouse_pos)
    bg = COLOR_BTN_HOVER if hover else COLOR_PANEL
    draw_round_rect(surf, rect, 6, bg, COLOR_EDGE_HI if hover else COLOR_EDGE, 1)
    t = font.render(label, True, COLOR_TEXT if hover else COLOR_TEXT_DIM)
    surf.blit(t, (rect.centerx - t.get_width() // 2, rect.centery - t.get_height() // 2))
    return hover


def main():
    pygame.init()
    pygame.display.set_caption(f"AC's SNES emu — {MEWSNES_CORE} (baked) · FILES=OFF")

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

    core = MewSNES01()
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
                            title=f"AC's SNES emu — {MEWSNES_CORE} — Open ROM",
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
                                    cap = f"AC's SNES emu — {MEWSNES_CORE} — {core.cart_title()[:36]}"
                                    pygame.display.set_caption(cap)
                                    show_t(f"{MEWSNES_CORE}: cart ok · {len(data)} bytes · FILES=OFF #nobake", 90)
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
        mode = f"{MEWSNES_CORE} · baked-in"
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
            f"FILES_OFF (pre-baked ROM = OFF): {FILES_OFF}  #nobake",
            f"Core: {MEWSNES_CORE} — baked into this single .py file.",
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
        title_s = core.cart_title()
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
        for row in core.trace[-10:]:
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
