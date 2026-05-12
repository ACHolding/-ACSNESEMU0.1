#!/usr/bin/env python3
"""
AC's SNES emu — silhouette shell with TWO baked engines:

  1. mewsnes libretro host     -- plays games via a libretro core .dll
                                  you provide at runtime. (FILES=OFF)
  2. mewsnes baked-CPU core    -- a real 65C816 CPU + SNES memory bus
                                  baked into this file. Executes ROM code
                                  and traces instructions. No PPU — does
                                  not render the actual game.

Switch between them with the Mode button.

FILES=OFF / #nobake / ultrathink
--------------------------------
  * No embedded core path. The libretro .dll is picked per session.
  * No embedded ROM bytes. The cart is picked per session.
  * Nothing is auto-loaded on startup.

Honest scope of the BAKED CPU
-----------------------------
This is a from-spec 65C816 implementation:
  * All standard addressing modes (immediate, dp, dp,X, dp,Y,
    (dp), [dp], (dp),Y, [dp],Y, (dp,X), sr,S, (sr,S),Y, abs,
    abs,X, abs,Y, long, long,X).
  * M/X size flags, REP/SEP, XCE, native/emulation modes.
  * ~95 of the most common opcodes including all transfers, all
    branches, all flag ops, JMP/JML/JSR/JSL/RTS/RTL, LDA/STA/LDX/
    STX/LDY/STY/STZ, ADC/SBC/AND/ORA/EOR/CMP/CPX/CPY, INC/DEC/INA/
    DEA/INX/DEX/INY/DEY, push/pop family, ASL/LSR/ROL/ROR (acc).
  * Binary ADC/SBC only (decimal mode is treated as binary).
  * NO PPU/APU/DMA — register reads return synthesized defaults
    (vblank-flag toggle so simple wait loops can eventually exit).

It does NOT render the actual game; it shows you what the CPU is
doing. On an unimplemented opcode it halts cleanly with the byte and
address logged so it can be added next.

Where to get a libretro core (Windows .dll)
-------------------------------------------
  RetroArch -> Online Updater -> Core Downloader -> "Nintendo - SNES/SFC".
  Cores land under RetroArch\\cores\\. snes9x_libretro.dll is the most
  forgiving choice (RGB565 frames, no awkward env requirements).

Requirements
------------
  pip install pygame numpy
  (optional Cython speed-up for libretro RGB565 -> RGB888:
   pip install cython
   python setup_mewsnes.py build_ext --inplace)

Default key map (libretro mode, port 0)
---------------------------------------
  Arrows -> D-Pad   Z/X -> B/A   A/S -> Y/X   Q/W -> L/R
  Enter -> Start    Backspace -> Select
  F1 -> Reset       F2 -> Toggle audio
"""

from __future__ import annotations

import ctypes
import math
import os
import sys
import traceback
from ctypes import (
    CFUNCTYPE, POINTER, Structure, byref,
    c_bool, c_char_p, c_double, c_float, c_int16, c_size_t,
    c_uint, c_uint8, c_uint16, c_void_p,
)
from pathlib import Path
from typing import Callable, Optional

try:
    import pygame
except ImportError:
    print("Error: pip install pygame", file=sys.stderr)
    sys.exit(1)

try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False

try:
    import tkinter as tk
    from tkinter import filedialog
    _HAS_TK = True
except ImportError:
    _HAS_TK = False

try:
    from mewsnes_fast import convert_rgb565 as _fast_565  # type: ignore
    _HAS_FAST = True
except ImportError:
    _HAS_FAST = False


MEWSNES_CORE = "mewsnes"
FILES_OFF = True


# =====================================================================
# Pre-baked libretro host (mewsnes)
# =====================================================================

class retro_game_info(Structure):
    _fields_ = [
        ("path", c_char_p),
        ("data", c_void_p),
        ("size", c_size_t),
        ("meta", c_char_p),
    ]


class retro_system_info(Structure):
    _fields_ = [
        ("library_name", c_char_p),
        ("library_version", c_char_p),
        ("valid_extensions", c_char_p),
        ("need_fullpath", c_bool),
        ("block_extract", c_bool),
    ]


class retro_game_geometry(Structure):
    _fields_ = [
        ("base_width", c_uint),
        ("base_height", c_uint),
        ("max_width", c_uint),
        ("max_height", c_uint),
        ("aspect_ratio", c_float),
    ]


class retro_system_timing(Structure):
    _fields_ = [
        ("fps", c_double),
        ("sample_rate", c_double),
    ]


class retro_system_av_info(Structure):
    _fields_ = [
        ("geometry", retro_game_geometry),
        ("timing", retro_system_timing),
    ]


VIDEO_CB = CFUNCTYPE(None, c_void_p, c_uint, c_uint, c_size_t)
AUDIO_SAMPLE_CB = CFUNCTYPE(None, c_int16, c_int16)
AUDIO_BATCH_CB = CFUNCTYPE(c_size_t, c_void_p, c_size_t)
INPUT_POLL_CB = CFUNCTYPE(None)
INPUT_STATE_CB = CFUNCTYPE(c_int16, c_uint, c_uint, c_uint, c_uint)
ENV_CB = CFUNCTYPE(c_bool, c_uint, c_void_p)


RETRO_API_VERSION = 1

RETRO_DEVICE_JOYPAD = 1
RETRO_DEVICE_ID_JOYPAD_B = 0
RETRO_DEVICE_ID_JOYPAD_Y = 1
RETRO_DEVICE_ID_JOYPAD_SELECT = 2
RETRO_DEVICE_ID_JOYPAD_START = 3
RETRO_DEVICE_ID_JOYPAD_UP = 4
RETRO_DEVICE_ID_JOYPAD_DOWN = 5
RETRO_DEVICE_ID_JOYPAD_LEFT = 6
RETRO_DEVICE_ID_JOYPAD_RIGHT = 7
RETRO_DEVICE_ID_JOYPAD_A = 8
RETRO_DEVICE_ID_JOYPAD_X = 9
RETRO_DEVICE_ID_JOYPAD_L = 10
RETRO_DEVICE_ID_JOYPAD_R = 11

RETRO_ENVIRONMENT_GET_OVERSCAN = 2
RETRO_ENVIRONMENT_GET_CAN_DUPE = 3
RETRO_ENVIRONMENT_SET_PERFORMANCE_LEVEL = 8
RETRO_ENVIRONMENT_GET_SYSTEM_DIRECTORY = 9
RETRO_ENVIRONMENT_SET_PIXEL_FORMAT = 10
RETRO_ENVIRONMENT_GET_VARIABLE = 15
RETRO_ENVIRONMENT_SET_VARIABLES = 16
RETRO_ENVIRONMENT_GET_VARIABLE_UPDATE = 17
RETRO_ENVIRONMENT_GET_LIBRETRO_PATH = 19
RETRO_ENVIRONMENT_GET_LOG_INTERFACE = 27
RETRO_ENVIRONMENT_GET_SAVE_DIRECTORY = 31
RETRO_ENVIRONMENT_GET_LANGUAGE = 39

RETRO_PIXEL_FORMAT_0RGB1555 = 0
RETRO_PIXEL_FORMAT_XRGB8888 = 1
RETRO_PIXEL_FORMAT_RGB565 = 2


