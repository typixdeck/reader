"""Create original tiny MOBI7 and KF8 books; no third-party book content.

Byte fields follow libmobi 0.12 src/mobi.h, read.c and util.c:
https://github.com/bfabiszewski/libmobi/tree/v0.12/src
KF8 has separate XHTML and CSS flow sections and a real FDST record.
The fixtures and original Chinese/English prose are CC0-1.0.
Run with Python 3; no external generator or downloaded executable is used.
"""
from pathlib import Path
import struct


def word(data, offset, value):
    struct.pack_into(">I", data, offset, value)


def make_book(kf8=False, encryption=0, compression=1):
    title = ("晨光中的纸船 · KF8" if kf8 else "晨光中的纸船 · MOBI7").encode()
    text = ('<html xmlns="http://www.w3.org/1999/xhtml"><head><title>第一章：出发</title>'
            + ('<link rel="stylesheet" href="kindle:flow:0001?mime=text/css"/>' if kf8 else '')
            + '</head><body><h1>第一章：出发</h1><p>清晨，小舟载着一片树叶，慢慢经过石桥。</p>'
            '<p>The paper boat followed the quiet stream.</p>'
            + ''.join(f'<p>沿河札记 {i:03d}：风吹动芦苇，水面映着远山。我们停下来，把这一刻写在纸上；'
                      '小舟继续前行，带走了安静而明亮的一天。</p>' for i in range(220))
            + '<h2>第二章：归来</h2><p>傍晚，我们在岸边把故事读完。</p></body></html>').encode()
    css = b"p { color: black; } /* not reading text */" if kf8 else b""
    raw = text + css
    # Uncompressed and PalmDOC literal-run encoding (including UTF-8 bytes).
    text_records = []
    for start in range(0, len(raw), 4096):
        chunk = raw[start:start + 4096]
        encoded = chunk if compression == 1 else b"".join(
            bytes([len(chunk[i:i+8])]) + chunk[i:i+8] for i in range(0, len(chunk), 8))
        text_records.append(encoded)
    non_text = len(text_records) + 1
    header = bytearray(280)
    struct.pack_into(">HHIHHHH", header, 0, compression, 0, len(raw), len(text_records), 4096, encryption, 0)
    header[16:20] = b"MOBI"
    for offset, value in {20:264, 24:2, 28:65001, 32:20260913,
                          36:8 if kf8 else 6, 80:non_text, 84:280, 88:len(title),
                          92:0x804, 104:8 if kf8 else 6, 168:0xFFFFFFFF}.items():
        word(header, offset, value)
    for offset in (*range(40, 80, 4), 108, 112, 120, 164, 200, 208, 224, 244, 248, 252, 256, 260):
        word(header, offset, 0xFFFFFFFF)
    if kf8:
        word(header, 192, non_text)
        word(header, 196, 2)
    else:
        struct.pack_into(">HH", header, 192, 1, len(text_records))
    records = [bytes(header) + title] + text_records
    if kf8:
        records.append(b"FDST" + struct.pack(">IIIIII", 12, 2, 0, len(text), len(text), len(raw)))
    else:
        records.append(b"\xe9\x8e\r\n")
    return pack_records(records)


def pack_records(records):
    pdb = bytearray(78)
    pdb[:16] = b"TypixStorySample"
    pdb[60:68] = b"BOOKMOBI"
    struct.pack_into(">H", pdb, 76, len(records))
    table, offset = bytearray(), 78 + 8 * len(records) + 2
    for i, record in enumerate(records):
        table += struct.pack(">II", offset, i * 2)
        offset += len(record)
    return bytes(pdb + table + b"\0\0") + b"".join(records)


if __name__ == "__main__":
    root = Path(__file__).parent
    (root / "paper-boat.mobi").write_bytes(make_book())
    (root / "paper-boat.prc").write_bytes(make_book(compression=2))
    (root / "paper-boat.azw3").write_bytes(make_book(kf8=True))
