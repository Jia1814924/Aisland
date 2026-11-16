"""Export the native SVG mark as PNG and a multi-resolution Windows ICO.

Run with the project's Python environment; uses Qt already required by Aisland.
No network requests, third-party icon downloads, or application startup.
"""
from __future__ import annotations

import os
import struct
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtCore, QtGui, QtSvg

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
SIZES = (16, 24, 32, 48, 64, 128, 256)


def render(renderer: QtSvg.QSvgRenderer, size: int) -> QtGui.QImage:
    image = QtGui.QImage(size, size, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(image)
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
    renderer.render(painter)
    painter.end()
    return image


def png_bytes(image: QtGui.QImage) -> bytes:
    buffer = QtCore.QBuffer()
    buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, "PNG"):
        raise RuntimeError("Qt failed to encode PNG")
    return bytes(buffer.data())


def main() -> None:
    app = QtGui.QGuiApplication.instance() or QtGui.QGuiApplication([])
    renderer = QtSvg.QSvgRenderer(str(ASSETS / "aisland.svg"))
    if not renderer.isValid():
        raise RuntimeError("Invalid SVG")
    ASSETS.mkdir(exist_ok=True)
    (ASSETS / "aisland.png").write_bytes(png_bytes(render(renderer, 512)))

    frames = [png_bytes(render(renderer, size)) for size in SIZES]
    offset = 6 + 16 * len(SIZES)
    entries = []
    for size, frame in zip(SIZES, frames):
        entries.append(struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(frame), offset))
        offset += len(frame)
    ico = struct.pack("<HHH", 0, 1, len(SIZES)) + b"".join(entries) + b"".join(frames)
    (ASSETS / "aisland.ico").write_bytes(ico)

    # A preview sheet is a build artifact; only the three assets above are shipped.
    preview = QtGui.QImage(1080, 400, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
    preview.fill(QtGui.QColor("#f0f3f7"))
    painter = QtGui.QPainter(preview)
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform)
    painter.drawImage(32, 30, render(renderer, 320))
    painter.setPen(QtCore.Qt.PenStyle.NoPen)
    painter.setBrush(QtGui.QColor("#ffffff"))
    painter.drawRoundedRect(QtCore.QRectF(376, 32, 668, 148), 24, 24)
    painter.setBrush(QtGui.QColor("#202938"))
    painter.drawRoundedRect(QtCore.QRectF(376, 216, 668, 148), 24, 24)
    for center_y in (106, 290):
        x = 406
        for size in (16, 24, 32, 48, 64, 128):
            painter.drawImage(x, center_y - size // 2, render(renderer, size))
            x += max(size, 24) + 35
    painter.end()
    preview_dir = ROOT / "build" / "icon_preview"
    preview_dir.mkdir(parents=True, exist_ok=True)
    preview.save(str(preview_dir / "aisland-icon-preview.png"))

    parsed = QtGui.QIcon(str(ASSETS / "aisland.ico"))
    found = sorted(s.width() for s in parsed.availableSizes())
    if found != list(SIZES):
        raise RuntimeError(f"ICO validation failed: expected {SIZES}, got {found}")
    print(f"Created PNG, SVG and ICO; verified sizes: {found}")
    print(f"Preview: {preview_dir / 'aisland-icon-preview.png'}")
    del app


if __name__ == "__main__":
    main()