class MewSNESLibretro:
    """libretro host: loads core .dll, runs frames, exposes RGB888 framebuffer."""

    def __init__(self) -> None:
        self.dll: Optional[ctypes.CDLL] = None
        self.core_path: str = ""
        self.library_name: str = ""
        self.library_version: str = ""

        self.pixel_format: int = RETRO_PIXEL_FORMAT_0RGB1555
        self.base_width: int = 256
        self.base_height: int = 224
        self.fps: float = 60.0
        self.sample_rate: float = 32040.0

        self.frame_w: int = 0
        self.frame_h: int = 0
        self.frame_rgb888: Optional[bytes] = None
        self.audio_buffer: bytearray = bytearray()

        self.inputs: dict[tuple[int, int], bool] = {}

        self._cb_env = ENV_CB(self._env_cb)
        self._cb_video = VIDEO_CB(self._video_cb)
        self._cb_audio_sample = AUDIO_SAMPLE_CB(self._audio_sample_cb)
        self._cb_audio_batch = AUDIO_BATCH_CB(self._audio_batch_cb)
        self._cb_input_poll = INPUT_POLL_CB(self._input_poll_cb)
        self._cb_input_state = INPUT_STATE_CB(self._input_state_cb)

        self._rom_buf = None
        self._dll_dir_handle = None

        self.loaded: bool = False
        self.rom_loaded: bool = False
        self.log: list[str] = []

    def load_core(self, path: str) -> str:
        core_dir = str(Path(path).parent)
        if sys.platform.startswith("win"):
            try:
                self._dll_dir_handle = os.add_dll_directory(core_dir)
            except (OSError, AttributeError):
                self._dll_dir_handle = None
        try:
            dll = ctypes.CDLL(str(path))
        except OSError as e:
            return f"load_core: {e}"

        try:
            self._bind(dll)
        except AttributeError as e:
            return f"core missing symbol: {e}"

        api_ver = dll.retro_api_version()
        if api_ver != RETRO_API_VERSION:
            return f"libretro API mismatch: core={api_ver} host={RETRO_API_VERSION}"

        si = retro_system_info()
        dll.retro_get_system_info(byref(si))
        self.library_name = (si.library_name or b"?").decode("latin-1", "replace")
        self.library_version = (si.library_version or b"?").decode("latin-1", "replace")

        dll.retro_set_environment(self._cb_env)
        dll.retro_set_video_refresh(self._cb_video)
        dll.retro_set_audio_sample(self._cb_audio_sample)
        dll.retro_set_audio_sample_batch(self._cb_audio_batch)
        dll.retro_set_input_poll(self._cb_input_poll)
        dll.retro_set_input_state(self._cb_input_state)
        dll.retro_init()

        self.dll = dll
        self.core_path = str(path)
        self.loaded = True
        self._log(f"[core] {self.library_name} {self.library_version}")
        return ""

    def load_rom(self, data: bytes) -> str:
        if not self.loaded or self.dll is None:
            return "core not loaded"
        if not data:
            return "empty ROM"

        buf = (c_uint8 * len(data)).from_buffer_copy(data)
        self._rom_buf = buf
        info = retro_game_info()
        info.path = None
        info.data = ctypes.cast(buf, c_void_p)
        info.size = len(data)
        info.meta = None

        ok = self.dll.retro_load_game(byref(info))
        if not ok:
            return "retro_load_game returned false"

        av = retro_system_av_info()
        self.dll.retro_get_system_av_info(byref(av))
        self.base_width = int(av.geometry.base_width or 256)
        self.base_height = int(av.geometry.base_height or 224)
        self.fps = float(av.timing.fps or 60.0)
        self.sample_rate = float(av.timing.sample_rate or 32040.0)

        self.rom_loaded = True
        self._log(
            f"[rom] {len(data)} bytes, {self.base_width}x{self.base_height}, "
            f"fps={self.fps:.2f}, sr={int(self.sample_rate)}"
        )
        return ""

    def run_frame(self) -> None:
        if self.loaded and self.rom_loaded and self.dll is not None:
            self.audio_buffer = bytearray()
            self.dll.retro_run()

    def reset(self) -> None:
        if self.loaded and self.rom_loaded and self.dll is not None:
            self.dll.retro_reset()
            self._log("[reset]")

    def unload(self) -> None:
        if self.dll is None:
            return
        try:
            if self.rom_loaded:
                self.dll.retro_unload_game()
            self.dll.retro_deinit()
        except Exception:
            pass
        self.dll = None
        self.loaded = False
        self.rom_loaded = False
        self.frame_rgb888 = None
        if self._dll_dir_handle is not None:
            try:
                self._dll_dir_handle.close()
            except Exception:
                pass
            self._dll_dir_handle = None
        self._log("[unload]")

    def set_button(self, port: int, button_id: int, pressed: bool) -> None:
        self.inputs[(port, button_id)] = bool(pressed)

    @staticmethod
    def _bind(dll: ctypes.CDLL) -> None:
        dll.retro_api_version.restype = c_uint
        dll.retro_api_version.argtypes = []
        dll.retro_init.restype = None
        dll.retro_init.argtypes = []
        dll.retro_deinit.restype = None
        dll.retro_deinit.argtypes = []
        dll.retro_get_system_info.restype = None
        dll.retro_get_system_info.argtypes = [POINTER(retro_system_info)]
        dll.retro_get_system_av_info.restype = None
        dll.retro_get_system_av_info.argtypes = [POINTER(retro_system_av_info)]
        dll.retro_set_environment.restype = None
        dll.retro_set_environment.argtypes = [ENV_CB]
        dll.retro_set_video_refresh.restype = None
        dll.retro_set_video_refresh.argtypes = [VIDEO_CB]
        dll.retro_set_audio_sample.restype = None
        dll.retro_set_audio_sample.argtypes = [AUDIO_SAMPLE_CB]
        dll.retro_set_audio_sample_batch.restype = None
        dll.retro_set_audio_sample_batch.argtypes = [AUDIO_BATCH_CB]
        dll.retro_set_input_poll.restype = None
        dll.retro_set_input_poll.argtypes = [INPUT_POLL_CB]
        dll.retro_set_input_state.restype = None
        dll.retro_set_input_state.argtypes = [INPUT_STATE_CB]
        dll.retro_load_game.restype = c_bool
        dll.retro_load_game.argtypes = [POINTER(retro_game_info)]
        dll.retro_unload_game.restype = None
        dll.retro_unload_game.argtypes = []
        dll.retro_run.restype = None
        dll.retro_run.argtypes = []
        dll.retro_reset.restype = None
        dll.retro_reset.argtypes = []

    def _env_cb(self, cmd: int, data: int) -> bool:
        if cmd == RETRO_ENVIRONMENT_GET_OVERSCAN:
            if data:
                ctypes.cast(data, POINTER(c_bool))[0] = False
            return True
        if cmd == RETRO_ENVIRONMENT_GET_CAN_DUPE:
            if data:
                ctypes.cast(data, POINTER(c_bool))[0] = True
            return True
        if cmd == RETRO_ENVIRONMENT_SET_PIXEL_FORMAT:
            if data:
                fmt = ctypes.cast(data, POINTER(c_uint))[0]
                if fmt in (
                    RETRO_PIXEL_FORMAT_0RGB1555,
                    RETRO_PIXEL_FORMAT_XRGB8888,
                    RETRO_PIXEL_FORMAT_RGB565,
                ):
                    self.pixel_format = int(fmt)
                    self._log(f"[env] pixel_format={fmt}")
                    return True
            return False
        if cmd == RETRO_ENVIRONMENT_SET_PERFORMANCE_LEVEL:
            return True
        if cmd == RETRO_ENVIRONMENT_SET_VARIABLES:
            return True
        if cmd == RETRO_ENVIRONMENT_GET_VARIABLE_UPDATE:
            if data:
                ctypes.cast(data, POINTER(c_bool))[0] = False
            return True
        if cmd == RETRO_ENVIRONMENT_GET_LANGUAGE:
            if data:
                ctypes.cast(data, POINTER(c_uint))[0] = 0
            return True
        return False

    def _video_cb(self, data: int, width: int, height: int, pitch: int) -> None:
        if not data or not width or not height:
            return
        w, h, p = int(width), int(height), int(pitch)
        self.frame_w, self.frame_h = w, h
        try:
            if self.pixel_format == RETRO_PIXEL_FORMAT_RGB565:
                self.frame_rgb888 = self._convert_rgb565(data, w, h, p)
            elif self.pixel_format == RETRO_PIXEL_FORMAT_0RGB1555:
                self.frame_rgb888 = self._convert_0rgb1555(data, w, h, p)
            elif self.pixel_format == RETRO_PIXEL_FORMAT_XRGB8888:
                self.frame_rgb888 = self._convert_xrgb8888(data, w, h, p)
        except Exception as e:  # noqa: BLE001
            self._log(f"[video] convert err: {e}")

    def _audio_sample_cb(self, left: int, right: int) -> None:
        self.audio_buffer += int(left).to_bytes(2, "little", signed=True)
        self.audio_buffer += int(right).to_bytes(2, "little", signed=True)

    def _audio_batch_cb(self, data: int, frames: int) -> int:
        n = int(frames)
        if not data or n <= 0:
            return n
        size = n * 4
        buf = (c_uint8 * size).from_address(data)
        self.audio_buffer += bytes(buf)
        return n

    def _input_poll_cb(self) -> None:
        return

    def _input_state_cb(self, port: int, device: int, index: int, id_: int) -> int:
        if device != RETRO_DEVICE_JOYPAD:
            return 0
        return 1 if self.inputs.get((int(port), int(id_)), False) else 0

    def _convert_rgb565(self, data_addr: int, w: int, h: int, pitch: int) -> bytes:
        if _HAS_FAST:
            return _fast_565(data_addr, w, h, pitch)
        if not _HAS_NUMPY:
            return self._convert_rgb565_slow(data_addr, w, h, pitch)
        buf = (c_uint8 * (pitch * h)).from_address(data_addr)
        full = np.frombuffer(buf, dtype=np.uint8).reshape(h, pitch)
        row16 = full[:, : w * 2].copy().view(np.uint16).reshape(h, w)
        r = ((row16 >> 11) & 0x1F).astype(np.uint8) << 3
        g = ((row16 >> 5) & 0x3F).astype(np.uint8) << 2
        b = (row16 & 0x1F).astype(np.uint8) << 3
        return np.dstack([r, g, b]).tobytes()

    def _convert_0rgb1555(self, data_addr: int, w: int, h: int, pitch: int) -> bytes:
        if not _HAS_NUMPY:
            return self._convert_rgb565_slow(data_addr, w, h, pitch, fmt1555=True)
        buf = (c_uint8 * (pitch * h)).from_address(data_addr)
        full = np.frombuffer(buf, dtype=np.uint8).reshape(h, pitch)
        row16 = full[:, : w * 2].copy().view(np.uint16).reshape(h, w)
        r = ((row16 >> 10) & 0x1F).astype(np.uint8) << 3
        g = ((row16 >> 5) & 0x1F).astype(np.uint8) << 3
        b = (row16 & 0x1F).astype(np.uint8) << 3
        return np.dstack([r, g, b]).tobytes()

    def _convert_xrgb8888(self, data_addr: int, w: int, h: int, pitch: int) -> bytes:
        buf = (c_uint8 * (pitch * h)).from_address(data_addr)
        if _HAS_NUMPY:
            full = np.frombuffer(buf, dtype=np.uint8).reshape(h, pitch)
            rgbx = full[:, : w * 4].reshape(h, w, 4)
            return np.ascontiguousarray(rgbx[:, :, [2, 1, 0]]).tobytes()
        out = bytearray(w * h * 3)
        src = bytes(buf)
        for y in range(h):
            sbase = y * pitch
            dbase = y * w * 3
            for x in range(w):
                so = sbase + x * 4
                do = dbase + x * 3
                out[do + 0] = src[so + 2]
                out[do + 1] = src[so + 1]
                out[do + 2] = src[so + 0]
        return bytes(out)

    def _convert_rgb565_slow(self, data_addr, w, h, pitch, fmt1555=False) -> bytes:
        buf = (c_uint8 * (pitch * h)).from_address(data_addr)
        src = bytes(buf)
        out = bytearray(w * h * 3)
        for y in range(h):
            sbase = y * pitch
            dbase = y * w * 3
            for x in range(w):
                so = sbase + x * 2
                px = src[so] | (src[so + 1] << 8)
                if fmt1555:
                    r = ((px >> 10) & 0x1F) << 3
                    g = ((px >> 5) & 0x1F) << 3
                    b = (px & 0x1F) << 3
                else:
                    r = ((px >> 11) & 0x1F) << 3
                    g = ((px >> 5) & 0x3F) << 2
                    b = (px & 0x1F) << 3
                do = dbase + x * 3
                out[do + 0] = r
                out[do + 1] = g
                out[do + 2] = b
        return bytes(out)

    def _log(self, msg: str) -> None:
        self.log.append(msg)
        if len(self.log) > 64:
            self.log = self.log[-64:]


# =====================================================================
# Pre-baked SNES core: 65C816 CPU + memory bus (mewsnes baked-cpu)
# =====================================================================

# Status flag bit masks
F_N = 0x80
F_V = 0x40
F_M = 0x20
F_X = 0x10
F_D = 0x08
F_I = 0x04
F_Z = 0x02
F_C = 0x01


class SNESBus:
    """SNES memory bus. Maps LoROM / HiROM / ExHiROM + WRAM + register stubs."""

    def __init__(self, rom: bytes, layout: str) -> None:
        self.rom = rom
        self.layout = layout
        self.wram = bytearray(0x20000)
        self.sram = bytearray(0x8000)
        self._fake_vblank = False
        self._vblank_cnt = 0
        self.reg_writes: list[tuple[int, int]] = []
        self.last_unmapped_read = -1
        self.last_unmapped_write = -1

    def _rom_off(self, addr24: int) -> int:
        bank = (addr24 >> 16) & 0xFF
        addr = addr24 & 0xFFFF
        n = len(self.rom)
        if n == 0:
            return -1
        if self.layout in ("LoROM", "SA-1 LoROM", "ExLoROM"):
            if addr >= 0x8000:
                off = ((bank & 0x7F) << 15) | (addr & 0x7FFF)
                return off % n if n else -1
            return -1
        if self.layout == "HiROM":
            off = ((bank & 0x3F) << 16) | (addr & 0xFFFF)
            return off % n if n else -1
        if self.layout == "ExHiROM":
            if 0x80 <= bank <= 0xFF and addr >= 0x8000:
                off = ((bank & 0x3F) << 16) | addr
            elif 0x40 <= bank <= 0x7D:
                off = 0x400000 + (((bank & 0x3F)) << 16) + (addr & 0xFFFF)
            elif bank <= 0x3F and addr >= 0x8000:
                off = ((bank & 0x3F) << 16) | addr
            else:
                return -1
            return off % n if n else -1
        if addr >= 0x8000:
            off = ((bank & 0x7F) << 15) | (addr & 0x7FFF)
            return off % n if n else -1
        return -1

    def read8(self, addr24: int) -> int:
        bank = (addr24 >> 16) & 0xFF
        addr = addr24 & 0xFFFF
        # LoROM/HiROM system banks 00-3F and 80-BF
        if (0x00 <= bank <= 0x3F) or (0x80 <= bank <= 0xBF):
            if addr <= 0x1FFF:
                return self.wram[addr]
            if 0x2100 <= addr <= 0x213F:
                return self._read_ppu(addr)
            if 0x4200 <= addr <= 0x437F:
                return self._read_cpu_reg(addr)
            if addr >= 0x8000:
                off = self._rom_off(addr24)
                return self.rom[off] if 0 <= off < len(self.rom) else 0
            # HiROM data area $6000-$7FFF
            if self.layout in ("HiROM", "ExHiROM") and 0x6000 <= addr <= 0x7FFF:
                off = self._rom_off(addr24)
                return self.rom[off] if 0 <= off < len(self.rom) else 0
            self.last_unmapped_read = addr24
            return 0
        if bank in (0x7E, 0x7F):
            woff = ((bank & 1) << 16) | addr
            return self.wram[woff] if woff < len(self.wram) else 0
        if (0x40 <= bank <= 0x7D) or (0xC0 <= bank <= 0xFF):
            off = self._rom_off(addr24)
            return self.rom[off] if 0 <= off < len(self.rom) else 0
        self.last_unmapped_read = addr24
        return 0

    def write8(self, addr24: int, val: int) -> None:
        val &= 0xFF
        bank = (addr24 >> 16) & 0xFF
        addr = addr24 & 0xFFFF
        if (0x00 <= bank <= 0x3F) or (0x80 <= bank <= 0xBF):
            if addr <= 0x1FFF:
                self.wram[addr] = val
                return
            if 0x2100 <= addr <= 0x213F or 0x4200 <= addr <= 0x437F:
                self.reg_writes.append((addr24, val))
                if len(self.reg_writes) > 64:
                    self.reg_writes = self.reg_writes[-64:]
                return
            self.last_unmapped_write = addr24
            return
        if bank in (0x7E, 0x7F):
            woff = ((bank & 1) << 16) | addr
            if woff < len(self.wram):
                self.wram[woff] = val
            return
        # ROM area writes ignored (no SRAM mapping yet)
        self.last_unmapped_write = addr24

    def read16(self, addr24: int) -> int:
        lo = self.read8(addr24)
        hi = self.read8((addr24 + 1) & 0xFFFFFF)
        return (hi << 8) | lo

    def write16(self, addr24: int, val: int) -> None:
        self.write8(addr24, val & 0xFF)
        self.write8((addr24 + 1) & 0xFFFFFF, (val >> 8) & 0xFF)

    def _read_ppu(self, addr: int) -> int:
        # Just enough to keep simple init loops from spinning forever.
        if addr == 0x2137:  # SLHV — H/V counter latch
            return 0
        if addr == 0x213F:  # STAT78 — PPU2 status (bit 7 = field, bit 6 = interlace toggle)
            return 0x01
        return 0

    def _read_cpu_reg(self, addr: int) -> int:
        if addr == 0x4210:  # RDNMI — bit 7 = NMI flag (auto-clears on read)
            self._vblank_cnt += 1
            if self._vblank_cnt > 256:
                self._vblank_cnt = 0
                self._fake_vblank = True
            v = 0x82 if self._fake_vblank else 0x02  # version low nibble = 2
            self._fake_vblank = False
            return v
        if addr == 0x4211:  # TIMEUP — bit 7 = IRQ flag
            return 0
        if addr == 0x4212:  # HVBJOY — bit 7 vblank, bit 6 hblank, bit 0 auto-joy busy
            self._vblank_cnt += 1
            return 0x80 if (self._vblank_cnt & 0x80) else 0x00
        if addr == 0x4213:  # RDIO
            return 0
        if 0x4214 <= addr <= 0x4217:  # math result regs
            return 0
        if 0x4218 <= addr <= 0x421F:  # joypad auto-read
            return 0
        return 0


