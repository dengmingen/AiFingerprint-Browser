"""生成插件图标（16/32/48/128 px PNG），无第三方依赖。"""
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "extension" / "icons"


def write_png(path: Path, size: int) -> None:
    c1 = (0x5b / 255, 0x8c / 255, 0xff / 255)   # 左上 indigo
    c2 = (0x7d / 255, 0x5b / 255, 0xff / 255)   # 右下 violet
    white = (1.0, 1.0, 1.0)
    s = size
    r_corner = s * 0.24          # 圆角半径
    cx = cy = (s - 1) / 2

    def rounded(px, py, radius):
        # 点是否在圆角矩形内
        x0, y0, x1, y1 = 0.5, 0.5, s - 0.5, s - 0.5
        rx = min(max(px, x0 + radius), x1 - radius)
        ry = min(max(py, y0 + radius), y1 - radius)
        return (px - rx) ** 2 + (py - ry) ** 2 <= radius ** 2

    rows = []
    for y in range(s):
        row = bytearray([0])  # filter type 0
        for x in range(s):
            if not rounded(x, y, r_corner):
                row += b"\x00\x00\x00\x00"  # 透明
                continue
            t = (x + y) / (2 * (s - 1))
            base = tuple(c1[i] + (c2[i] - c1[i]) * t for i in range(3))
            # 菱形描边 + 中心实心（指纹/图层标识）
            d = abs(x - cx) + abs(y - cy)
            ring_outer = s * 0.34
            ring_inner = s * 0.26
            core = s * 0.10
            if ring_inner <= d <= ring_outer or d <= core:
                a, col = 1.0, white
            else:
                a, col = 1.0, base
            row += bytes(int(c * 255) for c in (*col, a))
        rows.append(bytes(row))

    raw = b"".join(rows)
    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", s, s, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 9))
           + chunk(b"IEND", b""))
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"icon{s}.png").write_bytes(png)
    print(f"icon{s}.png  {len(png)} bytes")


for size in (16, 32, 48, 128):
    write_png(OUT, size)


# ICO（桌面快捷方式图标）：PNG 直接内嵌的 ICO 容器
def make_ico() -> None:
    entries = []
    for size in (16, 32, 48, 256):
        png_size = size if size != 256 else 128  # 256 槽位复用 128 图
        data = (OUT / f"icon{png_size}.png").read_bytes()
        entries.append((size, data))
    header = struct.pack("<HHH", 0, 1, len(entries))
    offset = 6 + 16 * len(entries)
    directory = b""
    blobs = b""
    for size, data in entries:
        w = 0 if size == 256 else size
        directory += struct.pack("<BBBBHHII", w, w, 0, 0, 1, 32, len(data), offset)
        blobs += data
        offset += len(data)
    ico_path = Path(__file__).resolve().parent.parent / "launcher" / "workbench.ico"
    ico_path.parent.mkdir(parents=True, exist_ok=True)
    ico_path.write_bytes(header + directory + blobs)
    print(f"workbench.ico  {ico_path.stat().st_size} bytes")


make_ico()
print("完成 →", OUT)
