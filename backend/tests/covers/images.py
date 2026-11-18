"""Tiny image headers for cover tests: enough for magic bytes and sizes."""

from __future__ import annotations

import struct
import zlib


def png(width: int, height: int, salt: bytes = b"") -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    chunk = b"IHDR" + ihdr
    body = struct.pack(">I", len(ihdr)) + chunk + struct.pack(">I", zlib.crc32(chunk))
    return b"\x89PNG\r\n\x1a\n" + body + b"\x00\x00\x00\x00IEND" + salt


def jpeg(width: int, height: int, salt: bytes = b"") -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + app0 + sof + b"\xff\xd9" + salt