class CPU65816:
    """65C816 CPU. Executes ~95 of the most common opcodes. Halts cleanly on
    anything unimplemented so the missing opcode can be added."""

    def __init__(self, bus: SNESBus) -> None:
        self.bus = bus
        self.a = 0
        self.x = 0
        self.y = 0
        self.s = 0x01FF
        self.d = 0
        self.pb = 0
        self.db = 0
        self.pc = 0
        self.p = F_M | F_X | F_I  # 0x34
        self.e = True
        self.halted = False
        self.halt_reason = ""
        self.cycles = 0
        self.instr_count = 0
        self.trace: list[str] = []
        self.unimpl_hits: dict[int, int] = {}
        self._ops: dict[int, tuple[str, Callable[[], None]]] = {}
        self._build_dispatch()

    # ---- public ----

    def reset(self, reset_vec: int) -> None:
        self.e = True
        self.p = (self.p | F_M | F_X | F_I) & ~F_D & 0xFF
        self.a = self.a & 0xFF
        self.x = self.x & 0xFF
        self.y = self.y & 0xFF
        self.s = 0x01FF
        self.d = 0
        self.db = 0
        self.pb = 0
        self.pc = reset_vec & 0xFFFF
        self.halted = False
        self.halt_reason = ""
        self.cycles = 0
        self.instr_count = 0
        self.trace.clear()
        self.unimpl_hits.clear()
        self._t(f"[reset] PB:PC=00:{self.pc:04X}")

    def step(self) -> None:
        if self.halted:
            return
        op_pc = self.pc
        op_pb = self.pb
        op_addr = (op_pb << 16) | op_pc
        op = self._fetch8()
        entry = self._ops.get(op)
        if entry is None:
            self.halted = True
            self.halt_reason = f"unimplemented opcode ${op:02X} @ ${op_addr:06X}"
            self._t(f"{op_addr:06X}: UNK ${op:02X}  -- halt")
            self.unimpl_hits[op] = self.unimpl_hits.get(op, 0) + 1
            return
        name, fn = entry
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            self.halted = True
            self.halt_reason = f"exception in ${op:02X} {name}: {e}"
            self._t(f"{op_addr:06X}: ERR {op:02X} {name} -> {e}")
            return
        self.cycles += 1
        self.instr_count += 1
        self._t(f"{op_addr:06X}: {op:02X} {name}")

    def step_many(self, n: int) -> int:
        """Run up to n instructions. Returns instructions actually run."""
        ran = 0
        while ran < n and not self.halted:
            self.step()
            ran += 1
        return ran

    def regs_str(self) -> str:
        flags = "".join(
            (c if (self.p & b) else "-")
            for c, b in zip("nvmxdizc", (F_N, F_V, F_M, F_X, F_D, F_I, F_Z, F_C))
        )
        return (
            f"A:{self.a:04X} X:{self.x:04X} Y:{self.y:04X} S:{self.s:04X} "
            f"D:{self.d:04X} DB:{self.db:02X} PB:PC={self.pb:02X}:{self.pc:04X} "
            f"P:{self.p:02X} [{flags}] E:{int(self.e)}"
        )

    # ---- internals ----

    def _t(self, msg: str) -> None:
        self.trace.append(msg)
        if len(self.trace) > 128:
            self.trace = self.trace[-128:]

    def _m8(self) -> bool:
        return self.e or bool(self.p & F_M)

    def _x8(self) -> bool:
        return self.e or bool(self.p & F_X)

    def _set_nz_a(self) -> None:
        if self._m8():
            v = self.a & 0xFF
            self.p = (self.p & ~F_N & ~F_Z) & 0xFF
            if v & 0x80:
                self.p |= F_N
            if v == 0:
                self.p |= F_Z
        else:
            v = self.a & 0xFFFF
            self.p = (self.p & ~F_N & ~F_Z) & 0xFF
            if v & 0x8000:
                self.p |= F_N
            if v == 0:
                self.p |= F_Z

    def _set_nz_x(self) -> None:
        if self._x8():
            v = self.x & 0xFF
            self.p = (self.p & ~F_N & ~F_Z) & 0xFF
            if v & 0x80:
                self.p |= F_N
            if v == 0:
                self.p |= F_Z
        else:
            v = self.x & 0xFFFF
            self.p = (self.p & ~F_N & ~F_Z) & 0xFF
            if v & 0x8000:
                self.p |= F_N
            if v == 0:
                self.p |= F_Z

    def _set_nz_y(self) -> None:
        if self._x8():
            v = self.y & 0xFF
            self.p = (self.p & ~F_N & ~F_Z) & 0xFF
            if v & 0x80:
                self.p |= F_N
            if v == 0:
                self.p |= F_Z
        else:
            v = self.y & 0xFFFF
            self.p = (self.p & ~F_N & ~F_Z) & 0xFF
            if v & 0x8000:
                self.p |= F_N
            if v == 0:
                self.p |= F_Z

    def _set_nz(self, v: int, bits: int = 8) -> None:
        mask = (1 << bits) - 1
        v &= mask
        self.p = (self.p & ~F_N & ~F_Z) & 0xFF
        if v & (1 << (bits - 1)):
            self.p |= F_N
        if v == 0:
            self.p |= F_Z

    def _fetch8(self) -> int:
        v = self.bus.read8((self.pb << 16) | self.pc)
        self.pc = (self.pc + 1) & 0xFFFF
        return v

    def _fetch16(self) -> int:
        lo = self._fetch8()
        hi = self._fetch8()
        return (hi << 8) | lo

    def _fetch24(self) -> int:
        lo = self._fetch8()
        mid = self._fetch8()
        hi = self._fetch8()
        return (hi << 16) | (mid << 8) | lo

    def _readM(self, addr24: int) -> int:
        if self._m8():
            return self.bus.read8(addr24)
        return self.bus.read16(addr24)

    def _writeM(self, addr24: int, val: int) -> None:
        if self._m8():
            self.bus.write8(addr24, val & 0xFF)
        else:
            self.bus.write16(addr24, val & 0xFFFF)

    def _readX(self, addr24: int) -> int:
        if self._x8():
            return self.bus.read8(addr24)
        return self.bus.read16(addr24)

    def _writeX(self, addr24: int, val: int) -> None:
        if self._x8():
            self.bus.write8(addr24, val & 0xFF)
        else:
            self.bus.write16(addr24, val & 0xFFFF)

    # Stack
    def _push8(self, val: int) -> None:
        self.bus.write8(self.s & 0xFFFF, val & 0xFF)
        self.s = (self.s - 1) & 0xFFFF
        if self.e:
            self.s = 0x0100 | (self.s & 0xFF)

    def _pop8(self) -> int:
        self.s = (self.s + 1) & 0xFFFF
        if self.e:
            self.s = 0x0100 | (self.s & 0xFF)
        return self.bus.read8(self.s & 0xFFFF)

    def _push16(self, val: int) -> None:
        self._push8((val >> 8) & 0xFF)
        self._push8(val & 0xFF)

    def _pop16(self) -> int:
        lo = self._pop8()
        hi = self._pop8()
        return (hi << 8) | lo

    def _pushM(self, val: int) -> None:
        if self._m8():
            self._push8(val & 0xFF)
        else:
            self._push16(val & 0xFFFF)

    def _popM(self) -> int:
        return self._pop8() if self._m8() else self._pop16()

    def _pushX(self, val: int) -> None:
        if self._x8():
            self._push8(val & 0xFF)
        else:
            self._push16(val & 0xFFFF)

    def _popX(self) -> int:
        return self._pop8() if self._x8() else self._pop16()

    # ---- addressing modes (return 24-bit effective address) ----

    def _am_imm_m(self) -> int:
        a = (self.pb << 16) | self.pc
        self.pc = (self.pc + (1 if self._m8() else 2)) & 0xFFFF
        return a

    def _am_imm_x(self) -> int:
        a = (self.pb << 16) | self.pc
        self.pc = (self.pc + (1 if self._x8() else 2)) & 0xFFFF
        return a

    def _am_imm8(self) -> int:
        a = (self.pb << 16) | self.pc
        self.pc = (self.pc + 1) & 0xFFFF
        return a

    def _am_dp(self) -> int:
        return (self.d + self._fetch8()) & 0xFFFF

    def _am_dp_x(self) -> int:
        return (self.d + self._fetch8() + self.x) & 0xFFFF

    def _am_dp_y(self) -> int:
        return (self.d + self._fetch8() + self.y) & 0xFFFF

    def _am_dp_ind(self) -> int:
        ptr = (self.d + self._fetch8()) & 0xFFFF
        lo = self.bus.read8(ptr)
        hi = self.bus.read8((ptr + 1) & 0xFFFF)
        return (self.db << 16) | (hi << 8) | lo

    def _am_dp_ind_long(self) -> int:
        ptr = (self.d + self._fetch8()) & 0xFFFF
        lo = self.bus.read8(ptr)
        mid = self.bus.read8((ptr + 1) & 0xFFFF)
        hi = self.bus.read8((ptr + 2) & 0xFFFF)
        return (hi << 16) | (mid << 8) | lo

    def _am_dp_ind_y(self) -> int:
        ptr = (self.d + self._fetch8()) & 0xFFFF
        lo = self.bus.read8(ptr)
        hi = self.bus.read8((ptr + 1) & 0xFFFF)
        base = (self.db << 16) | (hi << 8) | lo
        return (base + self.y) & 0xFFFFFF

    def _am_dp_ind_long_y(self) -> int:
        ptr = (self.d + self._fetch8()) & 0xFFFF
        lo = self.bus.read8(ptr)
        mid = self.bus.read8((ptr + 1) & 0xFFFF)
        hi = self.bus.read8((ptr + 2) & 0xFFFF)
        base = (hi << 16) | (mid << 8) | lo
        return (base + self.y) & 0xFFFFFF

    def _am_dp_x_ind(self) -> int:
        ptr = (self.d + self._fetch8() + self.x) & 0xFFFF
        lo = self.bus.read8(ptr)
        hi = self.bus.read8((ptr + 1) & 0xFFFF)
        return (self.db << 16) | (hi << 8) | lo

    def _am_sr(self) -> int:
        return (self.s + self._fetch8()) & 0xFFFF

    def _am_sr_y(self) -> int:
        ptr = (self.s + self._fetch8()) & 0xFFFF
        lo = self.bus.read8(ptr)
        hi = self.bus.read8((ptr + 1) & 0xFFFF)
        base = (self.db << 16) | (hi << 8) | lo
        return (base + self.y) & 0xFFFFFF

    def _am_abs(self) -> int:
        return (self.db << 16) | self._fetch16()

    def _am_abs_x(self) -> int:
        return ((self.db << 16) | self._fetch16()) + self.x & 0xFFFFFF

    def _am_abs_y(self) -> int:
        return ((self.db << 16) | self._fetch16()) + self.y & 0xFFFFFF

    def _am_abs_long(self) -> int:
        return self._fetch24()

    def _am_abs_long_x(self) -> int:
        return (self._fetch24() + self.x) & 0xFFFFFF

    # ---- operations ----

    def _op_lda(self, addr: int) -> None:
        v = self._readM(addr)
        if self._m8():
            self.a = (self.a & 0xFF00) | (v & 0xFF)
        else:
            self.a = v & 0xFFFF
        self._set_nz_a()

    def _op_sta(self, addr: int) -> None:
        self._writeM(addr, self.a)

    def _op_ldx(self, addr: int) -> None:
        v = self._readX(addr)
        self.x = (v & 0xFF) if self._x8() else (v & 0xFFFF)
        self._set_nz_x()

    def _op_stx(self, addr: int) -> None:
        self._writeX(addr, self.x)

    def _op_ldy(self, addr: int) -> None:
        v = self._readX(addr)
        self.y = (v & 0xFF) if self._x8() else (v & 0xFFFF)
        self._set_nz_y()

    def _op_sty(self, addr: int) -> None:
        self._writeX(addr, self.y)

    def _op_stz(self, addr: int) -> None:
        self._writeM(addr, 0)

    def _op_and(self, addr: int) -> None:
        v = self._readM(addr)
        if self._m8():
            self.a = (self.a & 0xFF00) | ((self.a & v) & 0xFF)
        else:
            self.a = self.a & v & 0xFFFF
        self._set_nz_a()

    def _op_ora(self, addr: int) -> None:
        v = self._readM(addr)
        if self._m8():
            self.a = (self.a & 0xFF00) | ((self.a | v) & 0xFF)
        else:
            self.a = (self.a | v) & 0xFFFF
        self._set_nz_a()

    def _op_eor(self, addr: int) -> None:
        v = self._readM(addr)
        if self._m8():
            self.a = (self.a & 0xFF00) | ((self.a ^ v) & 0xFF)
        else:
            self.a = (self.a ^ v) & 0xFFFF
        self._set_nz_a()

    def _op_adc(self, addr: int) -> None:
        v = self._readM(addr)
        c_in = 1 if (self.p & F_C) else 0
        if self._m8():
            a = self.a & 0xFF
            r = a + v + c_in
            ovf = (~(a ^ v) & (a ^ r)) & 0x80
            self.p &= ~F_C & ~F_V & 0xFF
            if r > 0xFF:
                self.p |= F_C
            if ovf:
                self.p |= F_V
            self.a = (self.a & 0xFF00) | (r & 0xFF)
        else:
            a = self.a & 0xFFFF
            r = a + v + c_in
            ovf = (~(a ^ v) & (a ^ r)) & 0x8000
            self.p &= ~F_C & ~F_V & 0xFF
            if r > 0xFFFF:
                self.p |= F_C
            if ovf:
                self.p |= F_V
            self.a = r & 0xFFFF
        self._set_nz_a()

    def _op_sbc(self, addr: int) -> None:
        v = self._readM(addr)
        c_in = 1 if (self.p & F_C) else 0
        if self._m8():
            a = self.a & 0xFF
            v ^= 0xFF
            r = a + v + c_in
            ovf = (~(a ^ v) & (a ^ r)) & 0x80
            self.p &= ~F_C & ~F_V & 0xFF
            if r > 0xFF:
                self.p |= F_C
            if ovf:
                self.p |= F_V
            self.a = (self.a & 0xFF00) | (r & 0xFF)
        else:
            a = self.a & 0xFFFF
            v ^= 0xFFFF
            r = a + v + c_in
            ovf = (~(a ^ v) & (a ^ r)) & 0x8000
            self.p &= ~F_C & ~F_V & 0xFF
            if r > 0xFFFF:
                self.p |= F_C
            if ovf:
                self.p |= F_V
            self.a = r & 0xFFFF
        self._set_nz_a()

    def _cmp_common(self, reg: int, v: int, eight: bool) -> None:
        if eight:
            a = reg & 0xFF
            v &= 0xFF
            r = (a - v) & 0x1FF
            self.p &= ~F_C & 0xFF
            if a >= v:
                self.p |= F_C
            self._set_nz(r, 8)
        else:
            a = reg & 0xFFFF
            v &= 0xFFFF
            r = (a - v) & 0x1FFFF
            self.p &= ~F_C & 0xFF
            if a >= v:
                self.p |= F_C
            self._set_nz(r, 16)

    def _op_cmp(self, addr: int) -> None:
        self._cmp_common(self.a, self._readM(addr), self._m8())

    def _op_cpx(self, addr: int) -> None:
        self._cmp_common(self.x, self._readX(addr), self._x8())

    def _op_cpy(self, addr: int) -> None:
        self._cmp_common(self.y, self._readX(addr), self._x8())

    def _op_bit(self, addr: int) -> None:
        v = self._readM(addr)
        if self._m8():
            r = (self.a & 0xFF) & v
            self.p &= ~F_Z & ~F_N & ~F_V & 0xFF
            if r == 0:
                self.p |= F_Z
            if v & 0x80:
                self.p |= F_N
            if v & 0x40:
                self.p |= F_V
        else:
            r = (self.a & 0xFFFF) & v
            self.p &= ~F_Z & ~F_N & ~F_V & 0xFF
            if r == 0:
                self.p |= F_Z
            if v & 0x8000:
                self.p |= F_N
            if v & 0x4000:
                self.p |= F_V

    def _op_inc_mem(self, addr: int) -> None:
        v = self._readM(addr)
        if self._m8():
            v = (v + 1) & 0xFF
        else:
            v = (v + 1) & 0xFFFF
        self._writeM(addr, v)
        self._set_nz(v, 8 if self._m8() else 16)

    def _op_dec_mem(self, addr: int) -> None:
        v = self._readM(addr)
        if self._m8():
            v = (v - 1) & 0xFF
        else:
            v = (v - 1) & 0xFFFF
        self._writeM(addr, v)
        self._set_nz(v, 8 if self._m8() else 16)

    def _op_asl_a(self) -> None:
        if self._m8():
            v = (self.a & 0xFF) << 1
            self.p = (self.p & ~F_C) | (F_C if v & 0x100 else 0)
            self.a = (self.a & 0xFF00) | (v & 0xFF)
        else:
            v = (self.a & 0xFFFF) << 1
            self.p = (self.p & ~F_C) | (F_C if v & 0x10000 else 0)
            self.a = v & 0xFFFF
        self._set_nz_a()

    def _op_lsr_a(self) -> None:
        if self._m8():
            v = self.a & 0xFF
            self.p = (self.p & ~F_C) | (F_C if v & 1 else 0)
            v >>= 1
            self.a = (self.a & 0xFF00) | (v & 0xFF)
        else:
            v = self.a & 0xFFFF
            self.p = (self.p & ~F_C) | (F_C if v & 1 else 0)
            v >>= 1
            self.a = v & 0xFFFF
        self._set_nz_a()

    def _op_rol_a(self) -> None:
        c_in = 1 if (self.p & F_C) else 0
        if self._m8():
            v = ((self.a & 0xFF) << 1) | c_in
            self.p = (self.p & ~F_C) | (F_C if v & 0x100 else 0)
            self.a = (self.a & 0xFF00) | (v & 0xFF)
        else:
            v = ((self.a & 0xFFFF) << 1) | c_in
            self.p = (self.p & ~F_C) | (F_C if v & 0x10000 else 0)
            self.a = v & 0xFFFF
        self._set_nz_a()

    def _op_ror_a(self) -> None:
        c_in = 1 if (self.p & F_C) else 0
        if self._m8():
            v = self.a & 0xFF
            new_c = v & 1
            v = (v >> 1) | (c_in << 7)
            self.p = (self.p & ~F_C) | (F_C if new_c else 0)
            self.a = (self.a & 0xFF00) | (v & 0xFF)
        else:
            v = self.a & 0xFFFF
            new_c = v & 1
            v = (v >> 1) | (c_in << 15)
            self.p = (self.p & ~F_C) | (F_C if new_c else 0)
            self.a = v & 0xFFFF
        self._set_nz_a()

    # branch
    def _branch(self, cond: bool) -> None:
        off = self._fetch8()
        if off & 0x80:
            off -= 0x100
        if cond:
            self.pc = (self.pc + off) & 0xFFFF

    # ---- dispatch table ----

    def _build_dispatch(self) -> None:
        O = self._ops

        # flag ops
        O[0x18] = ("CLC", lambda: self._set_flag(F_C, False))
        O[0x38] = ("SEC", lambda: self._set_flag(F_C, True))
        O[0x58] = ("CLI", lambda: self._set_flag(F_I, False))
        O[0x78] = ("SEI", lambda: self._set_flag(F_I, True))
        O[0xB8] = ("CLV", lambda: self._set_flag(F_V, False))
        O[0xD8] = ("CLD", lambda: self._set_flag(F_D, False))
        O[0xF8] = ("SED", lambda: self._set_flag(F_D, True))
        O[0xC2] = ("REP #", self._op_rep)
        O[0xE2] = ("SEP #", self._op_sep)
        O[0xFB] = ("XCE", self._op_xce)

        # LDA family
        O[0xA9] = ("LDA #", lambda: self._op_lda(self._am_imm_m()))
        O[0xA5] = ("LDA dp", lambda: self._op_lda(self._am_dp()))
        O[0xB5] = ("LDA dp,X", lambda: self._op_lda(self._am_dp_x()))
        O[0xB2] = ("LDA (dp)", lambda: self._op_lda(self._am_dp_ind()))
        O[0xA7] = ("LDA [dp]", lambda: self._op_lda(self._am_dp_ind_long()))
        O[0xB1] = ("LDA (dp),Y", lambda: self._op_lda(self._am_dp_ind_y()))
        O[0xB7] = ("LDA [dp],Y", lambda: self._op_lda(self._am_dp_ind_long_y()))
        O[0xA1] = ("LDA (dp,X)", lambda: self._op_lda(self._am_dp_x_ind()))
        O[0xA3] = ("LDA sr,S", lambda: self._op_lda(self._am_sr()))
        O[0xB3] = ("LDA (sr,S),Y", lambda: self._op_lda(self._am_sr_y()))
        O[0xAD] = ("LDA abs", lambda: self._op_lda(self._am_abs()))
        O[0xBD] = ("LDA abs,X", lambda: self._op_lda(self._am_abs_x()))
        O[0xB9] = ("LDA abs,Y", lambda: self._op_lda(self._am_abs_y()))
        O[0xAF] = ("LDA long", lambda: self._op_lda(self._am_abs_long()))
        O[0xBF] = ("LDA long,X", lambda: self._op_lda(self._am_abs_long_x()))

        # STA family
        O[0x85] = ("STA dp", lambda: self._op_sta(self._am_dp()))
        O[0x95] = ("STA dp,X", lambda: self._op_sta(self._am_dp_x()))
        O[0x92] = ("STA (dp)", lambda: self._op_sta(self._am_dp_ind()))
        O[0x87] = ("STA [dp]", lambda: self._op_sta(self._am_dp_ind_long()))
        O[0x91] = ("STA (dp),Y", lambda: self._op_sta(self._am_dp_ind_y()))
        O[0x97] = ("STA [dp],Y", lambda: self._op_sta(self._am_dp_ind_long_y()))
        O[0x81] = ("STA (dp,X)", lambda: self._op_sta(self._am_dp_x_ind()))
        O[0x83] = ("STA sr,S", lambda: self._op_sta(self._am_sr()))
        O[0x93] = ("STA (sr,S),Y", lambda: self._op_sta(self._am_sr_y()))
        O[0x8D] = ("STA abs", lambda: self._op_sta(self._am_abs()))
        O[0x9D] = ("STA abs,X", lambda: self._op_sta(self._am_abs_x()))
        O[0x99] = ("STA abs,Y", lambda: self._op_sta(self._am_abs_y()))
        O[0x8F] = ("STA long", lambda: self._op_sta(self._am_abs_long()))
        O[0x9F] = ("STA long,X", lambda: self._op_sta(self._am_abs_long_x()))

        # LDX/LDY/STX/STY
        O[0xA2] = ("LDX #", lambda: self._op_ldx(self._am_imm_x()))
        O[0xA6] = ("LDX dp", lambda: self._op_ldx(self._am_dp()))
        O[0xB6] = ("LDX dp,Y", lambda: self._op_ldx(self._am_dp_y()))
        O[0xAE] = ("LDX abs", lambda: self._op_ldx(self._am_abs()))
        O[0xBE] = ("LDX abs,Y", lambda: self._op_ldx(self._am_abs_y()))

        O[0xA0] = ("LDY #", lambda: self._op_ldy(self._am_imm_x()))
        O[0xA4] = ("LDY dp", lambda: self._op_ldy(self._am_dp()))
        O[0xB4] = ("LDY dp,X", lambda: self._op_ldy(self._am_dp_x()))
        O[0xAC] = ("LDY abs", lambda: self._op_ldy(self._am_abs()))
        O[0xBC] = ("LDY abs,X", lambda: self._op_ldy(self._am_abs_x()))

        O[0x86] = ("STX dp", lambda: self._op_stx(self._am_dp()))
        O[0x96] = ("STX dp,Y", lambda: self._op_stx(self._am_dp_y()))
        O[0x8E] = ("STX abs", lambda: self._op_stx(self._am_abs()))

        O[0x84] = ("STY dp", lambda: self._op_sty(self._am_dp()))
        O[0x94] = ("STY dp,X", lambda: self._op_sty(self._am_dp_x()))
        O[0x8C] = ("STY abs", lambda: self._op_sty(self._am_abs()))

        # STZ
        O[0x64] = ("STZ dp", lambda: self._op_stz(self._am_dp()))
        O[0x74] = ("STZ dp,X", lambda: self._op_stz(self._am_dp_x()))
        O[0x9C] = ("STZ abs", lambda: self._op_stz(self._am_abs()))
        O[0x9E] = ("STZ abs,X", lambda: self._op_stz(self._am_abs_x()))

        # ALU
        O[0x29] = ("AND #", lambda: self._op_and(self._am_imm_m()))
        O[0x2D] = ("AND abs", lambda: self._op_and(self._am_abs()))
        O[0x25] = ("AND dp", lambda: self._op_and(self._am_dp()))
        O[0x3D] = ("AND abs,X", lambda: self._op_and(self._am_abs_x()))
        O[0x39] = ("AND abs,Y", lambda: self._op_and(self._am_abs_y()))
        O[0x2F] = ("AND long", lambda: self._op_and(self._am_abs_long()))
        O[0x09] = ("ORA #", lambda: self._op_ora(self._am_imm_m()))
        O[0x0D] = ("ORA abs", lambda: self._op_ora(self._am_abs()))
        O[0x05] = ("ORA dp", lambda: self._op_ora(self._am_dp()))
        O[0x1D] = ("ORA abs,X", lambda: self._op_ora(self._am_abs_x()))
        O[0x19] = ("ORA abs,Y", lambda: self._op_ora(self._am_abs_y()))
        O[0x0F] = ("ORA long", lambda: self._op_ora(self._am_abs_long()))
        O[0x49] = ("EOR #", lambda: self._op_eor(self._am_imm_m()))
        O[0x4D] = ("EOR abs", lambda: self._op_eor(self._am_abs()))
        O[0x45] = ("EOR dp", lambda: self._op_eor(self._am_dp()))
        O[0x69] = ("ADC #", lambda: self._op_adc(self._am_imm_m()))
        O[0x6D] = ("ADC abs", lambda: self._op_adc(self._am_abs()))
        O[0x65] = ("ADC dp", lambda: self._op_adc(self._am_dp()))
        O[0xE9] = ("SBC #", lambda: self._op_sbc(self._am_imm_m()))
        O[0xED] = ("SBC abs", lambda: self._op_sbc(self._am_abs()))
        O[0xE5] = ("SBC dp", lambda: self._op_sbc(self._am_dp()))

        O[0xC9] = ("CMP #", lambda: self._op_cmp(self._am_imm_m()))
        O[0xCD] = ("CMP abs", lambda: self._op_cmp(self._am_abs()))
        O[0xC5] = ("CMP dp", lambda: self._op_cmp(self._am_dp()))
        O[0xDD] = ("CMP abs,X", lambda: self._op_cmp(self._am_abs_x()))
        O[0xD9] = ("CMP abs,Y", lambda: self._op_cmp(self._am_abs_y()))
        O[0xCF] = ("CMP long", lambda: self._op_cmp(self._am_abs_long()))
        O[0xE0] = ("CPX #", lambda: self._op_cpx(self._am_imm_x()))
        O[0xEC] = ("CPX abs", lambda: self._op_cpx(self._am_abs()))
        O[0xE4] = ("CPX dp", lambda: self._op_cpx(self._am_dp()))
        O[0xC0] = ("CPY #", lambda: self._op_cpy(self._am_imm_x()))
        O[0xCC] = ("CPY abs", lambda: self._op_cpy(self._am_abs()))
        O[0xC4] = ("CPY dp", lambda: self._op_cpy(self._am_dp()))

        O[0x89] = ("BIT #", lambda: self._op_bit(self._am_imm_m()))
        O[0x2C] = ("BIT abs", lambda: self._op_bit(self._am_abs()))
        O[0x24] = ("BIT dp", lambda: self._op_bit(self._am_dp()))
        O[0x3C] = ("BIT abs,X", lambda: self._op_bit(self._am_abs_x()))
        O[0x34] = ("BIT dp,X", lambda: self._op_bit(self._am_dp_x()))

        # INC / DEC (mem)
        O[0xE6] = ("INC dp", lambda: self._op_inc_mem(self._am_dp()))
        O[0xEE] = ("INC abs", lambda: self._op_inc_mem(self._am_abs()))
        O[0xC6] = ("DEC dp", lambda: self._op_dec_mem(self._am_dp()))
        O[0xCE] = ("DEC abs", lambda: self._op_dec_mem(self._am_abs()))

        # INA/DEA/INX/DEX/INY/DEY
        O[0x1A] = ("INA", self._op_ina)
        O[0x3A] = ("DEA", self._op_dea)
        O[0xE8] = ("INX", self._op_inx)
        O[0xCA] = ("DEX", self._op_dex)
        O[0xC8] = ("INY", self._op_iny)
        O[0x88] = ("DEY", self._op_dey)

        # Shifts (accumulator only — memory variants TODO)
        O[0x0A] = ("ASL A", self._op_asl_a)
        O[0x4A] = ("LSR A", self._op_lsr_a)
        O[0x2A] = ("ROL A", self._op_rol_a)
        O[0x6A] = ("ROR A", self._op_ror_a)

        # Transfers
        O[0xAA] = ("TAX", self._op_tax)
        O[0xA8] = ("TAY", self._op_tay)
        O[0x8A] = ("TXA", self._op_txa)
        O[0x98] = ("TYA", self._op_tya)
        O[0xBA] = ("TSX", self._op_tsx)
        O[0x9A] = ("TXS", self._op_txs)
        O[0x9B] = ("TXY", self._op_txy)
        O[0xBB] = ("TYX", self._op_tyx)
        O[0x5B] = ("TCD", self._op_tcd)
        O[0x7B] = ("TDC", self._op_tdc)
        O[0x1B] = ("TCS", self._op_tcs)
        O[0x3B] = ("TSC", self._op_tsc)
        O[0xEB] = ("XBA", self._op_xba)

        # Jumps / calls
        O[0x4C] = ("JMP abs", self._op_jmp_abs)
        O[0x6C] = ("JMP (abs)", self._op_jmp_abs_ind)
        O[0x7C] = ("JMP (abs,X)", self._op_jmp_abs_x_ind)
        O[0x5C] = ("JML long", self._op_jml_long)
        O[0xDC] = ("JML [abs]", self._op_jml_abs_ind_long)
        O[0x20] = ("JSR abs", self._op_jsr_abs)
        O[0xFC] = ("JSR (abs,X)", self._op_jsr_abs_x_ind)
        O[0x22] = ("JSL long", self._op_jsl_long)
        O[0x60] = ("RTS", self._op_rts)
        O[0x6B] = ("RTL", self._op_rtl)
        O[0x40] = ("RTI", self._op_rti)
        O[0x80] = ("BRA", lambda: self._branch(True))
        O[0x82] = ("BRL", self._op_brl)

        # Conditional branches
        O[0x90] = ("BCC", lambda: self._branch(not (self.p & F_C)))
        O[0xB0] = ("BCS", lambda: self._branch(bool(self.p & F_C)))
        O[0xF0] = ("BEQ", lambda: self._branch(bool(self.p & F_Z)))
        O[0xD0] = ("BNE", lambda: self._branch(not (self.p & F_Z)))
        O[0x30] = ("BMI", lambda: self._branch(bool(self.p & F_N)))
        O[0x10] = ("BPL", lambda: self._branch(not (self.p & F_N)))
        O[0x50] = ("BVC", lambda: self._branch(not (self.p & F_V)))
        O[0x70] = ("BVS", lambda: self._branch(bool(self.p & F_V)))

        # Stack
        O[0x48] = ("PHA", lambda: self._pushM(self.a))
        O[0x68] = ("PLA", self._op_pla)
        O[0xDA] = ("PHX", lambda: self._pushX(self.x))
        O[0xFA] = ("PLX", self._op_plx)
        O[0x5A] = ("PHY", lambda: self._pushX(self.y))
        O[0x7A] = ("PLY", self._op_ply)
        O[0x08] = ("PHP", lambda: self._push8(self.p))
        O[0x28] = ("PLP", self._op_plp)
        O[0x8B] = ("PHB", lambda: self._push8(self.db))
        O[0xAB] = ("PLB", self._op_plb)
        O[0x0B] = ("PHD", lambda: self._push16(self.d))
        O[0x2B] = ("PLD", self._op_pld)
        O[0x4B] = ("PHK", lambda: self._push8(self.pb))
        O[0xF4] = ("PEA #", self._op_pea)
        O[0xD4] = ("PEI dp", self._op_pei)
        O[0x62] = ("PER rel", self._op_per)

        # Misc
        O[0xEA] = ("NOP", lambda: None)
        O[0xCB] = ("WAI", self._op_wai)
        O[0xDB] = ("STP", self._op_stp)
        O[0x42] = ("WDM", lambda: self._fetch8())

    # ---- per-op handlers ----

    def _set_flag(self, mask: int, on: bool) -> None:
        if on:
            self.p |= mask
        else:
            self.p &= (~mask) & 0xFF

    def _op_rep(self) -> None:
        m = self._fetch8()
        self.p = self.p & (~m & 0xFF)
        if self.e:
            self.p |= F_M | F_X
        self._fix_xy_for_x8()

    def _op_sep(self) -> None:
        m = self._fetch8()
        self.p = self.p | (m & 0xFF)
        self._fix_xy_for_x8()

    def _fix_xy_for_x8(self) -> None:
        if self._x8():
            self.x &= 0xFF
            self.y &= 0xFF

    def _op_xce(self) -> None:
        # Swap C flag and E flag.
        old_c = bool(self.p & F_C)
        old_e = self.e
        self.e = old_c
        if old_e:
            self.p |= F_C
        else:
            self.p &= (~F_C) & 0xFF
        # Entering emulation mode: force M=X=1, high bytes of XY = 0, S high = $01.
        if self.e:
            self.p |= F_M | F_X
            self.x &= 0xFF
            self.y &= 0xFF
            self.s = 0x0100 | (self.s & 0xFF)

    def _op_ina(self) -> None:
        if self._m8():
            r = (self.a + 1) & 0xFF
            self.a = (self.a & 0xFF00) | r
        else:
            self.a = (self.a + 1) & 0xFFFF
        self._set_nz_a()

    def _op_dea(self) -> None:
        if self._m8():
            r = (self.a - 1) & 0xFF
            self.a = (self.a & 0xFF00) | r
        else:
            self.a = (self.a - 1) & 0xFFFF
        self._set_nz_a()

    def _op_inx(self) -> None:
        if self._x8():
            self.x = (self.x + 1) & 0xFF
        else:
            self.x = (self.x + 1) & 0xFFFF
        self._set_nz_x()

    def _op_dex(self) -> None:
        if self._x8():
            self.x = (self.x - 1) & 0xFF
        else:
            self.x = (self.x - 1) & 0xFFFF
        self._set_nz_x()

    def _op_iny(self) -> None:
        if self._x8():
            self.y = (self.y + 1) & 0xFF
        else:
            self.y = (self.y + 1) & 0xFFFF
        self._set_nz_y()

    def _op_dey(self) -> None:
        if self._x8():
            self.y = (self.y - 1) & 0xFF
        else:
            self.y = (self.y - 1) & 0xFFFF
        self._set_nz_y()

    def _op_tax(self) -> None:
        if self._x8():
            self.x = self.a & 0xFF
        else:
            self.x = self.a & 0xFFFF
        self._set_nz_x()

    def _op_tay(self) -> None:
        if self._x8():
            self.y = self.a & 0xFF
        else:
            self.y = self.a & 0xFFFF
        self._set_nz_y()

    def _op_txa(self) -> None:
        if self._m8():
            self.a = (self.a & 0xFF00) | (self.x & 0xFF)
        else:
            self.a = self.x & 0xFFFF
        self._set_nz_a()

    def _op_tya(self) -> None:
        if self._m8():
            self.a = (self.a & 0xFF00) | (self.y & 0xFF)
        else:
            self.a = self.y & 0xFFFF
        self._set_nz_a()

    def _op_tsx(self) -> None:
        if self._x8():
            self.x = self.s & 0xFF
        else:
            self.x = self.s & 0xFFFF
        self._set_nz_x()

    def _op_txs(self) -> None:
        if self.e:
            self.s = 0x0100 | (self.x & 0xFF)
        else:
            self.s = self.x & 0xFFFF

    def _op_txy(self) -> None:
        if self._x8():
            self.y = self.x & 0xFF
        else:
            self.y = self.x & 0xFFFF
        self._set_nz_y()

    def _op_tyx(self) -> None:
        if self._x8():
            self.x = self.y & 0xFF
        else:
            self.x = self.y & 0xFFFF
        self._set_nz_x()

    def _op_tcd(self) -> None:
        self.d = self.a & 0xFFFF
        self._set_nz(self.d, 16)

    def _op_tdc(self) -> None:
        self.a = self.d & 0xFFFF
        self._set_nz(self.a, 16)

    def _op_tcs(self) -> None:
        if self.e:
            self.s = 0x0100 | (self.a & 0xFF)
        else:
            self.s = self.a & 0xFFFF

    def _op_tsc(self) -> None:
        self.a = self.s & 0xFFFF
        self._set_nz(self.a, 16)

    def _op_xba(self) -> None:
        lo = self.a & 0xFF
        hi = (self.a >> 8) & 0xFF
        self.a = (lo << 8) | hi
        # NZ on new low byte
        v = self.a & 0xFF
        self.p &= ~F_N & ~F_Z & 0xFF
        if v & 0x80:
            self.p |= F_N
        if v == 0:
            self.p |= F_Z

    def _op_jmp_abs(self) -> None:
        self.pc = self._fetch16()

    def _op_jmp_abs_ind(self) -> None:
        ptr = self._fetch16()
        lo = self.bus.read8(ptr)
        hi = self.bus.read8((ptr + 1) & 0xFFFF)
        self.pc = (hi << 8) | lo

    def _op_jmp_abs_x_ind(self) -> None:
        base = self._fetch16()
        ptr = ((self.pb << 16) | ((base + self.x) & 0xFFFF))
        lo = self.bus.read8(ptr)
        hi = self.bus.read8((ptr + 1) & 0xFFFFFF)
        self.pc = (hi << 8) | lo

    def _op_jml_long(self) -> None:
        addr = self._fetch24()
        self.pc = addr & 0xFFFF
        self.pb = (addr >> 16) & 0xFF

    def _op_jml_abs_ind_long(self) -> None:
        ptr = self._fetch16()
        lo = self.bus.read8(ptr)
        mid = self.bus.read8((ptr + 1) & 0xFFFF)
        hi = self.bus.read8((ptr + 2) & 0xFFFF)
        self.pc = (mid << 8) | lo
        self.pb = hi

    def _op_jsr_abs(self) -> None:
        target = self._fetch16()
        ret = (self.pc - 1) & 0xFFFF
        self._push16(ret)
        self.pc = target

    def _op_jsr_abs_x_ind(self) -> None:
        base = self._fetch16()
        ret = (self.pc - 1) & 0xFFFF
        self._push16(ret)
        ptr = (self.pb << 16) | ((base + self.x) & 0xFFFF)
        lo = self.bus.read8(ptr)
        hi = self.bus.read8((ptr + 1) & 0xFFFFFF)
        self.pc = (hi << 8) | lo

    def _op_jsl_long(self) -> None:
        target = self._fetch24()
        ret_pc = (self.pc - 1) & 0xFFFF
        self._push8(self.pb)
        self._push16(ret_pc)
        self.pc = target & 0xFFFF
        self.pb = (target >> 16) & 0xFF

    def _op_rts(self) -> None:
        ret = self._pop16()
        self.pc = (ret + 1) & 0xFFFF

    def _op_rtl(self) -> None:
        ret = self._pop16()
        self.pb = self._pop8()
        self.pc = (ret + 1) & 0xFFFF

    def _op_rti(self) -> None:
        self.p = self._pop8()
        ret = self._pop16()
        self.pc = ret
        if not self.e:
            self.pb = self._pop8()
        self._fix_xy_for_x8()

    def _op_brl(self) -> None:
        off = self._fetch16()
        if off & 0x8000:
            off -= 0x10000
        self.pc = (self.pc + off) & 0xFFFF

    def _op_pla(self) -> None:
        v = self._popM()
        if self._m8():
            self.a = (self.a & 0xFF00) | (v & 0xFF)
        else:
            self.a = v & 0xFFFF
        self._set_nz_a()

    def _op_plx(self) -> None:
        v = self._popX()
        self.x = (v & 0xFF) if self._x8() else (v & 0xFFFF)
        self._set_nz_x()

    def _op_ply(self) -> None:
        v = self._popX()
        self.y = (v & 0xFF) if self._x8() else (v & 0xFFFF)
        self._set_nz_y()

    def _op_plp(self) -> None:
        self.p = self._pop8()
        self._fix_xy_for_x8()

    def _op_plb(self) -> None:
        self.db = self._pop8()
        self._set_nz(self.db, 8)

    def _op_pld(self) -> None:
        self.d = self._pop16()
        self._set_nz(self.d, 16)

    def _op_pea(self) -> None:
        v = self._fetch16()
        self._push16(v)

    def _op_pei(self) -> None:
        ptr = (self.d + self._fetch8()) & 0xFFFF
        lo = self.bus.read8(ptr)
        hi = self.bus.read8((ptr + 1) & 0xFFFF)
        self._push16((hi << 8) | lo)

    def _op_per(self) -> None:
        off = self._fetch16()
        if off & 0x8000:
            off -= 0x10000
        self._push16((self.pc + off) & 0xFFFF)

    def _op_wai(self) -> None:
        # No interrupts modeled — just halt with a clear reason.
        self.halted = True
        self.halt_reason = "WAI (no interrupts modeled — no PPU/APU)"
        self._t("[wai] halt: no interrupts modeled")

    def _op_stp(self) -> None:
        self.halted = True
        self.halt_reason = "STP"
        self._t("[stp] processor stopped")


# =====================================================================
# Cart header parsing (sidebar info)
# =====================================================================

_REGION = {
    0x00: "Japan (NTSC)", 0x01: "USA (NTSC)", 0x02: "Europe (PAL)",
    0x03: "Sweden/Scand (PAL)", 0x04: "Finland (PAL)", 0x05: "Denmark (PAL)",
    0x06: "France (PAL)", 0x07: "Netherlands (PAL)", 0x08: "Spain (PAL)",
    0x09: "Germany (PAL)", 0x0A: "Italy (PAL)", 0x0B: "China",
    0x0C: "Indonesia", 0x0D: "Korea", 0x0E: "Common", 0x0F: "Canada",
    0x10: "Brazil", 0x11: "Australia (PAL)",
}
_MAP_LAYOUT = {0x0: "LoROM", 0x1: "HiROM", 0x2: "ExLoROM", 0x3: "SA-1 LoROM", 0x5: "ExHiROM"}
_COPROC = {0x0: "DSP", 0x1: "SuperFX", 0x2: "OBC1", 0x3: "SA-1",
           0x4: "S-DD1", 0x5: "S-RTC", 0xE: "Other", 0xF: "Custom"}
_CUSTOM_CHIP = {0x00: "SPC7110", 0x01: "ST010/011", 0x02: "ST018", 0x10: "Cx4"}
_HW = {0x0: "ROM", 0x1: "ROM+RAM", 0x2: "ROM+RAM+batt", 0x3: "ROM+coproc",
       0x4: "ROM+coproc+RAM", 0x5: "ROM+coproc+RAM+batt", 0x6: "ROM+coproc+batt"}


def _strip_copier_header(data: bytes) -> bytes:
    if len(data) >= 512 and len(data) % 1024 == 512:
        return data[512:]
    return data


def _score_hdr(rom: bytes, base: int) -> int:
    if base + 0x20 > len(rom):
        return -10_000
    s = 0
    for b in rom[base : base + 21]:
        if 32 <= b < 127:
            s += 2
        elif b in (0, 0x20):
            s += 1
        else:
            s -= 2
    mm = rom[base + 0x15]
    expected = {0x7FC0: (0x0, 0x2, 0x3), 0xFFC0: (0x1,), 0x40FFC0: (0x5,)}
    if base in expected and (mm & 0x0F) in expected[base]:
        s += 20
    if (mm & 0xE0) in (0x20, 0x30):
        s += 4
    else:
        s -= 4
    csum = rom[base + 0x1E] | (rom[base + 0x1F] << 8)
    comp = rom[base + 0x1C] | (rom[base + 0x1D] << 8)
    if (csum ^ comp) == 0xFFFF and 0 < csum < 0xFFFF:
        s += 30
    return s


def identify(rom: bytes) -> dict:
    rom_clean = _strip_copier_header(rom)
    if len(rom_clean) < 0x8000:
        return {}
    candidates = []
    if len(rom_clean) >= 0x8000 + 0x20:
        candidates.append(0x7FC0)
    if len(rom_clean) >= 0x10000 + 0x20:
        candidates.append(0xFFC0)
    if len(rom_clean) >= 0x410000 + 0x20:
        candidates.append(0x40FFC0)
    best, chosen = -10_000, -1
    for off in candidates:
        sc = _score_hdr(rom_clean, off)
        if sc > best:
            best, chosen = sc, off
    if chosen < 0 or best < 10:
        return {}
    base = chosen
    mm = rom_clean[base + 0x15]
    layout = _MAP_LAYOUT.get(mm & 0x0F, f"?(${mm & 0x0F:X})")
    if base == 0x40FFC0:
        layout = "ExHiROM"
    elif (mm & 0x0F) == 0x3:
        layout = "SA-1 LoROM"
    hw = rom_clean[base + 0x16]
    hw_low, hw_high = hw & 0x0F, (hw >> 4) & 0x0F
    coproc = _COPROC.get(hw_high, f"?(${hw_high:X})") if hw_low >= 3 else "None"
    dev_id = rom_clean[base + 0x1A]
    if dev_id == 0x33 and base >= 0x10 and hw_low >= 3 and hw_high == 0xF:
        coproc = _CUSTOM_CHIP.get(rom_clean[base - 0x10 + 0x0F], "Custom")
    csum = rom_clean[base + 0x1E] | (rom_clean[base + 0x1F] << 8)
    comp = rom_clean[base + 0x1C] | (rom_clean[base + 0x1D] << 8)
    rs = rom_clean[base + 0x17]
    ss = rom_clean[base + 0x18]
    reset_vec = rom_clean[base + 0x3C] | (rom_clean[base + 0x3D] << 8)
    return {
        "title": rom_clean[base : base + 21].decode("latin-1", "replace").strip("\x00 ").strip() or "(blank)",
        "layout": layout,
        "fast_rom": bool(mm & 0x10),
        "hw": _HW.get(hw_low, f"hw(${hw_low:X})"),
        "coproc": coproc,
        "rom_kb": (1 << rs) if 0 <= rs <= 16 else 0,
        "sram_kb": (1 << ss) if 0 < ss <= 16 else 0,
        "region": _REGION.get(rom_clean[base + 0x19], f"?(${rom_clean[base + 0x19]:02X})"),
        "version": rom_clean[base + 0x1B],
        "checksum": csum,
        "complement": comp,
        "checksum_ok": (csum ^ comp) == 0xFFFF and 0 < csum < 0xFFFF,
        "reset_vec": reset_vec,
        "rom_bytes": rom_clean,
    }


# =====================================================================
# UI helpers
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
COLOR_OK = (130, 220, 150)
COLOR_WARN = (240, 190, 120)
COLOR_BAD = (230, 110, 110)


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
    tag = font_tag.render(
        f"{MEWSNES_CORE} (libretro + baked-CPU) · FILES=OFF · ultrathink",
        True, COLOR_TEXT_DIM,
    )
    surf.blit(tag, (rect.x + 12, y + 4))


def draw_round_rect(surf, rect, radius, fill, border=None, bw=1):
    pygame.draw.rect(surf, fill, rect, border_radius=radius)
    if border is not None:
        pygame.draw.rect(surf, border, rect, bw, border_radius=radius)


def draw_button(surf, font, label, rect, mouse_pos, enabled=True, highlight=False):
    hover = enabled and rect.collidepoint(mouse_pos)
    if highlight:
        bg = (48, 48, 60)
    else:
        bg = COLOR_BTN_HOVER if hover else COLOR_PANEL
    draw_round_rect(surf, rect, 6, bg, COLOR_EDGE_HI if (hover or highlight) else COLOR_EDGE, 1)
    tcol = COLOR_TEXT if (hover or highlight) else (COLOR_TEXT_DIM if enabled else (70, 70, 78))
    t = font.render(label, True, tcol)
    surf.blit(t, (rect.centerx - t.get_width() // 2, rect.centery - t.get_height() // 2))
    return hover


KEYMAP = {
    pygame.K_UP:        RETRO_DEVICE_ID_JOYPAD_UP,
    pygame.K_DOWN:      RETRO_DEVICE_ID_JOYPAD_DOWN,
    pygame.K_LEFT:      RETRO_DEVICE_ID_JOYPAD_LEFT,
    pygame.K_RIGHT:     RETRO_DEVICE_ID_JOYPAD_RIGHT,
    pygame.K_z:         RETRO_DEVICE_ID_JOYPAD_B,
    pygame.K_x:         RETRO_DEVICE_ID_JOYPAD_A,
    pygame.K_a:         RETRO_DEVICE_ID_JOYPAD_Y,
    pygame.K_s:         RETRO_DEVICE_ID_JOYPAD_X,
    pygame.K_q:         RETRO_DEVICE_ID_JOYPAD_L,
    pygame.K_w:         RETRO_DEVICE_ID_JOYPAD_R,
    pygame.K_RETURN:    RETRO_DEVICE_ID_JOYPAD_START,
    pygame.K_BACKSPACE: RETRO_DEVICE_ID_JOYPAD_SELECT,
}


# =====================================================================
# main
# =====================================================================

MODE_LIBRETRO = "libretro"
MODE_BAKED = "baked"


def main():
    pygame.mixer.pre_init(frequency=32040, size=-16, channels=2, buffer=512)
    pygame.init()
    try:
        pygame.mixer.init()
        mixer_ok = True
    except pygame.error:
        mixer_ok = False
    audio_channel = pygame.mixer.Channel(0) if mixer_ok else None

    pygame.display.set_caption(f"AC's SNES emu — {MEWSNES_CORE} (libretro + baked-CPU) · FILES=OFF")

    w, h = 1080, 640
    screen = pygame.display.set_mode((w, h))
    clock = pygame.time.Clock()

    try:
        font_title = pygame.font.SysFont("consolas", 18, bold=True)
        font_logo = pygame.font.SysFont("segoeui", 30, bold=True)
        font_tag = pygame.font.SysFont("segoeui", 13, italic=True)
        font_body = pygame.font.SysFont("consolas", 13)
        font_small = pygame.font.SysFont("consolas", 11)
        font_mono = pygame.font.SysFont("consolas", 12)
    except Exception:
        font_title = pygame.font.Font(None, 20)
        font_logo = pygame.font.Font(None, 32)
        font_tag = pygame.font.Font(None, 14)
        font_body = pygame.font.Font(None, 16)
        font_small = pygame.font.Font(None, 14)
        font_mono = pygame.font.Font(None, 14)

    host = MewSNESLibretro()
    bus: Optional[SNESBus] = None
    cpu: Optional[CPU65816] = None
    cart_info: dict = {}

    root = None
    if _HAS_TK:
        root = tk.Tk()
        root.withdraw()

    margin = 14
    header_h = 40
    bar_y = h - 48

    viewport = pygame.Rect(margin, header_h + margin, 560, 472)
    side = pygame.Rect(viewport.right + margin, viewport.y, w - viewport.right - 2 * margin, viewport.height)

    btn_mode = pygame.Rect(margin,       bar_y, 130, 36)
    btn_core = pygame.Rect(margin + 140, bar_y, 130, 36)
    btn_load = pygame.Rect(margin + 280, bar_y, 130, 36)
    btn_run  = pygame.Rect(margin + 420, bar_y, 100, 36)
    btn_rst  = pygame.Rect(margin + 530, bar_y, 80,  36)
    btn_step = pygame.Rect(margin + 620, bar_y, 80,  36)
    btn_aud  = pygame.Rect(margin + 710, bar_y, 100, 36)
    btn_unl  = pygame.Rect(margin + 820, bar_y, 80,  36)

    toast = ""
    toast_ticks = 0

    def show_t(msg, t=120):
        nonlocal toast, toast_ticks
        toast, toast_ticks = msg, t

    mode = MODE_LIBRETRO
    paused = False
    audio_muted = False

    def baked_ready() -> bool:
        return cpu is not None and cpu.bus is bus and len(bus.rom) > 0 if bus else False

    def pick_core():
        if not _HAS_TK:
            show_t("tkinter missing", 160)
            return
        path = filedialog.askopenfilename(
            parent=root,
            title="pick libretro core (.dll/.so/.dylib)",
            filetypes=[("libretro core", "*.dll *.so *.dylib"), ("All", "*.*")],
        )
        if not path:
            return
        if host.loaded:
            host.unload()
        err = host.load_core(path)
        if err:
            show_t(f"core: {err}", 220)
        else:
            cap = f"AC's SNES emu — {MEWSNES_CORE} · {host.library_name} {host.library_version}"
            pygame.display.set_caption(cap)
            show_t(f"core: {host.library_name} {host.library_version}", 160)

    def pick_rom():
        nonlocal bus, cpu, cart_info, paused
        if mode == MODE_LIBRETRO and not host.loaded:
            show_t("load a libretro core first", 180)
            return
        if not _HAS_TK:
            show_t("tkinter missing", 160)
            return
        path = filedialog.askopenfilename(
            parent=root,
            title="pick SNES ROM",
            filetypes=[("SNES ROM", "*.sfc *.smc *.SFC *.SMC *.fig *.swc"), ("All", "*.*")],
        )
        if not path:
            return
        try:
            data = Path(path).read_bytes()
        except OSError as e:
            show_t(f"read error: {e}", 180)
            return

        info = identify(data)
        cart_info = info

        if mode == MODE_LIBRETRO:
            err = host.load_rom(data)
            if err:
                show_t(f"libretro: {err}", 220)
                return
            title = info.get("title", Path(path).stem)
            pygame.display.set_caption(f"AC's SNES emu — {MEWSNES_CORE} · {title[:40]}")
            show_t(f"loaded: {title}", 140)
        else:
            # baked-CPU path
            if not info:
                show_t("no SNES header — cannot boot baked CPU", 220)
                return
            rom_bytes = info["rom_bytes"]
            bus = SNESBus(rom_bytes, info["layout"])
            cpu = CPU65816(bus)
            cpu.reset(info["reset_vec"])
            title = info["title"]
            pygame.display.set_caption(f"AC's SNES emu — baked CPU · {title[:40]}")
            show_t(f"baked CPU @ ${info['reset_vec']:04X}", 160)

        paused = False

    running = True
    try:
        while running:
            mouse = pygame.mouse.get_pos()
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                    if btn_mode.collidepoint(event.pos):
                        mode = MODE_BAKED if mode == MODE_LIBRETRO else MODE_LIBRETRO
                        show_t(f"mode: {mode}", 100)
                    elif btn_core.collidepoint(event.pos):
                        pick_core()
                    elif btn_load.collidepoint(event.pos):
                        pick_rom()
                    elif btn_run.collidepoint(event.pos):
                        if mode == MODE_LIBRETRO and host.rom_loaded:
                            paused = not paused
                            show_t("paused" if paused else "running", 60)
                        elif mode == MODE_BAKED and cpu is not None:
                            paused = not paused
                            show_t("paused" if paused else "running", 60)
                        else:
                            show_t("no ROM loaded", 80)
                    elif btn_rst.collidepoint(event.pos):
                        if mode == MODE_LIBRETRO and host.rom_loaded:
                            host.reset()
                            show_t("reset", 60)
                        elif mode == MODE_BAKED and cpu is not None and cart_info:
                            cpu.reset(cart_info["reset_vec"])
                            show_t("baked CPU reset", 60)
                    elif btn_step.collidepoint(event.pos):
                        if mode == MODE_BAKED and cpu is not None:
                            cpu.step()
                            show_t(f"step ({cpu.instr_count})", 60)
                    elif btn_aud.collidepoint(event.pos):
                        audio_muted = not audio_muted
                        show_t("audio muted" if audio_muted else "audio on", 60)
                    elif btn_unl.collidepoint(event.pos):
                        if mode == MODE_LIBRETRO and host.loaded:
                            host.unload()
                        elif mode == MODE_BAKED:
                            bus = None
                            cpu = None
                        cart_info = {}
                        show_t("unloaded", 60)
                elif event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_F1:
                        if mode == MODE_LIBRETRO and host.rom_loaded:
                            host.reset()
                            show_t("reset", 60)
                        elif mode == MODE_BAKED and cpu is not None and cart_info:
                            cpu.reset(cart_info["reset_vec"])
                            show_t("baked CPU reset", 60)
                    elif event.key == pygame.K_F2:
                        audio_muted = not audio_muted
                        show_t("audio muted" if audio_muted else "audio on", 60)
                    elif event.key in KEYMAP:
                        host.set_button(0, KEYMAP[event.key], True)
                elif event.type == pygame.KEYUP:
                    if event.key in KEYMAP:
                        host.set_button(0, KEYMAP[event.key], False)

            # ---- tick the active engine ----
            if not paused:
                if mode == MODE_LIBRETRO and host.rom_loaded:
                    try:
                        host.run_frame()
                    except Exception as e:  # noqa: BLE001
                        show_t(f"core crashed: {e}", 220)
                        traceback.print_exc()
                        host.unload()
                        cart_info = {}
                    if mixer_ok and audio_channel is not None and not audio_muted and host.audio_buffer:
                        try:
                            snd = pygame.mixer.Sound(buffer=bytes(host.audio_buffer))
                            if not audio_channel.get_busy():
                                audio_channel.play(snd)
                            elif audio_channel.get_queue() is None:
                                audio_channel.queue(snd)
                        except Exception:
                            pass
                elif mode == MODE_BAKED and cpu is not None and not cpu.halted:
                    # Run a budget of instructions per UI frame — keeps trace useful
                    cpu.step_many(2000)

            # ---- draw ----
            screen.fill(COLOR_VOID)

            hdr = pygame.Rect(0, 0, w, header_h)
            draw_round_rect(screen, hdr, 0, COLOR_PANEL, COLOR_EDGE, 1)
            mode_label = "libretro (.dll)" if mode == MODE_LIBRETRO else "baked CPU (65C816)"
            screen.blit(
                font_title.render(
                    f"AC's SNES emu  ·  {MEWSNES_CORE}  ·  mode: {mode_label}  ·  FILES=OFF",
                    True, COLOR_ACCENT,
                ),
                (margin, 10),
            )

            # --- viewport ---
            draw_round_rect(screen, viewport, 10, COLOR_PANEL, COLOR_EDGE, 1)
            inner = viewport.inflate(-20, -20)
            draw_round_rect(screen, inner, 8, COLOR_PANEL_INNER, None)

            if mode == MODE_LIBRETRO:
                if host.frame_rgb888 and host.frame_w > 0 and host.frame_h > 0:
                    try:
                        frame_surf = pygame.image.frombuffer(
                            host.frame_rgb888, (host.frame_w, host.frame_h), "RGB"
                        )
                        scaled = pygame.transform.scale(frame_surf, (inner.w, inner.h))
                        screen.blit(scaled, inner.topleft)
                    except Exception as e:  # noqa: BLE001
                        screen.blit(font_body.render(f"blit err: {e}", True, COLOR_BAD), (inner.x + 8, inner.y + 8))
                else:
                    _draw_idle(screen, inner, font_logo, font_tag, font_small, mode)
            else:
                # baked CPU viewport: registers + recent instructions
                _draw_baked_view(screen, inner, font_title, font_mono, font_small, cpu, bus)

            # --- side panel ---
            draw_round_rect(screen, side, 10, COLOR_PANEL, COLOR_EDGE, 1)
            sy = side.y + 10
            screen.blit(font_title.render("Status", True, COLOR_ACCENT), (side.x + 10, sy))
            sy += 24

            if mode == MODE_LIBRETRO:
                core_row = f"{host.library_name} {host.library_version}" if host.loaded else "(no core)"
                rom_row = (
                    f"yes ({host.base_width}x{host.base_height} @ {host.fps:.2f} fps)"
                    if host.rom_loaded else "(none)"
                )
                state_row = "paused" if paused else ("running" if host.rom_loaded else "idle")
                state_ok = host.rom_loaded and not paused
            else:
                core_row = "baked 65C816 + bus"
                rom_row = f"yes ({cart_info['layout']})" if cart_info else "(none)"
                if cpu is None:
                    state_row = "idle"
                    state_ok = False
                elif cpu.halted:
                    state_row = "halted"
                    state_ok = False
                elif paused:
                    state_row = "paused"
                    state_ok = False
                else:
                    state_row = "running"
                    state_ok = True

            audio_row = "muted" if audio_muted else (f"{int(host.sample_rate)} Hz" if mixer_ok else "(mixer down)")

            for label, val, ok in [
                ("Engine", core_row, True),
                ("ROM",    rom_row, bool(cart_info)),
                ("Audio",  audio_row, mixer_ok and not audio_muted),
                ("State",  state_row, state_ok),
            ]:
                screen.blit(font_body.render(f"{label:7s}", True, COLOR_TEXT_DIM), (side.x + 10, sy))
                col = COLOR_OK if ok else COLOR_TEXT_DIM
                screen.blit(font_body.render(str(val)[:42], True, col), (side.x + 10 + 64, sy))
                sy += 17

            sy += 6
            screen.blit(font_title.render("Cart", True, COLOR_ACCENT), (side.x + 10, sy))
            sy += 22
            if cart_info:
                rows = [
                    ("Title", cart_info["title"]),
                    ("Layout", f"{cart_info['layout']}  ({'FastROM' if cart_info['fast_rom'] else 'SlowROM'})"),
                    ("HW", cart_info["hw"]),
                    ("Coproc.", cart_info["coproc"]),
                    ("ROM", f"{cart_info['rom_kb']} KB"),
                    ("SRAM", f"{cart_info['sram_kb']} KB"),
                    ("Region", cart_info["region"]),
                    ("Reset", f"${cart_info['reset_vec']:04X}"),
                    ("Cksum", f"${cart_info['checksum']:04X}^${cart_info['complement']:04X} "
                              + ("OK" if cart_info["checksum_ok"] else "BAD")),
                ]
                for label, val in rows:
                    screen.blit(font_body.render(f"{label:8s}", True, COLOR_TEXT_DIM), (side.x + 10, sy))
                    if label == "Cksum":
                        col = COLOR_OK if cart_info["checksum_ok"] else COLOR_WARN
                    else:
                        col = COLOR_TEXT
                    screen.blit(font_body.render(str(val)[:34], True, col), (side.x + 10 + 70, sy))
                    sy += 17
            else:
                screen.blit(font_body.render("(no cart)", True, COLOR_TEXT_DIM), (side.x + 10, sy))
                sy += 17

            sy += 4
            log_title = "Log" if mode == MODE_LIBRETRO else "Bus regs (last writes)"
            screen.blit(font_title.render(log_title, True, COLOR_ACCENT), (side.x + 10, sy))
            sy += 20
            if mode == MODE_LIBRETRO:
                for row in host.log[-12:]:
                    screen.blit(font_small.render(row[:46], True, COLOR_TEXT_DIM), (side.x + 10, sy))
                    sy += 14
            elif bus is not None:
                last = bus.reg_writes[-12:]
                if not last:
                    screen.blit(font_small.render("(no writes yet)", True, COLOR_TEXT_DIM), (side.x + 10, sy))
                    sy += 14
                else:
                    for a24, v in last:
                        line = f"${a24:06X} <- ${v:02X}"
                        screen.blit(font_small.render(line, True, COLOR_TEXT_DIM), (side.x + 10, sy))
                        sy += 14

            # buttons
            draw_round_rect(screen, pygame.Rect(0, bar_y - 8, w, 56), 0, COLOR_VOID, None)
            draw_button(screen, font_body, f"Mode: {'libretro' if mode == MODE_LIBRETRO else 'baked'}", btn_mode, mouse)
            draw_button(screen, font_body, "Load Core…", btn_core, mouse,
                        enabled=(mode == MODE_LIBRETRO))
            draw_button(screen, font_body, "Load ROM…", btn_load, mouse,
                        enabled=(mode == MODE_BAKED or host.loaded))
            run_label = "Pause" if (not paused and ((mode == MODE_LIBRETRO and host.rom_loaded) or (mode == MODE_BAKED and cpu is not None and not cpu.halted))) else "Run"
            draw_button(screen, font_body, run_label, btn_run, mouse,
                        enabled=((mode == MODE_LIBRETRO and host.rom_loaded) or (mode == MODE_BAKED and cpu is not None and not cpu.halted)))
            draw_button(screen, font_body, "Reset", btn_rst, mouse,
                        enabled=((mode == MODE_LIBRETRO and host.rom_loaded) or (mode == MODE_BAKED and cpu is not None)))
            draw_button(screen, font_body, "Step", btn_step, mouse,
                        enabled=(mode == MODE_BAKED and cpu is not None and not cpu.halted))
            draw_button(screen, font_body, "Audio: OFF" if audio_muted else "Audio: ON",
                        btn_aud, mouse, enabled=mixer_ok)
            draw_button(screen, font_body, "Unload", btn_unl, mouse,
                        enabled=((mode == MODE_LIBRETRO and host.loaded) or (mode == MODE_BAKED and cpu is not None)))

            if toast_ticks > 0:
                toast_ticks -= 1
                surf = font_small.render(toast, True, (180, 220, 255))
                pygame.draw.rect(
                    screen, (28, 28, 36),
                    (8, h - surf.get_height() - 14, surf.get_width() + 12, surf.get_height() + 8),
                )
                screen.blit(surf, (14, h - surf.get_height() - 10))

            pygame.display.flip()
            clock.tick(60)
    finally:
        try:
            host.unload()
        except Exception:
            pass
        pygame.quit()
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass


def _draw_idle(screen, inner, font_logo, font_tag, font_small, mode: str) -> None:
    logo_rect = pygame.Rect(inner.x, inner.y + 24, inner.width, 140)
    draw_hue_logo(screen, font_logo, font_tag, logo_rect, pygame.time.get_ticks())
    tips_libretro = [
        "",
        f"FILES_OFF: {FILES_OFF}  #nobake  · ultrathink",
        f"Cython speed-up: {'ON' if _HAS_FAST else 'OFF (numpy)'}",
        "",
        "1. Load Core…  (snes9x_libretro.dll etc.)",
        "2. Load ROM…   (.sfc / .smc / .fig / .swc)",
        "3. Play.  arrows = D-Pad,  Z/X = B/A",
        "          A/S = Y/X,  Q/W = L/R",
        "          Enter = Start,  Backspace = Select",
        "",
        "F1 reset · F2 audio toggle",
    ]
    tips_baked = [
        "",
        "Baked CPU (65C816) — executes ROM code,",
        "does not render graphics (no PPU).",
        "",
        "1. Load ROM…   (.sfc / .smc / .fig / .swc)",
        "2. Run         (continuous step at 2000/tick)",
        "   or Step     (single instruction)",
        "",
        "Halts cleanly on an unimplemented opcode —",
        "the byte and address are logged so it can",
        "be added next.",
    ]
    tips = tips_libretro if mode == MODE_LIBRETRO else tips_baked
    ty = logo_rect.bottom + 6
    for ln in tips:
        screen.blit(font_small.render(ln, True, COLOR_TEXT_DIM), (inner.x + 10, ty))
        ty += 15


def _draw_baked_view(screen, inner, font_title, font_mono, font_small, cpu, bus) -> None:
    pad = 8
    x = inner.x + pad
    y = inner.y + pad
    screen.blit(font_title.render("Baked 65C816 — register state", True, COLOR_ACCENT), (x, y))
    y += 24
    if cpu is None:
        screen.blit(font_small.render("Load a ROM in 'baked' mode to boot the CPU.", True, COLOR_TEXT_DIM), (x, y))
        return

    # Register block
    flags = "".join(
        (c.upper() if (cpu.p & b) else c)
        for c, b in zip("nvmxdizc", (F_N, F_V, F_M, F_X, F_D, F_I, F_Z, F_C))
    )
    rows = [
        f"A : ${cpu.a:04X}   X : ${cpu.x:04X}   Y : ${cpu.y:04X}",
        f"S : ${cpu.s:04X}   D : ${cpu.d:04X}   DB: ${cpu.db:02X}",
        f"PB:PC = ${cpu.pb:02X}:${cpu.pc:04X}    E={int(cpu.e)}",
        f"P : ${cpu.p:02X}  [{flags}]",
        f"instr: {cpu.instr_count}    halted: {cpu.halted}",
    ]
    if cpu.halt_reason:
        rows.append(f"halt: {cpu.halt_reason[:48]}")
    for line in rows:
        screen.blit(font_mono.render(line, True, COLOR_TEXT), (x, y))
        y += 18

    y += 6
    screen.blit(font_title.render("Recent trace", True, COLOR_ACCENT), (x, y))
    y += 22
    for line in cpu.trace[-18:]:
        col = COLOR_BAD if (" UNK " in line or " ERR " in line or "halt" in line) else COLOR_TEXT_DIM
        screen.blit(font_small.render(line[:64], True, col), (x, y))
        y += 14

    if cpu.unimpl_hits:
        y += 4
        items = sorted(cpu.unimpl_hits.items(), key=lambda kv: -kv[1])[:6]
        line = "unimpl: " + " ".join(f"${op:02X}({c})" for op, c in items)
        screen.blit(font_small.render(line[:64], True, COLOR_WARN), (x, y))


if __name__ == "__main__":
    main()
