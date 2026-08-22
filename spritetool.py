#!/usr/bin/env python3
"""
wa-spritetool: extract Worms Armageddon graphics archives and decode the
sprites and images inside them.

The .dir container and the Team17 compression routine follow the Worms
Knowledge Base; the .spr sprite format has no public specification and was
reverse engineered for this tool -- see SPR_FORMAT.md.
"""

import struct
import sys
import os
from pathlib import Path
from typing import Optional, BinaryIO, Dict, Tuple, List, Sequence

try:
    from PIL import Image
except ImportError:
    sys.exit("this needs Pillow: pip install pillow")

try:
    import numpy as np
except ImportError:  # optional; only makes fitting PNG colours quicker
    np = None

__version__ = "0.3.0"

# Sanity bound on decoded sprite dimensions. Level themes ship full-screen
# backdrops, the largest seen being 1280x370.
MAX_DIM = 8192

# Highest offset a frame may start at within its stream. The game's own
# archives never exceed it, and matching the rule reproduces their stream
# splitting exactly; see encode_sprite.
MAX_DATA_POS = 16384

# Most streams a sprite may have. A frame names its stream as stream*256 in a
# u16 the game reads as signed, so stream 128 is the first whose selector
# (32768) flips negative and sends the loader to a wild pointer -- the game
# dies with an access violation. Nothing shipped exceeds 128 streams (the
# 128-frame front.spr files are the largest), which is the encoder's own
# tools refusing to go past it too.
MAX_STREAMS = 128


class DecompressionError(Exception):
    """Raised when a Team17 stream is malformed or does not decode as expected.

    Carries partial progress so callers investigating a format can see how far
    the stream got before failing.
    """

    def __init__(self, message: str, produced: int = 0, consumed: int = 0):
        super().__init__(message)
        self.produced = produced
        self.consumed = consumed


class Team17Decompressor:
    """Decompresses data using Team17's compression algorithm"""

    @staticmethod
    def decompress(compressed_data: bytes, decompressed_size: int,
                   allow_short: bool = False) -> bytes:
        """
        Decompress Team17-compressed data.

        Args:
            compressed_data: The compressed byte stream
            decompressed_size: Expected size of decompressed data
            allow_short: If True, a stream that terminates cleanly before
                filling the buffer is accepted (remainder stays zero).

        Returns:
            Decompressed bytes of exactly decompressed_size length.

        Raises:
            DecompressionError: on a malformed stream. Never returns a
                partially-filled buffer silently -- a zero-padded result is
                indistinguishable from a correctly decoded transparent image,
                which hides format bugs.
        """
        dstream = bytearray(decompressed_size + 3)  # 3 bytes slack, see docs
        output = 0
        pos = 0

        while pos < len(compressed_data) and output < decompressed_size:
            cmd = compressed_data[pos]
            pos += 1

            if (cmd & 0x80) == 0:
                # command: 1 byte (literal colour index)
                dstream[output] = cmd
                output += 1
                continue

            if pos >= len(compressed_data):
                raise DecompressionError(
                    "stream ended mid-command (expected arg2)", output, pos)

            arg1 = (cmd >> 3) & 0x0F
            arg2 = ((cmd << 8) | compressed_data[pos]) & 0x7FF
            pos += 1

            if arg1 == 0:
                if arg2 == 0:
                    # 0x80 0x00 is the reference routine's failure case
                    raise DecompressionError(
                        "invalid command 0x80 0x00", output, pos)
                if pos >= len(compressed_data):
                    raise DecompressionError(
                        "stream ended mid-command (expected arg3)", output, pos)
                repeat = compressed_data[pos] + 18
                pos += 1
                copy_offset = arg2
            else:
                repeat = arg1 + 2
                copy_offset = arg2 + 1

            if copy_offset > output:
                raise DecompressionError(
                    f"back-reference {copy_offset} precedes buffer start "
                    f"(only {output} bytes produced)", output, pos)
            if output + repeat > len(dstream):
                raise DecompressionError(
                    f"copy of {repeat} bytes at {output} overruns "
                    f"{len(dstream)}-byte buffer", output, pos)

            Team17Decompressor._copy_data(dstream, output, copy_offset, repeat)
            output += repeat

        if output < decompressed_size and not allow_short:
            raise DecompressionError(
                f"stream produced {output} of {decompressed_size} bytes",
                output, pos)

        return bytes(dstream[:decompressed_size])

    @staticmethod
    def _copy_data(buffer: bytearray, offset: int, copy_offset: int, repeat: int) -> None:
        """Copy data from earlier in buffer (LZ77-style back-reference)"""
        for _ in range(repeat):
            buffer[offset] = buffer[offset - copy_offset]
            offset += 1


class AquaDecompressor:
    """The second compression in Worms World Party Aqua.

    Aqua ships some files -- every per-level backdrop and gradient, and the
    gauge and loading-screen images -- under a different scheme from Team17's,
    with no published description. It was reverse engineered for this tool
    against `gradient.spr`, whose Team17-compressed sibling `gradient.img`
    gives the same picture and palette to check against.

        0xFF a b c   copy c bytes from (a << 8 | b) back
        0xFF 0 0 0   end of stream
        n            emit the n bytes that follow, unchanged

    A literal byte is never 0xFF, which is what leaves the escape
    unambiguous. Distances observed reach 4095, so the window is 12 bits.
    """

    ESCAPE = 0xFF

    @staticmethod
    def decompress(data: bytes, decompressed_size: int) -> bytes:
        out = bytearray()
        i = 0
        n = len(data)
        while i < n and len(out) < decompressed_size:
            b = data[i]
            if b != AquaDecompressor.ESCAPE:
                # Literal run: a count, then that many bytes verbatim.
                i += 1
                chunk = data[i:i + b]
                if len(chunk) != b:
                    raise DecompressionError(
                        'literal run runs past the end of the stream',
                        len(out), i)
                out += chunk
                i += b
                continue
            if i + 3 >= n:
                raise DecompressionError(
                    'stream ended mid-command', len(out), i)
            dist = (data[i + 1] << 8) | data[i + 2]
            length = data[i + 3]
            i += 4
            if dist == 0 and length == 0:
                break
            if dist == 0 or dist > len(out):
                raise DecompressionError(
                    f'back-reference {dist} precedes buffer start '
                    f'(only {len(out)} bytes produced)', len(out), i)
            for _ in range(length):
                out.append(out[len(out) - dist])
        if len(out) < decompressed_size:
            raise DecompressionError(
                f'stream produced {len(out)} of {decompressed_size} bytes',
                len(out), i)
        return bytes(out[:decompressed_size])


def decompress_stream(data: bytes, size: int) -> bytes:
    """Decompress a stream in whichever of the two schemes it uses.

    Nothing in the headers distinguishes them -- Aqua's Gfx.dir holds both --
    so the choice is made by trying, and the order matters. It is not
    symmetric: 2933 of the shipped Team17 streams also decode cleanly under
    the Aqua reader, while none of the 41 Aqua streams decodes under Team17.
    So Team17 must be tried first, and only a stream it rejects is Aqua's.
    """
    try:
        return Team17Decompressor.decompress(data, size)
    except DecompressionError:
        return AquaDecompressor.decompress(data, size)


class Team17Compressor:
    """Produces streams that `Team17Decompressor` can read back.

    The format allows many valid encodings of the same data, so output is not
    byte-identical to Team17's own tools; it only has to decode to the same
    bytes. Round-tripping through `Team17Decompressor` is necessary but not
    sufficient evidence of that -- it shares this module's assumptions, so a
    stream both agree on can still be one the game reads differently.

    Command set (mirrors the decompressor):
        0xxxxxxx                    literal colour index, 0-127
        1aaaa bbbbbbbbbbb           copy arg1+2 bytes from arg2+1 back
        10000 bbbbbbbbbbb cccccccc  copy arg3+18 bytes from arg2 back

    So a match reaches at most 2047 bytes back and copies 3-17 bytes with the
    two-byte form or 18-273 with the three-byte form.
    """

    MAX_DIST = 2047
    MIN_MATCH = 3
    MAX_MATCH = 273
    # Sprite data is highly repetitive, so a single 3-byte key can accumulate
    # thousands of positions. Walking them all is what makes a naive encoder
    # unusably slow; the newest few almost always contain the best match.
    MAX_CHAIN = 32

    @staticmethod
    def compress(data: bytes) -> bytes:
        out = bytearray()
        # Map the next three bytes to recent positions, newest last, so the
        # search walks plausible candidates instead of the whole window.
        index: Dict[bytes, List[int]] = {}
        n = len(data)
        min_match = Team17Compressor.MIN_MATCH
        max_match = Team17Compressor.MAX_MATCH
        max_dist = Team17Compressor.MAX_DIST
        max_chain = Team17Compressor.MAX_CHAIN
        i = 0
        while i < n:
            best_len = 0
            best_dist = 0
            if i + min_match <= n:
                key = data[i:i + min_match]
                chain = index.get(key)
                if chain:
                    lo = i - max_dist
                    limit = min(max_match, n - i)
                    tried = 0
                    for pos in reversed(chain):
                        if pos < lo or tried >= max_chain:
                            break
                        tried += 1
                        # Runs are legal: the copy reads bytes this same
                        # command is still writing, which is how long fills
                        # are encoded.
                        length = 0
                        while length < limit and data[pos + length] == data[i + length]:
                            length += 1
                        if length > best_len:
                            best_len = length
                            best_dist = i - pos
                            if length >= limit:
                                break

            literal = data[i]
            # A literal command only encodes 0-127; the top bit marks a match.
            if best_len >= Team17Compressor.MIN_MATCH:
                Team17Compressor._emit_match(out, best_dist, best_len)
                advance = best_len
            elif literal < 0x80:
                out.append(literal)
                advance = 1
            else:
                # 128-255 has no literal command and no match was found, so
                # this byte is unencodable. The largest palette in any shipped
                # archive is 96 colours, so it does not arise in practice.
                raise ValueError(
                    f"cannot encode byte {literal:#04x} at offset {i}: values "
                    "above 0x7F have no literal command and no earlier "
                    "occurrence to copy. Palette indices must stay below 128.")

            for k in range(i, min(i + advance, n - min_match + 1)):
                chain = index.setdefault(data[k:k + min_match], [])
                chain.append(k)
                # Keep chains bounded. Without this they grow to the length of
                # the file and the search degenerates even with MAX_CHAIN,
                # because most entries are already out of range.
                if len(chain) > max_chain:
                    del chain[:-max_chain]
            i += advance

        # Every stream Team17 ships ends with this marker. Our decompressor
        # stops once the buffer is full and so never needs it, which is why
        # omitting it still round-trips here -- match the reference anyway.
        out += b'\x80\x00'
        return bytes(out)

    @staticmethod
    def _emit_match(out: bytearray, dist: int, length: int) -> None:
        if length <= 17:
            # 2-byte form: length = arg1+2 (so 3..17), distance = arg2+1
            arg1 = length - 2
            arg2 = dist - 1
            out.append(0x80 | (arg1 << 3) | ((arg2 >> 8) & 0x07))
            out.append(arg2 & 0xFF)
        else:
            # 3-byte form: length = arg3+18 (so 18..273), distance = arg2
            arg2 = dist
            out.append(0x80 | ((arg2 >> 8) & 0x07))
            out.append(arg2 & 0xFF)
            out.append(length - 18)


class SpriteFile:
    """A Worms Armageddon sprite file (.spr).

    The layout was reverse engineered against spriteEditor's output; see
    SPR_FORMAT.md. Uncompressed sprites have no stream table and take a
    separate path.

        0   "SPR\\x1A"
        4   u32  file length
        8   u16  flags        bit 0x4000 set = Team17-compressed
        10  u16  palette entry count
        12  palette, 3 bytes per entry, RGB, starting at colour 1
            u32  stream count
            pad to a 4-byte boundary
            Stream[] 12 bytes each, in one of two field orders (below)
            Sprite   u16 frame rate, flags, width, height, frame count
            Frame[]  u16 data_pos, stream_selector, left, up, right, down
            stream data (offsets relative to here)  q

    `ncol % 4` alone selects the stream-table layout, and the sprite record
    always begins at a file offset congruent to 2 mod 4 -- the two layouts
    reach that offset from opposite sides:

        ncol % 4 in (0, 1)  (position, unused, decompressed length)
                            whole records, then two bytes of padding
        ncol % 4 in (2, 3)  (unused, decompressed length, position of the
                            NEXT stream); stream 0 starts at 0, so the last
                            record's position is meaningless and its final
                            two bytes are simply not written

    A frame holds only its cropped bounding box within the sprite cell;
    `left`/`up`/`right`/`down` are measured from the top-left, and frames are
    in natural animation order.
    """

    SIGNATURE = b'SPR\x1A'
    COMPRESSED_FLAG = 0x4000

    def __init__(self, data: bytes):
        self.data = data
        self.frames = 0
        self.width = 0
        self.height = 0
        self.flags = 0
        self.framerate = 0
        self.palette: bytes = b''
        self.blobs: List[bytes] = []
        self.recs: List[Tuple[int, int, int, int, int, int]] = []
        self.description = ''
        self.base = 8               # offset of the flags word

    def parse(self) -> bool:
        if len(self.data) < 12 or self.data[0:4] != self.SIGNATURE:
            return False
        try:
            self._read_description()
            return (self._parse_compressed() if self.is_compressed
                    else self._parse_uncompressed())
        except (struct.error, IndexError, ValueError, DecompressionError):
            return False

    def _read_description(self) -> None:
        """Online Worms names its sprites in the file, ahead of the flags word.

        The flags always have bit 0x8000 set, which no letter pair produces,
        so its absence marks the older form -- the same trick ImageFile uses
        to spot a description there.
        """
        if struct.unpack('<H', self.data[8:10])[0] & 0x8000:
            return
        end = self.data.find(b'\x00', 8)
        if end < 0:
            raise ValueError('unterminated sprite description')
        self.description = self.data[8:end].decode('latin-1', 'replace')
        self.base = end + 1

    @property
    def is_compressed(self) -> bool:
        """Uncompressed sprites (flag bit clear) store raw cropped pixels and
        have no stream table; Gfx0.dir is entirely uncompressed."""
        b = self.base
        return bool(struct.unpack('<H', self.data[b:b + 2])[0] & self.COMPRESSED_FLAG)

    def _header(self):
        b = self.base
        ncol = struct.unpack('<H', self.data[b + 2:b + 4])[0]
        p = b + 4 + ncol * 3
        nstream = struct.unpack('<I', self.data[p:p + 4])[0]
        # A level backdrop runs to 128 streams, well past anything in Gfx.dir.
        if not 0 < nstream <= 4096:
            raise ValueError('implausible stream count')
        return ncol, p, nstream

    def _finish(self, ncol, p, rec, ft, blobs) -> bool:
        """Read the sprite record at `rec` and the frame table at `ft`."""
        rate, flags, w, h, fc = struct.unpack('<HHHHH', self.data[rec:rec + 10])
        if not (0 < w <= MAX_DIM and h <= MAX_DIM and 0 < fc <= 4096):
            return False
        if ft + fc * 12 > len(self.data):
            return False
        # The box is signed: a frame may hang off the left or top of its
        # cell (WWPA's scrolling water layers start at left = -4).
        recs = [struct.unpack('<HHhhhh', self.data[ft + i * 12:ft + i * 12 + 12])
                for i in range(fc)]
        if h == 0:
            # Online Worms leaves the cell height at 0 on its backdrops and
            # lets the frame boxes carry the extent.
            h = max((r[5] for r in recs), default=0)
            if not 0 < h <= MAX_DIM:
                return False
        self.framerate, self.flags = rate, flags
        self.width, self.height, self.frames = w, h, fc
        self.palette = self.data[self.base + 4:p]
        self.blobs = blobs
        self.recs = recs
        return True

    def _parse_uncompressed(self) -> bool:
        """Uncompressed sprites (flag bit 0x4000 clear, as in Gfx0.dir).

        There is no stream count and no stream table: the palette is followed
        by a 4-byte gap (holding the frame rate at +2), the sprite record, one
        pad byte, the frame table, then the cropped frame pixels laid out
        contiguously. All frame data is treated as a single implicit stream.
        """
        b = self.base
        ncol = struct.unpack('<H', self.data[b + 2:b + 4])[0]
        p = b + 4 + ncol * 3
        rec = p + 2
        # The frame table is aligned the way the stream table is in the
        # compressed form: pad the sprite record by ncol % 4. Online Worms
        # does not pad here either, matching its fixed stream-table padding.
        ft = rec + 10 + (0 if self.description else ncol % 4)
        ds = ft + struct.unpack('<H', self.data[rec + 8:rec + 10])[0] * 12
        if ds > len(self.data):
            return False
        return self._finish(ncol, p, rec, ft, [self.data[ds:]])

    def _parse_compressed(self) -> bool:
        """Read the stream table, then the frames it holds.

        Both field orders carry the decompressed length; they differ in how a
        stream's position is found. See the class docstring for the layouts.
        """
        ncol, p, nstream = self._header()
        if self.description:
            # Online Worms pads the stream count by a fixed two bytes and only
            # ever uses the next-position field order, whatever the palette
            # length -- measured across 1716 of its sprites.
            layouts = [(p + 6, False, False)]
        else:
            # Armageddon's, then Aqua's. Aqua reuses Online Worms' table
            # placement but aligns the frame table to four bytes, which Online
            # Worms does not: 1250 of its sprites leave it unaligned.
            layouts = [(p + 4 + (ncol % 4), ncol % 4 in (0, 1), False),
                       (p + 6, False, True)]
        for q, positional, align in layouts:
            if self._try_layout(ncol, p, nstream, q, positional, align):
                return True
        return False

    def _try_layout(self, ncol, p, nstream, q, positional, align) -> bool:
        rec = q + nstream * 12 + (2 if positional else -2)
        if rec < 0 or rec + 10 > len(self.data):
            return False
        ft = rec + 10
        if align:
            ft += -ft % 4
        fc = struct.unpack('<H', self.data[rec + 8:rec + 10])[0]
        ds = ft + fc * 12
        if ds > len(self.data):
            return False

        # The last record of the non-positional form is two bytes short, so
        # its position field is never read -- only records 0..n-2 supply one.
        recs = [struct.unpack('<III', self.data[q + k * 12:q + k * 12 + 12])
                for k in range(nstream)]
        if positional:
            spans = [(r[0], r[2]) for r in recs]
        else:
            spans = [(0 if k == 0 else recs[k - 1][2], recs[k][1])
                     for k in range(nstream)]
        try:
            blobs = [decompress_stream(self.data[ds + pos:], dlen)
                     if dlen else b'' for pos, dlen in spans]
        except (DecompressionError, IndexError, struct.error):
            return False
        return self._finish(ncol, p, rec, ft, blobs)

    def render_frame(self, index: int) -> Optional[bytes]:
        """Frame as width*height palette indices in top-down row order."""
        dpos, sel, l, u, r, d = self.recs[index]
        if self.is_compressed:
            stream = sel // 256
            if stream >= len(self.blobs):
                return None
            src = self.blobs[stream]
            start = dpos
        else:
            # Uncompressed sprites hold one contiguous block of pixels and
            # `dpos` is only 16 bits, so `sel` carries the high bits of the
            # offset: a frame begins at sel * 65536 + dpos.
            src = self.blobs[0]
            start = sel * 65536 + dpos
        w, h = self.width, self.height
        fw, fh = r - l, d - u
        if fw < 0 or fh < 0 or start + fw * fh > len(src):
            return None
        # A zero-area box is a legitimate empty frame, not an error.
        cell = bytearray(w * h)
        for y in range(fh):
            dest = u + y
            if dest < 0:
                continue
            if dest >= h:
                break
            row = src[start + y * fw:start + (y + 1) * fw]
            # A few sprites (circle25) declare a box wider than the cell, and
            # a few start left of it; the game clips rather than wrapping into
            # the neighbouring row.
            skip = max(0, -l)
            x = l + skip
            visible = min(fw - skip, w - x)
            if visible > 0:
                cell[dest * w + x:dest * w + x + visible] = row[skip:skip + visible]
        return bytes(cell)

    def render_sheet(self) -> Optional[bytes]:
        """All frames stacked vertically, frame 0 at the top."""
        rows = []
        for i in range(self.frames):
            f = self.render_frame(i)
            if f is None:
                return None
            rows.append(f)
        return b''.join(rows)

    def rgb_palette(self) -> bytes:
        """Palette expanded to a 256-entry RGB table for image output.

        Entries are stored as RGB. Colour 0 is the transparent background and
        is not stored: the table on disk begins at colour 1, so entries shift
        up by one and index 0 stays black.
        """
        out = bytearray(768)
        for i in range(min(self.ncolours, 255)):
            out[(i + 1) * 3:(i + 1) * 3 + 3] = self.palette[i * 3:i * 3 + 3]
        return bytes(out)

    @property
    def ncolours(self) -> int:
        return len(self.palette) // 3

    def to_metadata_string(self) -> str:
        """Export metadata in .spd format"""
        return (
            f"frames = {self.frames}\r\n"
            f"height = {self.height}\r\n"
            f"width = {self.width}\r\n"
            f"framerate = {self.framerate}\r\n"
            f"flags = {self.flags}\r\n"
        )

    @staticmethod
    def _create_bmp(pixel_data: bytes, palette: bytes, width: int, height: int) -> Optional[bytes]:
        """
        Create 8-bit BMP file data from pixel data and palette.

        Args:
            pixel_data: Raw pixel data (width × height bytes)
            palette: 256-color palette (768 bytes, RGB format)
            width: Image width in pixels
            height: Image height in pixels

        Returns:
            Complete BMP file data, or None if creation fails
        """
        if len(pixel_data) != width * height:
            return None
        if len(palette) != 768:
            return None

        # BMP scanlines are padded to a 4-byte boundary and stored bottom-up.
        # pixel_data arrives top-down, so emit the rows in reverse.
        stride = (width + 3) // 4 * 4
        pad = bytes(stride - width)
        padded = b''.join(pixel_data[y * width:(y + 1) * width] + pad
                          for y in range(height - 1, -1, -1))

        # BMP file header (14 bytes)
        bmp_signature = b'BM'
        reserved = b'\x00\x00\x00\x00'

        # DIB header (40 bytes - BITMAPINFOHEADER)
        dib_size = struct.pack('<I', 40)
        width_bytes = struct.pack('<i', width)
        height_bytes = struct.pack('<i', height)
        planes = struct.pack('<H', 1)
        bits_per_pixel = struct.pack('<H', 8)
        compression = struct.pack('<I', 0)
        image_size = struct.pack('<I', len(padded))
        x_pixels_per_meter = struct.pack('<i', 0)
        y_pixels_per_meter = struct.pack('<i', 0)
        colors_used = struct.pack('<I', 256)
        important_colors = struct.pack('<I', 0)

        dib_header = (
            dib_size + width_bytes + height_bytes + planes + bits_per_pixel +
            compression + image_size + x_pixels_per_meter +
            y_pixels_per_meter + colors_used + important_colors
        )

        # Palette (256 × 4 bytes, BGRA format - convert RGB to BGRA)
        palette_data = bytearray()
        for i in range(256):
            r, g, b = palette[i*3], palette[i*3+1], palette[i*3+2]
            palette_data.extend([b, g, r, 0])  # BGRA

        offset_to_pixels = 14 + len(dib_header) + len(palette_data)
        file_size = offset_to_pixels + len(padded)

        file_size_bytes = struct.pack('<I', file_size)
        offset_bytes = struct.pack('<I', offset_to_pixels)

        bmp_header = bmp_signature + file_size_bytes + reserved + offset_bytes

        return bmp_header + dib_header + palette_data + padded

    @staticmethod
    def _create_gif_pillow(frames_data: bytes, palette: bytes, width: int,
                           height: int, frame_count: int,
                           framerate: int) -> Optional[bytes]:
        """Write the animation with Pillow when it is installed.

        Preferred over the built-in writer: hand-rolled GIF output silently
        produced corrupt frames twice during development (a bad LZW code-width
        schedule, then missing frame disposal), and neither was visible without
        decoding the result.
        """
        import io
        # GIF stores the delay in centiseconds, so anything below 20ms rounds
        # to 0-1cs. Browsers and most viewers silently clamp delays that short
        # to ~100ms, which makes playback erratic rather than fast. Keep every
        # frame at or above 20ms. The game runs at roughly 20-25fps, so 50ms
        # is a sensible default when the sprite declares no rate.
        delay = 50 if framerate <= 0 else max(20, round(1000 / framerate / 10) * 10)
        cells = []
        for i in range(frame_count):
            im = Image.frombytes('P', (width, height),
                                 frames_data[i * width * height:(i + 1) * width * height])
            im.putpalette(palette)
            cells.append(im)

        # Always loop the preview. Flag 0 means "play once" in game, but a
        # preview that stops on its last frame is harder to browse, and the
        # true playback mode is still recorded in the .spd file.
        loop = 0

        buf = io.BytesIO()
        # Pillow merges a frame identical to its predecessor into that frame
        # and adds the durations together, so a sprite that holds a pose ends
        # up with fewer GIF frames but the same total running time. That is
        # correct GIF encoding -- do not "fix" it by forcing duplicates.
        # palette=: force every frame to share the global colour table.
        # Without it Pillow re-optimises the palette per frame and emits a
        # local colour table on almost every one; macOS Preview mishandles
        # those and appears to animate only the final frames, though browsers
        # play them correctly. Every frame of a sprite uses the same palette
        # anyway, so this is both more compatible and ~60% smaller.
        cells[0].save(buf, format='GIF', save_all=True, append_images=cells[1:],
                      duration=delay, loop=loop, disposal=2, optimize=False,
                      palette=palette)
        return buf.getvalue()

    @staticmethod
    def _create_gif(frames_data: bytes, palette: bytes, width: int, height: int,
                    frame_count: int, framerate: int, flags: int = 1) -> Optional[bytes]:
        """
        Create an animated GIF from frame data and palette.

        Args:
            frames_data: All frame pixel data concatenated (width × height × frame_count bytes)
            palette: 256-color palette (768 bytes, RGB format)
            width: Frame width in pixels
            height: Frame height in pixels
            frame_count: Number of frames
            framerate: Frame rate (0 = no delay, higher = slower)

        Returns:
            Complete GIF file data, or None if creation fails
        """
        if len(frames_data) != width * height * frame_count:
            return None
        if len(palette) != 768:
            return None

        # Sprite flags: 0 = play once, 1 = loop, 2 = forwards then backwards
        # once, 3 = ping-pong forever. A ping-pong sprite's last frame is
        # nothing like its first (cloudm grows monotonically), so looping it
        # forwards only produces a visible jump at the wrap. Materialise the
        # return leg here, before either writer runs -- GIF has no ping-pong
        # mode, and both writers must agree on what they are given.
        if flags in (2, 3) and frame_count > 2:
            cell = width * height
            cells = [frames_data[i * cell:(i + 1) * cell] for i in range(frame_count)]
            cells += cells[-2:0:-1]
            frames_data = b''.join(cells)
            frame_count = len(cells)

        # GIF is written with Pillow. There was a hand-rolled writer here as a
        # fallback, and it silently produced corrupt frames twice -- a bad LZW
        # code-width schedule, then missing frame disposal -- neither visible
        # without decoding the result. Pillow is required now, so the second
        # implementation is gone rather than kept as a trap.
        return SpriteFile._create_gif_pillow(
            frames_data, palette, width, height, frame_count, framerate)


class ImageFile:
    """A Team17 image file (.img) -- a single palettised bitmap.

    Simpler than SpriteFile: no frames, no stream table.

        0   "IMG\\x1A"
        4   u32  file length
        -   optional NUL-terminated description (Worms 2 / Online Worms only)
        8   u8   bits per pixel (always 8 in practice)
        9   u8   flags: 0x40 = Team17-compressed, 0x80 = has palette
        -   u16  palette colour count, then count*3 RGB bytes  (if 0x80)
        -   u16  width
        -   u16  height
        -   image data, compressed or raw width*height bytes

    As in .spr, colour 0 is the transparent background and is not stored, so
    the palette on disk begins at colour 1.
    """

    SIGNATURE = b'IMG\x1A'
    COMPRESSED_FLAG = 0x40
    PALETTE_FLAG = 0x80

    def __init__(self, data: bytes, aligned: bool = True):
        self.data = data
        # Images inside a .dir start their data on a 4-byte boundary; the ones
        # embedded in land.dat do not. Measured both ways: see LandFile.
        self.aligned = aligned
        self.width = 0
        self.height = 0
        self.bpp = 0
        self.flags = 0
        self.palette: bytes = b''
        self.pixels: bytes = b''
        self.description = ''
        self.stride = 0

    def parse(self) -> bool:
        d = self.data
        if len(d) < 12 or d[0:4] != self.SIGNATURE:
            return False
        i = 8
        # Some Worms 2 / Online Worms images carry a description string here.
        # Distinguish it from a bpp byte: a real bpp is a small number and is
        # followed by a plausible flags byte.
        if d[i] not in (1, 2, 4, 8, 16, 24, 32):
            end = d.find(b'\x00', i)
            if end < 0:
                return False
            self.description = d[i:end].decode('latin-1', 'replace')
            i = end + 1
        self.bpp = d[i]
        self.flags = d[i + 1]
        i += 2
        if self.flags & self.PALETTE_FLAG:
            ncol = struct.unpack('<H', d[i:i + 2])[0]
            i += 2
            self.palette = d[i:i + ncol * 3]
            i += ncol * 3
        if i + 4 > len(d):
            return False
        self.width, self.height = struct.unpack('<HH', d[i:i + 4])
        i += 4
        if not (0 < self.width <= 4096 and 0 < self.height <= 4096):
            return False
        # Rows are packed to a whole number of bytes. Every image in a .dir is
        # 8 bpp, where this is just width * height; the 1 bpp form turns up in
        # land.dat, whose collision and background masks use it.
        self.stride = (self.width * self.bpp + 7) // 8
        need = self.stride * self.height
        if self.flags & self.COMPRESSED_FLAG:
            # Image data starts on a 4-byte boundary -- but only in this,
            # the plain form, and only inside a .dir. The Worms 2 / Online
            # Worms form that carries a description string is never padded
            # (1101 such images across the shipped archives, none compressed).
            if not self.description and self.aligned:
                i += -i % 4
            try:
                self.pixels = decompress_stream(d[i:], need)
            except DecompressionError:
                return False
        else:
            # Stored raw, so the declared file length gives the padding
            # outright. Preferred over assuming the alignment: a handful of
            # third-party images are written without it.
            pad = len(d) - i - need
            if 0 <= pad <= 3:
                i += pad
            raw = d[i:i + need]
            if len(raw) < need:
                return False
            self.pixels = raw
        if self.bpp == 1:
            # Expand to one byte per pixel so callers need not care, dropping
            # the row padding as they go.
            flat = bytearray(self.width * self.height)
            for y in range(self.height):
                row = self.pixels[y * self.stride:(y + 1) * self.stride]
                base = y * self.width
                for x in range(self.width):
                    flat[base + x] = (row[x >> 3] >> (7 - (x & 7))) & 1
            self.pixels = bytes(flat)
        return True

    @property
    def ncolours(self) -> int:
        return len(self.palette) // 3

    def rgb_palette(self) -> bytes:
        """256-entry RGB table; colour 0 is the unstored transparent black."""
        out = bytearray(768)
        if not self.ncolours and self.bpp == 1:
            # A land.dat mask carries no palette of its own. Render it as
            # black and white rather than black on black.
            out[3:6] = b'\xff\xff\xff'
            return bytes(out)
        for k in range(min(self.ncolours, 255)):
            out[(k + 1) * 3:(k + 1) * 3 + 3] = self.palette[k * 3:k * 3 + 3]
        return bytes(out)


class BankFile:
    """A Team17 sprite bank (.bnk) -- many animations sharing one palette.

    Worms World Party Aqua keeps its worm animations here rather than in
    loose .spr files: `mainspr.bnk` alone holds 1136 of them.

        0   "BNK\\x1A"
        4   u32  file length
        8   u16  palette entry count, excluding the background colour
            palette, 3 bytes per entry
            pad to a 4-byte position
            u32  sprite count,  Sprite[] 12 bytes each
            u32  frame count,   Frame[]  12 bytes each
            u32  stream count,  Stream[] 12 bytes each
            stream data

    Sprites index a shared frame table, and frames index a shared stream
    table, so one stream commonly backs a whole animation.

    Two details differ from the published description. The palette is stored
    RGB, not BGR: matched against a known Worms Armageddon worm palette, the
    stored order scores a mean colour distance of 9.0 against 15.9 for the
    swap, and swapping turns worm flesh grey-blue. The Frame struct also
    leads with the stream number, where .spr leads with the data position.
    """

    SIGNATURE = b'BNK\x1A'

    def __init__(self, data: bytes):
        self.data = data
        self.palette: bytes = b''
        self.sprites: List[Tuple[int, int, int, int, int, int, int]] = []
        self.frames: List[Tuple[int, int, int, int, int, int]] = []
        self.streams: List[Tuple[int, int, int]] = []
        self.data_start = 0
        self._blobs: Dict[int, bytes] = {}

    def parse(self) -> bool:
        d = self.data
        if len(d) < 12 or d[0:4] != self.SIGNATURE:
            return False
        try:
            ncol = struct.unpack('<H', d[8:10])[0]
            i = 10 + ncol * 3
            self.palette = d[10:i]
            i += -i % 4
            nspr = struct.unpack('<I', d[i:i + 4])[0]
            i += 4
            self.sprites = [struct.unpack('<HHHHHBB', d[i + k * 12:i + k * 12 + 12])
                            for k in range(nspr)]
            i += nspr * 12
            nfrm = struct.unpack('<I', d[i:i + 4])[0]
            i += 4
            # Offsets are signed; a frame may hang off its cell, as in .spr.
            self.frames = [struct.unpack('<HHhhhh', d[i + k * 12:i + k * 12 + 12])
                           for k in range(nfrm)]
            i += nfrm * 12
            nstr = struct.unpack('<I', d[i:i + 4])[0]
            i += 4
            self.streams = [struct.unpack('<III', d[i + k * 12:i + k * 12 + 12])
                            for k in range(nstr)]
            i += nstr * 12
        except (struct.error, IndexError):
            return False
        self.data_start = i
        if i > len(d) or not self.sprites:
            return False
        # Every sprite's frame range must land inside the shared frame table.
        return all(0 <= fs and fs + fc <= nfrm for _f, _w, _h, fs, fc, _u, _r
                   in self.sprites)

    @property
    def ncolours(self) -> int:
        return len(self.palette) // 3

    def rgb_palette(self) -> bytes:
        """256-entry RGB table; colour 0 is the unstored transparent black."""
        out = bytearray(768)
        for k in range(min(self.ncolours, 255)):
            out[(k + 1) * 3:(k + 1) * 3 + 3] = self.palette[k * 3:k * 3 + 3]
        return bytes(out)

    def _stream(self, index: int) -> bytes:
        if index not in self._blobs:
            pos, dlen, _unused = self.streams[index]
            self._blobs[index] = decompress_stream(
                self.data[self.data_start + pos:], dlen)
        return self._blobs[index]

    def render_sheet(self, index: int) -> Optional[bytes]:
        """Sprite `index` as its frames stacked vertically, frame 0 on top."""
        _flags, w, h, start, count, _unk, _rate = self.sprites[index]
        if not (0 < w <= MAX_DIM and 0 < h <= MAX_DIM and count):
            return None
        rows = bytearray()
        for j in range(start, start + count):
            sel, dpos, left, up, right, down = self.frames[j]
            if sel >= len(self.streams):
                return None
            try:
                src = self._stream(sel)
            except DecompressionError:
                return None
            fw, fh = right - left, down - up
            if fw < 0 or fh < 0 or dpos + fw * fh > len(src):
                return None
            cell = bytearray(w * h)
            for y in range(fh):
                dest = up + y
                if dest < 0:
                    continue
                if dest >= h:
                    break
                row = src[dpos + y * fw:dpos + (y + 1) * fw]
                skip = max(0, -left)
                x = left + skip
                visible = min(fw - skip, w - x)
                if visible > 0:
                    cell[dest * w + x:dest * w + x + visible] = row[skip:skip + visible]
            rows += cell
        return bytes(rows)


class LandFile:
    """A land.dat map: the terrain as generated, not as tiled from a theme.

        0   "LND\\x1A"  Worms 2 / Armageddon / World Party / Online Worms
            "LND\\x1B"  World Party Aqua
        4   u32  file length
        8   u32  width, u32 height
            u32  indestructible cavern border
            u32  water height          (Armageddon, World Party, Aqua)
            u32  unknown               (Armageddon, Aqua)
            u32  object count, then that many (i32 x, i32 y)
            u32  unknown               (Worms 2, Online Worms)
            IMG  8 bpp foreground, the visible land
            IMG  1 bpp collision mask, black where the land is solid
            IMG  1 bpp background land
            IMG  1 bpp small, purpose unknown  (Worms 2, Online Worms)
            two 1-byte length-prefixed paths: land texture, then water.dir

    The images embedded here are NOT 4-byte aligned, unlike those inside a
    .dir. Measured on all 51 in Aqua's maps: reading from the header end
    consumes each stream to exactly four bytes short of its declared length,
    which is its terminator, while the aligned offset does not decode at all
    for the 1 bpp masks.
    """

    SIGNATURES = (b'LND\x1A', b'LND\x1B')

    def __init__(self, data: bytes):
        self.data = data
        self.width = 0
        self.height = 0
        self.border = 0
        self.water = 0
        self.objects: List[Tuple[int, int]] = []
        self.images: List[ImageFile] = []
        self.texture = ''
        self.water_dir = ''

    def parse(self) -> bool:
        d = self.data
        if len(d) < 24 or d[0:4] not in self.SIGNATURES:
            return False
        aqua = d[0:4] == b'LND\x1B'
        # LND\x1A covers Worms 2, Armageddon, World Party and Online Worms,
        # and the four do not agree on the header: Worms 2 and Online Worms
        # carry no water height and end with a word before the images and a
        # fourth image after them, where Armageddon and World Party carry the
        # water height and neither of those. The signature cannot tell them
        # apart, so both shapes are tried and the one that consumes the file
        # exactly is taken -- the same test the return already applies.
        layouts = ((True, False, 3),) if aqua else (
            (True, False, 3), (False, True, 4))
        for has_water, trailing_word, image_count in layouts:
            self.water = 0
            self.objects = []
            self.images = []
            if self._read(d, has_water, trailing_word, image_count):
                return True
        return False

    def _read(self, d: bytes, has_water: bool,
              trailing_word: bool, image_count: int) -> bool:
        try:
            o = 8
            self.width, self.height, self.border = struct.unpack('<3I', d[o:o + 12])
            o += 12
            if has_water:
                # Water height, then a word whose meaning is not known -- the
                # two travel together in Armageddon, World Party and Aqua.
                self.water = struct.unpack('<I', d[o:o + 4])[0]
                o += 8
            count = struct.unpack('<I', d[o:o + 4])[0]
            o += 4
            if count > 65535:
                return False
            self.objects = [struct.unpack('<ii', d[o + k * 8:o + k * 8 + 8])
                            for k in range(count)]
            o += count * 8
            if trailing_word:
                o += 4                      # Worms 2 / Online Worms only
            for _ in range(image_count):
                if d[o:o + 4] != ImageFile.SIGNATURE:
                    return False
                length = struct.unpack('<I', d[o + 4:o + 8])[0]
                image = ImageFile(d[o:o + length], aligned=False)
                if not image.parse():
                    return False
                self.images.append(image)
                o += length
            names = []
            for _ in range(2):
                n = d[o]
                names.append(d[o + 1:o + 1 + n].decode('latin-1', 'replace'))
                o += 1 + n
            self.texture, self.water_dir = names
        except (struct.error, IndexError):
            return False
        return o == len(d)

    @property
    def foreground(self) -> Optional[ImageFile]:
        return self.images[0] if self.images else None


def _mission_land(data: bytes) -> Optional[bytes]:
    """Pull the land data out of a World Party Aqua mission archive.

    A mission is a chunked container -- a four-character tag then a u32
    length, nested one level -- holding VERS, GUID, INST, WAM (the scheme)
    and IMG, whose payload is the land.dat.
    """
    if len(data) < 8:
        return None
    end = 8 + struct.unpack('<I', data[4:8])[0]
    i = 8
    while i + 8 <= min(end, len(data)):
        tag = data[i:i + 4]
        length = struct.unpack('<I', data[i + 4:i + 8])[0]
        if tag.strip() == b'IMG':
            return data[i + 8:i + 8 + length]
        i += 8 + length
    return None


class DirectoryReader:
    """Reads Worms Armageddon .dir files"""

    SIGNATURE = b'DIR\x1A'
    TOC_SIGNATURE = 0x0000000A
    HASH_SIZE = 1024

    def __init__(self, filepath: str):
        self.filepath = filepath
        self.file_size = 0
        self.toc_offset = 0
        self.files: Dict[str, Tuple[int, int]] = {}  # name -> (offset, length)

    def read(self) -> bool:
        """Read and parse the .dir file structure"""
        try:
            with open(self.filepath, 'rb') as f:
                # Read header
                signature = f.read(4)
                if signature != self.SIGNATURE:
                    print(f"Error: Invalid DIR signature: {signature}")
                    return False

                self.file_size = struct.unpack('<I', f.read(4))[0]
                self.toc_offset = struct.unpack('<I', f.read(4))[0]

                # Read TOC
                f.seek(self.toc_offset)

                toc_sig = struct.unpack('<I', f.read(4))[0]
                if toc_sig != self.TOC_SIGNATURE:
                    print(f"Warning: Unexpected TOC signature: {hex(toc_sig)}")

                # Read hash table
                hash_table = []
                for _ in range(self.HASH_SIZE):
                    offset = struct.unpack('<I', f.read(4))[0]
                    hash_table.append(offset)

                # Read file entries
                for hash_offset in hash_table:
                    if hash_offset == 0:
                        continue

                    # Follow the chain for this hash bucket
                    current_offset = hash_offset
                    while current_offset != 0:
                        f.seek(self.toc_offset + current_offset)

                        next_offset = struct.unpack('<I', f.read(4))[0]
                        file_offset = struct.unpack('<I', f.read(4))[0]
                        file_length = struct.unpack('<I', f.read(4))[0]

                        # Read filename (padded to 4-byte boundary)
                        filename_start = f.tell()
                        filename_bytes = b''
                        while True:
                            byte = f.read(1)
                            if not byte or byte[0] == 0:
                                break
                            filename_bytes += byte

                        filename = filename_bytes.decode('ascii', errors='replace')

                        self.files[filename] = (file_offset, file_length)
                        current_offset = next_offset

                return True

        except Exception as e:
            print(f"Error reading DIR file: {e}")
            return False

    def extract_file(self, f: BinaryIO, filename: str) -> Optional[bytes]:
        """Extract raw file data from the DIR"""
        if filename not in self.files:
            return None

        offset, length = self.files[filename]
        f.seek(offset)
        return f.read(length)

    def extract_all(self, output_dir: str) -> int:
        """Extract all files from the DIR"""
        os.makedirs(output_dir, exist_ok=True)
        count = 0

        try:
            with open(self.filepath, 'rb') as f:
                for filename, (offset, length) in self.files.items():
                    data = self.extract_file(f, filename)
                    if data:
                        output_path = os.path.join(output_dir, filename)
                        os.makedirs(os.path.dirname(output_path), exist_ok=True)

                        with open(output_path, 'wb') as out:
                            out.write(data)

                        count += 1
                        print(f"Extracted: {filename} ({len(data)} bytes)")

        except Exception as e:
            print(f"Error extracting files: {e}")

        return count

    def list_files(self) -> None:
        """List all files in the DIR"""
        print(f"Files in {self.filepath}:")
        print(f"{'Filename':<50} {'Offset':<10} {'Size':<10}")
        print("-" * 70)

        total_size = 0
        for filename in sorted(self.files.keys()):
            offset, length = self.files[filename]
            print(f"{filename:<50} {offset:<10} {length:<10}")
            total_size += length

        print(f"\nTotal: {len(self.files)} files, {total_size} bytes")


def read_bmp(data: bytes) -> Tuple[int, int, bytes, bytes]:
    """Read an 8-bit indexed BMP.

    Returns (width, height, pixels top-down, palette as RGB triples).

    spriteEditor writes the old 12-byte BITMAPCOREHEADER form with a 3-byte
    palette, so both that and the 40-byte BITMAPINFOHEADER form are accepted.
    """
    if len(data) < 26 or data[:2] != b'BM':
        raise ValueError('not a BMP')
    pixel_offset = struct.unpack('<I', data[10:14])[0]
    dib = struct.unpack('<I', data[14:18])[0]
    if dib == 12:
        width, height = struct.unpack('<HH', data[18:22])
        bpp = struct.unpack('<H', data[24:26])[0]
        pal_entry = 3
        top_down = False
    else:
        width, height = struct.unpack('<ii', data[18:26])
        bpp = struct.unpack('<H', data[28:30])[0]
        pal_entry = 4
        top_down = height < 0
        height = abs(height)
    if bpp != 8:
        raise ValueError(f'expected an 8-bit BMP, got {bpp}-bit')

    pal_start = 14 + dib
    ncol = (pixel_offset - pal_start) // pal_entry
    palette = bytearray()
    for i in range(ncol):
        o = pal_start + i * pal_entry
        b, g, r = data[o], data[o + 1], data[o + 2]
        palette += bytes((r, g, b))

    stride = (width + 3) // 4 * 4
    rows = []
    for y in range(height):
        start = pixel_offset + y * stride
        rows.append(data[start:start + width])
    if not top_down:
        rows.reverse()          # BMP scanlines run bottom-up
    return width, height, b''.join(rows), bytes(palette)


# --------------------------------------------------------------------- png --
# Authoring in PNG means not having to index the art by hand, but the game
# takes indexed pictures with one transparent colour, so a PNG has to be
# reduced on the way in.

PNG_ALPHA_THRESHOLD = 128       # alpha at or above this is drawn, below is not
# The compressor's literal command carries 0-127, so an index of 128 or more
# can only be written as part of a match and a picture that needs one may be
# unencodable. The largest palette in a stock install is 96, so this is well
# clear of anything the game itself ships.
MAX_DRAWN_COLOURS = 127

# The engine aggregates every picture's palette into one table for the terrain.
# From the guide: "The game allows for a palette of up to 112 unique colours to
# be shared by all of your terrain objects. Your terrain won't load if it goes
# beyond 112 colours." The median across a stock install is exactly 112.
MAX_SHARED_COLOURS = 112


def png_colour_counts(data: bytes,
                      alpha_threshold: int = PNG_ALPHA_THRESHOLD
                      ) -> Dict[Tuple[int, int, int], int]:
    """How often each drawn colour occurs in a PNG."""
    from io import BytesIO
    raw = Image.open(BytesIO(data)).convert('RGBA').tobytes()
    counts: Dict[Tuple[int, int, int], int] = {}
    for o in range(0, len(raw), 4):
        if raw[o + 3] >= alpha_threshold:
            key = (raw[o], raw[o + 1], raw[o + 2])
            counts[key] = counts.get(key, 0) + 1
    return counts


def cut_palette(counts: Dict[Tuple[int, int, int], int],
                max_colours: int) -> List[Tuple[int, int, int]]:
    """Reduce a set of weighted colours to at most `max_colours` by median cut."""
    if len(counts) <= max_colours:
        return sorted(counts)
    total = sum(counts.values())
    strip = Image.new('RGB', (total, 1))
    strip.putdata([c for c, n in counts.items() for _ in range(n)])
    cut = strip.quantize(colors=max_colours, method=Image.Quantize.MEDIANCUT)
    flat = cut.getpalette()[:max_colours * 3]
    return sorted({tuple(flat[i * 3:i * 3 + 3]) for i in range(len(flat) // 3)})


def _map_to_palette(counts: Dict[Tuple[int, int, int], int],
                    chosen: List[Tuple[int, int, int]]
                    ) -> Tuple[Dict[Tuple[int, int, int], int], float]:
    """Nearest palette entry for each colour, and the total distance moved.

    Exact, and Pillow's quantize(palette=) is not: it builds an approximate
    lookup and on a photograph lands some 70% further from the original, which
    is poor value in the one step whose purpose is to move colours as little
    as possible.

    Comparing every colour against every entry is what made this slow -- a
    photograph brings a few hundred thousand colours and the palette holds a
    hundred-odd. Entries are bucketed on a coarse grid instead, and a colour
    is compared only against buckets near it, widening the ring until the
    nearest entry found is closer than the ring itself can reach.
    """
    distinct = list(counts)
    if np is not None and len(distinct) > 256:
        # Every colour against every entry at once. A photograph brings tens
        # of thousands of colours and the same work in Python takes several
        # seconds a picture.
        cols = np.array(distinct, dtype=np.int32)
        pal = np.array(chosen, dtype=np.int32)
        gaps = ((cols[:, None, :] - pal[None, :, :]) ** 2).sum(2)
        nearest = gaps.argmin(1)
        best_d = gaps[np.arange(len(cols)), nearest]
        weights = np.fromiter((counts[c] for c in distinct),
                              dtype=np.int64, count=len(distinct))
        error_sum = float((weights * np.sqrt(best_d)).sum())
        index_of = {c: int(i) + 1            # index 0 is the transparent one
                    for c, i in zip(distinct, nearest)}
        return index_of, error_sum

    index_of: Dict[Tuple[int, int, int], int] = {}
    error_sum = 0.0
    for colour, n in counts.items():
        r, g, b = colour
        best = -1
        best_d = 1 << 30
        for i, (pr, pg, pb) in enumerate(chosen):
            d = (pr - r) ** 2 + (pg - g) ** 2 + (pb - b) ** 2
            if d < best_d:
                best_d = d
                best = i
                if not d:
                    break
        index_of[colour] = best + 1         # index 0 is the transparent one
        error_sum += n * (best_d ** 0.5)
    return index_of, error_sum


def read_png(data: bytes, max_colours: int = MAX_DRAWN_COLOURS,
             alpha_threshold: int = PNG_ALPHA_THRESHOLD,
             palette: Optional[List[Tuple[int, int, int]]] = None
             ) -> Tuple[int, int, bytes, bytes, List[str]]:
    """Read a PNG as indexed pixels, the way read_bmp returns a BMP.

    Alpha is thresholded rather than kept: the game has one transparent colour,
    not an alpha channel, so a pixel is either drawn or it is not. Everything
    below the threshold becomes index 0.

    The drawn colours are reduced to `max_colours` if there are more, and the
    returned notes say how far that moved them.
    """
    from io import BytesIO
    src = Image.open(BytesIO(data)).convert('RGBA')
    width, height = src.size
    notes: List[str] = []
    raw = src.tobytes()                     # RGBA, row-major, top-down

    npix = width * height
    drawn = bytearray(npix)
    counts: Dict[Tuple[int, int, int], int] = {}
    for i in range(npix):
        o = i * 4
        if raw[o + 3] >= alpha_threshold:
            drawn[i] = 1
            key = (raw[o], raw[o + 1], raw[o + 2])
            counts[key] = counts.get(key, 0) + 1
    ndrawn = sum(counts.values())
    if not ndrawn:
        # Legal, and shipped: Coral Reef's soil.img is entirely transparent.
        # An empty palette is what the game's own encoder writes for it.
        notes.append('every pixel is transparent; the picture will be empty')
        return width, height, bytes(npix), b'\x00\x00\x00', notes

    if palette is not None:
        # One palette cut across the whole terrain, so the archive as a whole
        # stays inside the budget rather than each picture separately.
        chosen = palette
    elif len(counts) <= max_colours:
        chosen = sorted(counts)
    else:
        # Median cut over the drawn pixels only, so a fully transparent area --
        # often some colour that appears nowhere else -- neither spends a
        # palette entry nor pulls the cut about. Each colour is presented as
        # often as it occurs so the cut weighs them properly.
        chosen = cut_palette(counts, max_colours)
        notes.append(f'{len(counts)} colours reduced to {len(chosen)}')

    # Nearest entry for every distinct colour. Pillow does this in C against a
    # palette image, which matters: searching in Python is a scan of the whole
    # palette per colour, and a photograph brings hundreds of thousands of
    # them.
    index_of, error_sum = _map_to_palette(counts, chosen)

    pixels = bytearray(npix)
    for i in range(npix):
        if drawn[i]:
            o = i * 4
            pixels[i] = index_of[(raw[o], raw[o + 1], raw[o + 2])]

    mean_error = error_sum / ndrawn
    if mean_error:
        # 441 is the longest distance in the RGB cube, sqrt(3 * 255**2).
        notes.append(f'mean colour shift {mean_error:.1f} of 441 '
                     f'({100 * mean_error / 441:.1f}%)')
    if npix - ndrawn:
        notes.append(f'{npix - ndrawn} pixel(s) under alpha '
                     f'{alpha_threshold} made transparent')

    # Index 0 is the transparent colour and is never drawn; the guide has every
    # image agree on it and the stock terrains use black.
    palette = bytearray(b'\x00\x00\x00')
    for r, g, b in chosen:
        palette += bytes((r, g, b))
    return width, height, bytes(pixels), bytes(palette), notes


def plan_shared_palette(sources: Dict[str, str],
                        budget: int = MAX_SHARED_COLOURS,
                        borrowed: Sequence[str] = ()
                        ) -> Tuple[Optional[List[Tuple[int, int, int]]], List[str]]:
    """Cut one palette for every PNG in a terrain.

    The engine aggregates the palettes of all a terrain's pictures into one
    table and the guide caps it: "The game allows for a palette of up to 112
    unique colours to be shared by all of your terrain objects. Your terrain
    won't load if it goes beyond 112 colours." Reducing each picture on its own
    would not honour that -- thirty-two pictures of a hundred colours each
    still come to far more than a hundred colours together.

    Everything the terrain draws counts: the objects and land textures, and
    the background sprites too. The guide's budget is written around exactly
    those -- "all background elements (sky gradient, debris, and background
    layer)" share it with the foreground -- and back.spr and debris.spr are
    core files, so an images-only budget would leave them unaccounted.

    A gfx0 or gfx1 sprite does not count. Those replace what the game would
    otherwise take from Gfx.dir, so they are not the terrain's own art.
    Across a stock install the median under this rule is exactly the 112 the
    guide gives; counting the overrides puts terrains over that their art is
    comfortably inside.

    An already-indexed source is left alone, since its colours were chosen
    deliberately, but they are counted against the budget so what remains is
    what the PNGs may spend. Returns None when there is nothing to cut.

    Art named in `borrowed` -- the tool's own defaults, standing in for a
    piece the author has not made -- spends none of the budget. The whole of
    it goes to the author's own work, and the defaults are mapped onto
    whatever palette that produces however badly they come out. A default
    bridge in the wrong colours is a prompt to draw one; an author's texture
    reduced to make room for it is a loss.
    """
    notes: List[str] = []
    fixed: set = set()
    counts: Dict[Tuple[int, int, int], int] = {}
    png_names: List[str] = []
    lent = {b.lower() for b in borrowed}

    for name, path in sorted(sources.items()):
        with open(path, 'rb') as fh:
            blob = fh.read()
        if name.lower() in lent:
            # Mapped onto the palette later, but never consulted in cutting it.
            png_names.append(name)
            continue
        if path.lower().endswith('.png'):
            png_names.append(name)
            for colour, n in png_colour_counts(blob).items():
                counts[colour] = counts.get(colour, 0) + n
        else:
            try:
                _w, _h, pixels, pal = read_bmp(blob)
            except ValueError:
                continue
            for v in set(pixels):
                if v:
                    fixed.add(tuple(pal[v * 3:v * 3 + 3]))

    if not png_names:
        return None, notes

    # A colour a fixed source already spends is free for the PNGs to reuse.
    spare = {c: n for c, n in counts.items() if c not in fixed}
    room = budget - len(fixed)
    if room <= 0:
        # Nothing can be cut back far enough: the already-indexed pictures
        # alone are over budget, and re-quantising those would change art the
        # author indexed deliberately. Say so plainly rather than pretend the
        # cut achieved anything.
        notes.append(f'the indexed sources alone use {len(fixed)} colours, '
                     f'past the {budget} the terrain may hold; reduce those '
                     f'and the PNGs cannot make up for it. Cutting the PNGs '
                     f'to 1 colour each so the total grows no further')
        room = 1
    lent_note = (f', and {len(lent)} default(s) fitted to it afterwards'
                 if lent else '')
    if len(spare) <= room:
        chosen = sorted(fixed | set(spare))
        notes.append(f'{len(png_names) - len(lent)} PNG(s) share '
                     f'{len(chosen)} colours with the rest of the terrain, '
                     f'inside the {budget} budget; nothing reduced{lent_note}')
        return chosen, notes

    cut = cut_palette(spare, room)
    chosen = sorted(fixed | set(cut))
    total = len(fixed) + len(spare)
    if len(chosen) <= budget:
        notes.append(f'{total} colours across {len(png_names) - len(lent)} '
                     f'PNG(s) and the indexed sources cut to {len(chosen)}, '
                     f'inside the {budget} the terrain may hold{lent_note}')
    else:
        notes.append(f'{total} colours cut to {len(chosen)}, still past the '
                     f'{budget} the terrain may hold: {len(fixed)} of them '
                     f'come from indexed sources, which are packed as authored')
    return chosen, notes


def build_palette(pixels: bytes, source_palette: bytes) -> Tuple[bytes, bytes]:
    """Compact a picture onto its own palette.

    A source BMP indexes a shared 256-colour table, but a .spr or .img stores
    only the colours it actually uses, renumbered from 1. Colour 0 is the
    transparent background and is never stored (see SPR_FORMAT.md).

    Returns the packed RGB palette and the pixels remapped onto it.
    """
    # Order by first appearance scanning the pixels top-down. This reproduces
    # spriteEditor's palettes exactly; ordering by source index instead yields
    # the right colours in the wrong order.
    used: List[int] = []
    seen = bytearray(256)
    for value in pixels:
        if value and not seen[value]:
            seen[value] = 1
            used.append(value)
    if len(used) > 255:
        raise ValueError(f'picture uses {len(used)} colours; the format allows 255')
    palette = bytearray()
    table = bytearray(256)
    for new, old in enumerate(used, start=1):
        table[old] = new
        palette += source_palette[old * 3:old * 3 + 3]
    return bytes(palette), pixels.translate(bytes(table))


def encode_sprite(width: int, height: int, frames: int, pixels: bytes,
                  palette: bytes, flags: int, framerate: int,
                  compress: bool = True) -> bytes:
    """Build a compressed .spr from a top-down sheet of `frames` stacked cells.

    Frames are cropped to their ink and packed into streams; which stream-table
    layout is written follows from the palette length, as SpriteFile describes.
    """
    cell = width * height
    if len(pixels) != cell * frames:
        raise ValueError(f'expected {cell * frames} pixels, got {len(pixels)}')

    # Crop each frame to the pixels that are actually set. Index 0 is the
    # transparent background and is excluded from the box, exactly as the
    # game's own tools do.
    boxes: List[Tuple[int, int, int, int]] = []
    crops: List[bytes] = []
    for i in range(frames):
        frame = pixels[i * cell:(i + 1) * cell]
        left, top, right, bottom = width, height, 0, 0
        for y in range(height):
            row = frame[y * width:(y + 1) * width]
            for x in range(width):
                if row[x]:
                    if x < left:
                        left = x
                    if x >= right:
                        right = x + 1
                    if y < top:
                        top = y
                    if y >= bottom:
                        bottom = y + 1
        if right <= left or bottom <= top:
            # A frame with no ink is recorded as zero-area and carries no
            # pixels, which is what the game's own files do: -Beach's
            # debris.spr has six blank frames among its 128 and each is
            # w=0 h=0 at the origin, sharing the position of the frame after.
            left = top = right = bottom = 0
        crop = bytearray()
        for y in range(top, bottom):
            crop += frame[y * width + left:y * width + right]
        crops.append(bytes(crop))
        boxes.append((left, top, right, bottom))

    # A frame may not START beyond MAX_DATA_POS within its stream, so split
    # the frames into streams on that bound. This bounds the offset, not the
    # stream: a stream holding a single oversized frame may be far larger
    # (414,720 bytes is the largest in the reference archive). Following the
    # rule reproduces the reference's stream counts exactly.
    streams: List[bytearray] = [bytearray()]
    frames_in_stream = [0]
    placement: List[Tuple[int, int]] = []          # (stream index, offset)
    for crop in crops:
        # Start a new stream when appending would push this one past the
        # bound. A stream may only exceed it while holding a single frame,
        # which is how the game's own huge backdrops are stored.
        if frames_in_stream[-1] and len(streams[-1]) + len(crop) > MAX_DATA_POS:
            streams.append(bytearray())
            frames_in_stream.append(0)
        placement.append((len(streams) - 1, len(streams[-1])))
        streams[-1] += crop
        frames_in_stream[-1] += 1

    if compress and len(streams) > MAX_STREAMS:
        # Dense frames are what does it: each one overflows MAX_DATA_POS on
        # its own and so takes a whole stream, and past MAX_STREAMS the game
        # cannot index them. The levers are fewer frames, a smaller cell, or
        # less ink per frame -- all of which cut the stream count.
        raise ValueError(
            f'this sprite needs {len(streams)} streams, over the '
            f'{MAX_STREAMS} the game can load; use fewer frames, a smaller '
            f'cell, or sparser frames')

    ncol = len(palette) // 3
    out = bytearray()
    out += SpriteFile.SIGNATURE
    out += struct.pack('<I', 0)                    # file length, filled below
    out += struct.pack('<H', 0x8008 | (SpriteFile.COMPRESSED_FLAG if compress else 0))
    out += struct.pack('<H', ncol)
    out += palette

    body = bytearray()
    if compress:
        encoded = [Team17Compressor.compress(bytes(b)) for b in streams]
        body += struct.pack('<I', len(streams))
        body += b'\x00' * (ncol % 4)               # align the stream table
        # Two record layouts, selected by ncol % 4 alone -- see SpriteFile.
        positional = ncol % 4 in (0, 1)
        pos = 0
        for k, (raw, enc) in enumerate(zip(streams, encoded)):
            nxt = pos + len(enc)
            last = k == len(streams) - 1
            if positional:
                # (position of THIS stream, unused, decompressed length)
                body += struct.pack('<III', pos, 0, len(raw))
            elif last:
                # (unused, decompressed length, position where the NEXT
                # stream begins). The reader takes stream k's position from
                # record k-1, with stream 0 at 0, so the last stream has no
                # next position and the field is simply not written -- the
                # table ends two bytes short of a whole record.
                body += struct.pack('<IIH', 0, len(raw), 0)
            else:
                body += struct.pack('<III', 0, len(raw), nxt)
            pos = nxt
        stream = b''.join(encoded)
    else:
        # Uncompressed: no stream count and no stream table. The palette is
        # followed by a two-byte gap, the sprite record, the frame table
        # padded by ncol % 4, then every frame's pixels laid out end to end.
        #
        # Needed because the game insists on it for two files: back.spr is
        # stored uncompressed in 109 of 114 stock themes and debris.spr in
        # 108 of 125. Handing it a compressed one sends its LZ77 loop off the
        # end of the buffer and the game dies with an access violation.
        body += b'\x00\x00'
        body += struct.pack('<HHHHH', framerate, flags, width, height, frames)
        body += b'\x00' * (ncol % 4)
        # Frames sit end to end and each entry records where its own starts.
        # The offset is 32 bits split across the two leading 16-bit fields,
        # low word first -- what reads like a "stream number" beside a
        # position is really the high half. Graveyard's debris.spr shows it
        # plainly: frame 58 at (64944, 0) is 1500 bytes, and frame 59 follows
        # at (908, 1), which is 66444.
        pos = 0
        for crop, (left, top, right, bottom) in zip(crops, boxes):
            body += struct.pack('<HHHHHH', pos & 0xffff, pos >> 16,
                                left, top, right, bottom)
            pos += len(crop)
        body += b''.join(crops)
        out += body
        struct.pack_into('<I', out, 4, len(out))
        return bytes(out)

    # Pad so the record lands at an offset congruent to 2 mod 4; the other
    # layout gets there by stopping two bytes early instead. The record leads
    # with the frame rate, so it must come from the .spd -- an animated sprite
    # left at 0 divides by zero in the game's animation code.
    if positional:
        body += b'\x00\x00'
    body += struct.pack('<HHHHH', framerate, flags, width, height, frames)
    for (left, top, right, bottom), (sidx, off) in zip(boxes, placement):
        # data_pos, then the stream number shifted left by 8.
        body += struct.pack('<HHHHHH', off, sidx * 256,
                            left, top, right, bottom)
    body += stream

    out += body
    struct.pack_into('<I', out, 4, len(out))
    return bytes(out)


# ------------------------------------------------------------------- icons --
# A terrain's icon is the loose TEXT.img beside its Level.dir. It is not stored
# in an archive, so the game cannot derive where the pixels start from an entry
# length -- it has to compute the offset, and the shipped icons show what it
# expects. Across the 129 icons in a stock install:
#
#   compressed    ncol is 8 or 16 -- always a multiple of 4, so the header
#                 lands on a 4-byte boundary and nothing is padded
#   uncompressed  ncol is anything (8, 10, 13, 14, 15, 16, 17, 20 all occur)
#
# Writing a compressed icon whose palette is not a multiple of 4 puts padding
# before the pixels, a form no shipped icon uses, and the game crashes on it --
# a 17-colour one took down the land generator screen.

ICON_DIM = 64
ICON_NAME = 'TEXT.img'          # the icon beside Level.dir
TEXTURE_NAME = 'text.img'       # the land texture packed inside it
TEXTURE_DIM = 256

# ---------------------------------------------------------------- the guide --
# Limits from the terrain creation guide (docs/Guide.MD). The ones the guide
# states outright and no shipped terrain breaks are refused; the rest are
# reported and built anyway, because the shipped terrains do break them.
MAX_OBJECTS = 32                # "Maximum of 32 objects"; 143 terrains obey it
# What wkTerrainSync will hand to another player. Past this it refuses the
# transfer outright -- "specified file size exceeds allowed limit" -- and the
# terrain works at home but cannot be shared, which is usually not what an
# author wants to find out from someone else.
MAX_SYNC_BYTES = 10 * 1024 * 1024
SOIL_DIM = (256, 256)           # MAX_SHARED_COLOURS is up with the PNG reader
GRADIENT_DIM = (8, 916)
GRASS_WIDTH = 136               # height varies
BRIDGE_WIDTH = 64               # height varies
CORE_IMG_DIMS = {
    'text.img': (TEXTURE_DIM, TEXTURE_DIM),
    'soil.img': SOIL_DIM,
    'gradient.img': GRADIENT_DIM,
}
# Of those three, the two the game will not survive being wrong about. The
# land is tiled from text.img and dug out of soil.img at a fixed 256x256, and
# a 1254x1254 texture crashed it outright. All 143 shipped terrains hold both
# at that size, where gradient.img really does vary -- 137 are 8x916 and 6 are
# 8x900 -- so that one stays advice.
CORE_IMG_DIMS_REQUIRED = ('text.img', 'soil.img')
CORE_IMG_WIDTHS = {
    'grass.img': GRASS_WIDTH,
    'bridge.img': BRIDGE_WIDTH,
    'bridge-l.img': BRIDGE_WIDTH,
    'bridge-r.img': BRIDGE_WIDTH,
}

# The .inf beside each object. The guide gives this example, and the shipped
# terrains bear it out: collision is 1 in 3263 of 3264 objects, in-front 0 in
# 97%, nothing-stacks-on-me 1 in 95%, floor placement in 71%.
DEFAULT_INF = (5, 0, 0, 1, 1, 3)
INF_FIELDS = (
    ('weight', 1, 10, 'how often the object is placed, relative to the others'),
    ('in front', 0, 1, '0 = behind the terrain, 1 = in front'),
    ('soil', 0, 1, '0 = no soil when destroyed, 1 = soil'),
    ('collision', 0, 1, '1 = enabled'),
    ('no stacking', 0, 1, '0 = other objects may be placed on this, 1 = not'),
    ('location', 0, 3, '0/1 = side (left/right), 2 = ceiling, 3 = floor'),
)

# One file for the whole terrain instead of a .inf beside every object. The
# keys are in the order the .inf stores them, and each holds the number the
# format holds -- collide = 1 enables collision, as the guide has it, rather
# than reading as its own opposite.
PALETTE_NAME = 'palette.png'
SETTINGS_NAME = 'object_settings.txt'
SETTINGS_KEYS = ('probability', 'front', 'soil', 'collide', 'nostack', 'where')
# Written at the top of the file, so what each setting does is where it is
# needed rather than in a document somewhere else. Wording follows the guide.
SETTINGS_HEADER = """\
// How each object is placed. One block an object: its picture's name, then
// its settings. Blank lines are ignored and these // lines are comments.
// The order of the blocks means nothing -- the terrain is packed
// alphabetically whatever order they are written in.
//
// probability  1 to 10. Affects the chance of an object being placed, and is
//              relative to the values of other objects. Smaller objects, or
//              ones with a narrow base, typically have more places to appear.
// front        Whether the object is in front or behind the terrain.
//              0 = behind, 1 = in front
// soil         Whether the soil texture appears when the object is
//              destroyed. 0 = none, 1 = soil
// collide      Enables or disables collision. 1 = enabled, 0 = disabled
// nostack      Whether other objects can be placed onto this one.
//              0 = yes, 1 = no
// where        Where the object is placed. 2 = ceiling, 3 = floor, 0 or 1 =
//              the side of the terrain, saying which side of the object is
//              fixed to it, left (0) or right (1)

"""


def parse_inf(text: str) -> Optional[List[int]]:
    """Read a .inf as its list of integers, or None if it is not one.

    The extension is not the test: an object's parameters open with six small
    integers, so anything that reads as those is one and anything else --
    notes, a stray index.txt -- is not. 310 shipped objects carry two further
    fields, the first of them a filename, so only the six are required to be
    numbers and anything after them is ignored.
    """
    values: List[int] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            values.append(int(line))
        except ValueError:
            break
        if len(values) == len(INF_FIELDS):
            break
    if len(values) < len(INF_FIELDS):
        return None
    return values


def inf_problems(values: List[int]) -> List[str]:
    """Values outside the ranges the guide documents, and settings to avoid."""
    out: List[str] = []
    for i, (label, lo, hi, meaning) in enumerate(INF_FIELDS):
        if not (lo <= values[i] <= hi):
            out.append(f'{label} is {values[i]}, the guide says {lo}-{hi} '
                       f'({meaning})')
    if values[3] == 0:
        # Legal, and exactly one shipped object uses it -- a ceiling weed in
        # CI5Water. Worms and weapons pass straight through, which reads as a
        # bug in play rather than a feature.
        out.append('collision is disabled; worms and shots pass through the '
                   'object, which plays badly. 1 of 3264 shipped objects does '
                   'this')
    return out


def parse_settings(text: str) -> Tuple[Dict[str, List[int]], List[str]]:
    """Read object_settings.txt into {picture name: six values}.

    A block is a filename followed by its `key = value` lines; blank lines are
    ignored and a new filename starts the next block. Keys may come in any
    order, and one left out takes the guide's default.
    """
    settings: Dict[str, List[int]] = {}
    problems: List[str] = []
    current: Optional[str] = None
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith('//') or line.startswith('#'):
            continue
        if '=' not in line:
            current = line
            settings[current] = list(DEFAULT_INF)
            continue
        if current is None:
            problems.append(f'line {n}: "{line}" before any object is named')
            continue
        key, _, value = line.partition('=')
        key = key.strip().lower()
        if key not in SETTINGS_KEYS:
            problems.append(f'line {n}: no setting called "{key.strip()}"')
            continue
        try:
            settings[current][SETTINGS_KEYS.index(key)] = int(value.strip())
        except ValueError:
            problems.append(f'line {n}: "{value.strip()}" is not a number')
    return settings, problems


def format_settings(entries: Sequence[Tuple[str, Sequence[int]]]) -> str:
    """Write object_settings.txt, a block an object, in the order given."""
    out: List[str] = [SETTINGS_HEADER.rstrip('\n'), '']
    for name, values in entries:
        out.append(name)
        out += [f'{k} = {v}' for k, v in zip(SETTINGS_KEYS, values)]
        out.append('')
    return '\n'.join(out)


def format_inf(values: Sequence[int]) -> bytes:
    """Write a .inf the way the shipped ones are written: one value a line."""
    return ''.join(f'{v}\r\n' for v in values).encode('latin-1')


# Every terrain in a stock install carries these eight; the rest are optional
# but equally fixed in name, so anything else with a .inf beside it is an
# object. Order here is the order they are written into the archive, which the
# game does not care about -- only the order of index.txt matters.
CORE_ENTRIES = (
    'text.img', 'soil.img', 'grass.img', 'gradient.img',
    'bridge.img', 'bridge-l.img', 'bridge-r.img',
    'back.spr', '_back.spr', 'back2.spr', 'front.spr', 'debris.spr',
)

# Subfolders that may hold sprite overrides -- worm animations, smoke, crates
# and so on, replacing what the game would take from Gfx.dir. Only gfx0 and
# gfx1 appear in a stock install (2360 entries across 10 terrains, every one a
# .spr); gfx is allowed as the obvious sibling.
SPRITE_SUBFOLDERS = ('gfx', 'gfx0', 'gfx1')

# Which fixed table each subfolder is painted with. Only gfx0 and gfx1 occur
# in a stock install -- 1796 entries and 17 -- and only those two have a known
# palette, so a bare gfx folder is packed as authored rather than fitted to a
# table guessed for it.

# The palettes those overrides are drawn with. A gfx0/gfx1 sprite carries a
# palette of its own, but the game does not paint with it -- it indexes into
# the fixed table the slot already holds, so a colour is whatever that table
# says, not what the sprite meant. Every one of Coral Reef's 446 gfx0
# overrides stays inside the 89 colours below; art that does not comes out
# recoloured, which is the fault these two tables exist to prevent.
#
# Index 0 is transparent and is not listed; entry 0 here is palette index 1.
# One of the 89 is a true black, so the run cannot be found by trimming
# blacks off the end -- it is 89 long by definition.
# Taken from Palette_gfx0_90cols.ACT and Palette_gfx1_90cols.ACT, which the
# repository does not ship; they are inlined so the tool needs no data file.

GFX0_PALETTE: Tuple[Tuple[int, int, int], ...] = (
    (242, 185,   6), (188, 144,   0), (242, 184,  60), (224,  96,   0),
    (238, 123,   6), (246, 157,   8), (118,  36,  10), (120,  91,  30),
    (164,  99,  28), (176, 141,  62), (140,   2,   2), (166,  47,  12),
    (186,  75,  36), (236,  81,  54), (244, 123, 114), (252, 128, 128),
    (246,   1,   0), (240,  48,  24), (  0,   0,   0), ( 70,  39,  24),
    ( 70,  50,  50), ( 86,  63,  62), ( 94,  73,  72), (106,  76,  74),
    (124,  89,  88), (150, 141, 132), (246, 230, 204), ( 52,  11,   2),
    (118,  64,  48), (144, 103, 102), (158, 115, 114), (176, 128, 126),
    (190, 144, 136), (218, 153, 140), (224, 166, 164), (222, 183, 168),
    (252, 217,   0), (252, 254,   6), (234, 224, 110), (242, 254, 116),
    (252, 254, 128), (242, 220,  66), ( 80, 152,  28), (106, 185,  68),
    (138, 221,  92), ( 60,  69,  14), ( 64,  96,  14), ( 76, 124,  28),
    (132, 132,  50), (174, 186,  98), (228, 224, 180), ( 28,  40,   6),
    (182, 184, 178), (126, 254, 126), (156, 157, 156), (252, 254, 252),
    (110, 188, 180), (118, 222, 222), ( 92,  97, 100), (116, 124, 124),
    (128, 254, 252), (  4,   5,  10), ( 80,  78, 108), (190, 198, 222),
    ( 70,  93, 186), ( 54,  55, 106), ( 42,  53, 158), ( 74,  83, 154),
    ( 84,  99, 154), (100, 105, 182), (120, 126, 174), (132, 136, 202),
    (164, 157, 224), (156, 157, 254), (176, 177, 228), (202, 196, 252),
    ( 22,  17, 194), (  0,   1, 220), (130, 107, 136), (238, 234, 240),
    (170, 143, 226), (130,  73, 134), (252, 128, 252), (208,  93,  94),
    ( 24,  19,  22), ( 44,  39,  42), (180, 147, 156), (220, 203, 208),
    (190, 100, 172),
)

GFX1_PALETTE: Tuple[Tuple[int, int, int], ...] = (
    (244, 184,   6), (158, 132,  18), (210, 152,   2), (252, 186,  62),
    (228,  83,   0), (230, 122,   4), (246, 231, 198), (102,  13,   0),
    (162,  70,  26), (146, 105,  14), (156, 124,  76), (210, 162, 100),
    (158,  39,  10), (208,  35,  26), (216,  76,  52), (232, 120, 100),
    (254, 127, 126), (186,   1,   0), (252,   0,   0), (252,  22,  14),
    (  0,   0,   0), ( 28,  10,   0), ( 54,  29,  28), ( 64,  44,  42),
    ( 80,  60,  58), ( 86,  72,  72), (106,  74,  74), (130, 126, 124),
    (214, 182, 164), (226, 197, 196), (250, 248, 242), (110,  71,  50),
    (136,  93,  88), (160, 115, 114), (178, 132, 128), (216, 160, 158),
    (224, 167, 166), (250, 251,  36), (252, 254,  64), (250, 218,   6),
    (108, 167,  22), (152, 175,  48), (204, 197,  80), (232, 225, 112),
    (242, 253, 114), ( 34,  44,   0), ( 48,  70,   6), ( 82, 108,   6),
    ( 98, 104,  78), (158, 164, 138), (  0, 190,   0), (  0, 208,   0),
    (  8, 110,   8), (156, 157, 156), (252, 254, 252), ( 88, 162, 136),
    ( 92, 116, 114), ( 68, 187, 190), ( 76, 241, 240), ( 78, 255, 252),
    (  0, 149, 252), ( 16,  16,  20), (172, 175, 208), (192, 195, 202),
    ( 60,  87, 174), ( 40,  49, 126), ( 58,  68, 116), ( 86,  88, 156),
    ( 72,  91, 192), ( 84, 107, 160), (104, 109, 166), (112, 116, 202),
    (158, 142, 218), (166, 157, 216), (160, 157, 250), (200, 195, 252),
    ( 28, 123, 202), ( 66, 150, 248), (  8,   7, 240), ( 70,   6, 102),
    (170, 107, 174), (210,   0, 208), (230,   0, 232), (118,  83,  84),
    (130, 101, 102), (162, 140, 154), (232, 226, 230), (212, 136, 140),
    (172,   4,  86),
)

GFX_PALETTES = {'gfx0': GFX0_PALETTE, 'gfx1': GFX1_PALETTE}

PICTURE_EXTS = ('.bmp', '.png')


def split_picture(name: str) -> Optional[Tuple[str, Optional[str]]]:
    """Split a picture's filename into (stem, 'img' | 'spr' | None).

    SpriteEditor derives an entry's name from the file's, so its inputs are
    spelled toaster.img.bmp. Nothing in the archive requires that -- an object
    is always an .img -- so a plain toaster.png is taken the same way and the
    infix is optional.
    """
    low = name.lower()
    for ext in PICTURE_EXTS:
        if not low.endswith(ext):
            continue
        stem = name[:-len(ext)]
        for kind in ('.img', '.spr'):
            if stem.lower().endswith(kind):
                return stem[:-4], kind[1:]
        return stem, None
    return None


# ----------------------------------------------------------- default art --
# Art shipped with the tool for the pieces a terrain cannot do without. The
# guide lists debris.spr as required, but a terrain packs and plays without one
# -- it is only blander for it -- so a missing debris is offered rather than
# insisted on. The bridge is different: the game draws it whenever a map is
# generated with bridges and will not load a terrain that has no bridge to
# draw, so building one of those is not worth doing.
DEFAULTS_DIR = 'presets'
DEFAULT_BRIDGE = ('bridge.img', 'bridge-l.img', 'bridge-r.img')
DEFAULT_DEBRIS = 'debris.spr'
# The icon, kept under its own name because it is not an archive entry: it
# goes beside Level.dir as TEXT.img. A terrain needs one, so it is offered
# like the bridge and refused like it when declined.
DEFAULT_ICON = 'icon.img'
# The land texture and the sky. Offered rather than insisted on: a terrain
# cannot go without either, but standing in for them is lending someone
# else's look, so it is a question rather than something done quietly.
DEFAULT_LOOK = ('text.img', 'gradient.img')

# Written into a build folder the first time it is packed, and looked for on
# every run after. Its presence is what makes lending art a first-run
# question: once an author has answered it, a piece that is missing later was
# deleted on purpose, and asking again would talk them back into art they
# already turned down.
SETTINGS_FILE = 'settings.spritetool'
# What the game will not open without. Everything else is offered once and
# never mentioned again.
REQUIRED_ASSETS = DEFAULT_LOOK + DEFAULT_BRIDGE + (DEFAULT_ICON,)
# Filled in without asking. Both are blank, so no look is being imposed and
# there is nothing to decide -- and the game does not treat them as optional:
# with no grass.img its ApplyGrassFringe reads the grass through a pointer
# that was never set and dies on the spot.
#
# back.spr is not among them. A terrain plays perfectly well without one --
# 24 in a stock install have none, Coral Reef among them -- and standing in
# with a blank would only give the game something to draw where it would
# otherwise draw the sky.
DEFAULT_SILENT = ('soil.img', 'grass.img')
# The parallax layers. All optional -- a terrain plays with none of them, and
# 24 stock ones ship no back.spr at all -- so each is offered once and taken
# or not. back.spr and _back.spr hold a single frame; back2 and front animate.
DEFAULT_LAYERS = ('back.spr', 'back2.spr', 'front.spr')
REPO_URL = 'https://github.com/EdLud/wa-spritetool'


def defaults_folder() -> Optional[str]:
    """Where the shipped art lives, or None when it is not there.

    Beside the script rather than beside the terrain: it belongs to the tool,
    and a copy run from anywhere should still find it.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, DEFAULTS_DIR)
    return path if os.path.isdir(path) else None


def default_sources(names: Sequence[str]) -> Dict[str, str]:
    """Map each wanted entry to a file in the defaults folder that can build it.

    Empty when the folder is missing, so a caller can tell "no defaults
    installed" from "defaults installed but missing this piece".
    """
    folder = defaults_folder()
    if folder is None:
        return {}
    found: Dict[str, str] = {}
    for name in names:
        for cand in (f'{name}.bmp', f'{name}.png',
                     f'{name[:-4]}.bmp', f'{name[:-4]}.png'):
            path = os.path.join(folder, cand)
            if os.path.exists(path):
                found[name] = path
                break
    return found


def ask(question: str, assume: Optional[bool] = None) -> bool:
    """Yes or no from the terminal. `assume` answers for a non-interactive run."""
    if assume is not None:
        print(f'{question} [{"y" if assume else "n"}, not asking]')
        return assume
    try:
        # Piped input counts: a terminal is not the only thing that answers.
        return input(f'{question} [y/N] ').strip().lower().startswith('y')
    except (EOFError, KeyboardInterrupt):
        # Nothing to read from, so take the safe answer and say so.
        print(f'{question} [n, nothing to read an answer from]')
        return False


def _copy_defaults(pieces: Dict[str, str], source_dir: str) -> List[str]:
    """Copy the shipped art for `pieces` into the terrain's own folder.

    Copied rather than read in place so that the next run picks the art up as
    the terrain's own and the author can edit it meanwhile. A sprite's .spd
    goes with it -- it carries the frame count, which the sheet does not.
    """
    import shutil
    copied: List[str] = []
    for name, path in sorted(pieces.items()):
        # The .spd is named for the ENTRY, not for the picture: the preset
        # for back.spr is back.png beside back.spr.spd. Deriving it from the
        # picture looks for back.spd, finds nothing, and the sprite is packed
        # without the frame count it cannot be built without.
        candidates = [(path, os.path.basename(path))]
        if name.lower().endswith('.spr'):
            spd = os.path.join(os.path.dirname(path), f'{name}.spd')
            candidates.append((spd, f'{name}.spd'))
        for src, as_name in candidates:
            if not os.path.exists(src):
                continue
            dest = os.path.join(source_dir, as_name)
            if os.path.exists(dest):
                continue
            # An author's picture is never written over, but a sprite of
            # theirs with no .spd beside it still cannot be built -- so the
            # metadata is copied even where the art was left alone.
            shutil.copyfile(src, dest)
            copied.append(as_name)
    return copied


def _settle_object_settings(folder: str, objects: Sequence[str],
                            assume: Optional[bool]
                            ) -> Tuple[Dict[str, List[int]], str]:
    """Bring a terrain's object settings together into one file.

    Returns the settings by object stem and a reason to stop when there is
    one. The per-object .inf and .txt files the format itself uses are still
    read and still written into the archive; this is only how the author keeps
    them, and one file for the terrain beats one beside every picture.
    """
    path = os.path.join(folder, SETTINGS_NAME)
    present = {f.lower(): f for f in os.listdir(folder)
               if os.path.isfile(os.path.join(folder, f))}

    def loose_for(stem: str) -> Optional[str]:
        """A per-object file covering `stem`, if one is there and parses."""
        for cand in (f'{stem}.inf', f'{stem}.txt'):
            real = present.get(cand)
            if real is None:
                continue
            with open(os.path.join(folder, real), 'r', encoding='latin-1') as fh:
                if parse_inf(fh.read()) is not None:
                    return real
        return None

    settings: Dict[str, List[int]] = {}
    if os.path.exists(path):
        with open(path, 'r', encoding='latin-1') as fh:
            by_picture, problems = parse_settings(fh.read())
        if problems:
            return {}, (f'{SETTINGS_NAME} does not read: '
                        + '; '.join(problems[:3]))
        # Keyed by picture name in the file, by stem here.
        for picture, values in by_picture.items():
            split = split_picture(picture)
            settings[(split[0] if split else picture).lower()] = values
        # A loose file for an object the settings already cover is two answers
        # to one question, and there is no telling which was meant.
        clash = [o for o in objects
                 if o.lower() in settings and loose_for(o.lower())]
        if clash:
            files = ', '.join(loose_for(o.lower()) for o in clash[:3])
            return {}, (f'{files} set what {SETTINGS_NAME} already sets for '
                        f'the same object(s). Delete one or the other -- there '
                        f'is no saying which you meant')

    missing = [o for o in objects if o.lower() not in settings]
    loose = {o: loose_for(o.lower()) for o in missing}
    to_take = {o: f for o, f in loose.items() if f}
    if to_take:
        shown = ', '.join(sorted(to_take.values())[:4])
        print(f'{len(to_take)} object(s) keep their settings in a file of '
              f'their own: {shown}'
              + (f' and {len(to_take) - 4} more' if len(to_take) > 4 else ''))
        if not ask(f'Move them into {SETTINGS_NAME} and delete them?', assume):
            return {}, (f'a terrain keeps its object settings in '
                        f'{SETTINGS_NAME}; nothing was changed')
        for stem, real in to_take.items():
            with open(os.path.join(folder, real), 'r', encoding='latin-1') as fh:
                settings[stem.lower()] = parse_inf(fh.read())
            os.remove(os.path.join(folder, real))
            print(f'  took {real} into {SETTINGS_NAME}', file=sys.stderr)

    for stem in objects:
        settings.setdefault(stem.lower(), list(DEFAULT_INF))

    # Alphabetical, whatever order the file was in: the archive is packed that
    # way, and the two agreeing is one less thing to wonder about.
    picture_of = {}
    for f in os.listdir(folder):
        split = split_picture(f)
        if split:
            picture_of.setdefault(split[0].lower(), f)
    rows = [(picture_of.get(s.lower(), s), settings[s.lower()])
            for s in sorted(objects, key=str.lower)]
    with open(path, 'w', encoding='latin-1', newline='\n') as fh:
        fh.write(format_settings(rows))
    return settings, ''


def _today() -> str:
    import datetime
    return datetime.date.today().isoformat()


def read_settings(folder: str) -> Optional[Dict[str, str]]:
    """A build folder's settings, or None when it has never been packed.

    None is the thing worth knowing: it means the folder is new, and that a
    missing piece has not been declined yet but simply never offered.
    """
    path = os.path.join(folder, SETTINGS_FILE)
    if not os.path.exists(path):
        return None
    out: Dict[str, str] = {}
    with open(path, 'r', encoding='latin-1') as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith('//') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            out[key.strip()] = value.strip()
    return out


def write_settings(folder: str, values: Dict[str, str]) -> None:
    """Mark the folder as packed once, and keep what was settled."""
    lines = ['// Written by spritetool the first time this folder was packed.',
             '// Its presence is what stops the defaults being offered again:',
             '// a piece missing now was deleted on purpose, not overlooked.',
             '// Delete this file to be asked about everything once more.',
             '']
    for key in sorted(values):
        lines.append(f'{key} = {values[key]}')
    with open(os.path.join(folder, SETTINGS_FILE), 'w',
              encoding='latin-1') as fh:
        fh.write('\n'.join(lines) + '\n')


def _has_icon(source_dir: str) -> bool:
    present = {n.lower() for n in os.listdir(source_dir)}
    return any(f'icon{s}' in present
               for s in ('.png', '.img.png', '.bmp', '.img.bmp'))


def _fill_from_defaults(names: List[str], source_dir: str,
                        assume: Optional[bool], first_run: bool
                        ) -> Tuple[List[str], Dict[str, str], str]:
    """Offer the shipped art for whatever the folder has not got.

    Only on a folder's first run, which `first_run` decides. After that a
    missing piece is the author's doing and the only question left is whether
    the game will open the result, so the required ones are checked and the
    optional ones are not mentioned.

    Returns the entry list, the sources keyed by entry name, and a reason to
    stop when there is one. Declining a required piece stops the build;
    declining an optional one carries on without it.
    """
    borrowed: Dict[str, str] = {}
    have = {n.lower() for n in names}
    where = os.path.basename(source_dir.rstrip(os.sep)) or source_dir

    def missing_note(what: str, pieces: Sequence[str]) -> str:
        return (f'no {what} in the folder and no {DEFAULTS_DIR} beside the '
                f'tool to take one from. Fetch that folder from {REPO_URL}, '
                f'or supply {", ".join(pieces)} yourself')

    def take(wanted: Sequence[str]) -> None:
        available = default_sources(wanted)
        for piece in wanted:
            if piece == DEFAULT_ICON:
                continue            # not an archive entry; copied below
            borrowed[piece] = available[piece]
            names.append(piece)
        for copied in _copy_defaults(
                {p: available[p] for p in wanted if p != DEFAULT_ICON},
                source_dir):
            print(f'  copied {copied} into {where}', file=sys.stderr)

    def lacking(piece: str) -> bool:
        if piece == DEFAULT_ICON:
            return not _has_icon(source_dir)
        return piece.lower() not in have

    if not first_run:
        # Nothing is offered. What remains is whether the game can open it:
        # the required pieces have to be there, however they got there.
        gone = [p for p in REQUIRED_ASSETS if lacking(p)]
        if gone:
            pretty = ', '.join(p.split('.')[0] for p in gone)
            return names, borrowed, (
                f'no {pretty}. A terrain needs it, and this folder has been '
                f'packed before, so nothing is offered -- put it back, or '
                f'delete {SETTINGS_FILE} to be asked again')
        return names, borrowed, ''

    # The blank ones, without asking: there is no look to choose between, and
    # a terrain without grass crashes the game outright.
    silent = [s for s in DEFAULT_SILENT if lacking(s)]
    if silent:
        stock = default_sources(silent)
        got = [s for s in silent if s in stock]
        if got:
            take(got)
        lost = [s for s in silent if s not in stock]
        if lost:
            return names, borrowed, missing_note(', '.join(lost), lost)

    # Everything else one at a time, so each is a decision of its own rather
    # than one answer standing for several pieces.
    offers = (
        (DEFAULT_LOOK[0], True,
         'the land texture, which every piece of ground is tiled from'),
        (DEFAULT_LOOK[1], True,
         'the sky behind the map'),
        (DEFAULT_BRIDGE, True,
         'the bridge, which the game draws whenever a map is generated '
         'with one'),
        (DEFAULT_ICON, True,
         'the icon shown for this terrain on the land generator screen'),
        (DEFAULT_DEBRIS, False,
         'the debris that falls through the sky'),
        (DEFAULT_LAYERS[0], False,
         'a still backdrop behind the map'),
        (DEFAULT_LAYERS[1], False,
         'an animated layer behind the map'),
        (DEFAULT_LAYERS[2], False,
         'an animated layer in front of the map'),
    )
    for piece, required, what in offers:
        pieces = piece if isinstance(piece, tuple) else (piece,)
        gone = [p for p in pieces if lacking(p)]
        if not gone:
            continue
        available = default_sources(pieces)
        if any(p not in available for p in pieces):
            if required:
                return names, borrowed, missing_note(
                    pieces[0].split('.')[0], pieces)
            print(f'  note: {missing_note(pieces[0].split(".")[0], pieces)}',
                  file=sys.stderr)
            continue
        stem = pieces[0].split('.')[0]
        need = 'needs' if required else 'can do without'
        print(f'No {stem}: {what}. A terrain {need} it.')
        if not ask(f'Use the default {stem} and write it to {where}?', assume):
            if required:
                return names, borrowed, f'a terrain needs {stem}'
            continue
        take(gone)
        if DEFAULT_ICON in gone:
            dest = os.path.join(source_dir, 'icon.img.png')
            if not os.path.exists(dest):
                import shutil
                shutil.copyfile(available[DEFAULT_ICON], dest)
                print(f'  copied icon.img.png into {where}', file=sys.stderr)
    return names, borrowed, ''


def _emit_sibling_icon(dir_file: str, out_dir: str) -> Optional[str]:
    """Write the icon sitting beside an archive as the source pack wants.

    A terrain's icon is not an archive entry: it is a loose text.img next to
    Level.dir, spelled in whatever casing its author used. Decompressing an
    archive would otherwise leave it behind, and packing the result would find
    no icon and offer a default in place of the terrain's own.

    Written as icon.img.bmp rather than under its own name, which cannot be
    used: a source folder holds the land texture as text.img, the same name
    but for case, and on a case-insensitive filesystem the two are one file.
    """
    folder = os.path.dirname(os.path.abspath(dir_file))
    if os.path.basename(dir_file).lower() != 'level.dir':
        return None                 # only a terrain keeps an icon beside it
    for entry in sorted(os.listdir(folder)):
        if entry.lower() != 'text.img':
            continue
        with open(os.path.join(folder, entry), 'rb') as fh:
            blob = fh.read()
        image = ImageFile(blob)
        if not image.parse():
            continue
        if (image.width, image.height) != (ICON_DIM, ICON_DIM):
            # The land texture is 256x256 and shares the name; size is what
            # tells them apart, not the spelling.
            continue
        bmp = SpriteFile._create_bmp(image.pixels, image.rgb_palette(),
                                     image.width, image.height)
        if bmp is None:
            return None
        dest = os.path.join(out_dir, 'icon.img.bmp')
        with open(dest, 'wb') as fh:
            fh.write(bmp)
        return os.path.basename(dest)
    return None


def read_palette_sheet(path: str) -> Tuple[List[Tuple[int, int, int]], str]:
    """Read a palette out of a picture: its colours, in the order first met.

    Any picture will do, not only a sheet this tool wrote -- an author with a
    palette they like can hand it over as it is. Transparent pixels are
    skipped, so the spare squares of a sheet's last row count for nothing.
    """
    if not os.path.exists(path):
        return [], f'no {path}'
    raw = Image.open(path).convert('RGBA').tobytes()
    seen: List[Tuple[int, int, int]] = []
    known: set = set()
    for o in range(0, len(raw), 4):
        if raw[o + 3] < PNG_ALPHA_THRESHOLD:
            continue
        colour = (raw[o], raw[o + 1], raw[o + 2])
        if colour not in known:
            known.add(colour)
            seen.append(colour)
    if not seen:
        return [], f'{os.path.basename(path)} has no opaque pixels'
    if len(seen) > MAX_SHARED_COLOURS:
        return seen[:MAX_SHARED_COLOURS], (
            f'{os.path.basename(path)} holds {len(seen)} colours; a terrain '
            f'may hold {MAX_SHARED_COLOURS}, so the first {MAX_SHARED_COLOURS} '
            f'were taken')
    return seen, ''


def _write_palette_sheet(entries: Dict[str, bytes], dest: str,
                         swatch: int = 16, across: int = 16) -> str:
    """Draw the terrain's colours as a grid of swatches.

    Taken from the archive rather than from the plan that made it: a terrain
    of already-indexed art has no plan, and what matters is the colours the
    game will read. Ordered as first met, walking the entries the way the
    engine aggregates them.

    The gfx0/gfx1 overrides are left out, as they are from the budget -- they
    replace what the game takes from Gfx.dir and are not the terrain's own.
    """
    seen: List[bytes] = []
    known: set = set()
    for name in sorted(entries):
        if '\\' in name or name.lower() == 'icon.img':
            continue
        blob = entries[name]
        if blob[:4] == ImageFile.SIGNATURE:
            picture = ImageFile(blob)
        elif blob[:4] == SpriteFile.SIGNATURE:
            picture = SpriteFile(blob)
        else:
            continue
        if not picture.parse():
            continue
        for i in range(picture.ncolours):
            colour = bytes(picture.palette[i * 3:i * 3 + 3])
            if colour not in known:
                known.add(colour)
                seen.append(colour)
    if not seen:
        return f'not writing {os.path.basename(dest)}: no colours found'

    # The last row rarely fills. Leave the spare squares transparent rather
    # than black, which a terrain may itself use and which would read as one
    # more colour than the terrain has.
    down = (len(seen) + across - 1) // across
    sheet = Image.new('RGBA', (across * swatch, down * swatch), (0, 0, 0, 0))
    pixels = sheet.load()
    for n, colour in enumerate(seen):
        cx, cy = (n % across) * swatch, (n // across) * swatch
        rgb = (colour[0], colour[1], colour[2], 255)
        for y in range(swatch):
            for x in range(swatch):
                pixels[cx + x, cy + y] = rgb
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    sheet.save(dest, 'PNG', optimize=True)
    return (f'{os.path.basename(dest)}: {len(seen)} colours of the '
            f'{MAX_SHARED_COLOURS} a terrain may hold')


def _write_terrain_folder(source_dir: str, out_dir: str,
                          compress_img: bool) -> List[str]:
    """Write the pieces of a terrain that live beside Level.dir, not in it.

    The icon and Water.dir are not archive entries: the game reads them from
    the terrain's folder. So pack-terrain produces that folder rather than a
    bare archive, and this puts the rest of it in place.
    """
    said: List[str] = []
    present = {f.lower(): f for f in os.listdir(source_dir)
               if os.path.isfile(os.path.join(source_dir, f))}

    # The icon. Spelled icon.* on the way in and TEXT.img on the way out: the
    # land texture inside the archive is text.img, the same name but for case,
    # and telling them apart by case alone has caught this tool out before.
    icon_src = next((present[f'icon{suffix}']
                     for suffix in ('.png', '.img.png', '.bmp', '.img.bmp')
                     if f'icon{suffix}' in present), None)
    if icon_src is None:
        # Only reachable from a listing, where nothing was scanned and so
        # nothing was offered; a scanned folder is stopped before this.
        said.append('no icon here, so the terrain has none; the game shows '
                    'one on the land generator screen and will not load '
                    'without it')
    else:
        path = os.path.join(source_dir, icon_src)
        with open(path, 'rb') as fh:
            blob = fh.read()
        if path.lower().endswith('.png'):
            w, h, pixels, pal, notes = read_png(blob)
            for note in notes:
                said.append(f'{icon_src}: {note}')
        else:
            w, h, pixels, pal = read_bmp(blob)
        if (w, h) != (ICON_DIM, ICON_DIM):
            said.append(f'{icon_src} is {w}x{h}; an icon must be '
                        f'{ICON_DIM}x{ICON_DIM}, so none was written')
        else:
            palette, remapped = build_palette(pixels, pal)
            icon = encode_icon(remapped, palette, compress=compress_img)
            os.makedirs(out_dir, exist_ok=True)
            with open(os.path.join(out_dir, ICON_NAME), 'wb') as fh:
                fh.write(icon)
            said.append(f'{icon_src} -> {ICON_NAME}')

    # Water.dir rides along as it is: wkTerrainSync reads it for the terrain's
    # own water colour, and nothing here has to understand it.
    water = next((present[f] for f in present if f == 'water.dir'), None)
    if water is not None:
        import shutil
        os.makedirs(out_dir, exist_ok=True)
        shutil.copyfile(os.path.join(source_dir, water),
                        os.path.join(out_dir, 'Water.dir'))
        said.append(f'{water} -> Water.dir')
    return said


BUILD_DIR_NAME = 'build'


def _terrain_needs(folder: str) -> str:
    """Whether this folder is meant to be packed as a terrain; '' when it is.

    The test is its name. Everything a terrain needs can now be stood in for,
    so there is no asset whose absence proves the folder is not one -- and
    running pack-terrain over some directory of pictures by accident is worth
    making hard. A folder called build says the author meant it.

    That is all the safety it has to carry. A build folder holding hundreds of
    unrelated pictures still fails, further along, on the 32 objects a terrain
    may have.
    """
    if os.path.basename(folder.rstrip(os.sep)).lower() == BUILD_DIR_NAME:
        return ''
    return (f'pack-terrain builds a folder called {BUILD_DIR_NAME}, and this '
            f'one is called {os.path.basename(folder.rstrip(os.sep))!r}. '
            f'Rename it, or use `pack` to build a .dir of anything else')


def scan_archive(folder: str, subfolders: Optional[Sequence[str]] = None
                 ) -> Tuple[List[str], List[str]]:
    """Work out an archive's entries from the files in a folder.

    Knows nothing of terrains: a picture with a .spd beside it is a sprite and
    any other is an image, anything else is carried through as it stands. That
    is all a .dir is -- Water.dir is 75 sprites and an image, Gfx.dir adds
    fonts -- and none of them has objects or an index.txt.

    `subfolders` limits which subdirectories are descended into; the default
    is all of them, which is what Gfx.dir's hi\\ and lo\\ need.
    """
    notes: List[str] = []
    entries: List[str] = []

    def scan_dir(path: str, prefix: str) -> None:
        present = {f.lower(): f for f in os.listdir(path)
                   if os.path.isfile(os.path.join(path, f))}
        seen: set = set()
        for low in sorted(present):
            split = split_picture(low)
            if split is None:
                continue
            stem, kind = split
            if stem in seen:
                continue
            seen.add(stem)
            if kind is None:
                kind = 'spr' if f'{stem}.spd' in present else 'img'
            if kind == 'spr' and f'{stem}.spd' not in present \
                    and f'{stem}.spr' not in present:
                notes.append(f'{prefix}{stem}.spr has neither a .spd nor a '
                             f'built .spr and cannot be rebuilt; skipped')
                continue
            entries.append(f'{prefix}{stem}.{kind}')
        # Everything that is not art or its metadata rides along untouched.
        # A decompressed folder holds both bubble1.spr and bubble1.spr.bmp --
        # the decoded pixels and the editable sheet -- so anything already
        # claimed above must not be added a second time.
        claimed = {e[len(prefix):].lower() for e in entries
                   if e.startswith(prefix)}
        for low in sorted(present):
            if split_picture(low) is not None or low in claimed:
                continue
            if low.endswith('.spd'):
                continue                # consumed by the sprite it belongs to
            if low.endswith('.dir.txt'):
                continue                # a listing of this folder, not content
            if low.endswith('.dir'):
                continue                # an archive of its own, not an entry
            entries.append(prefix + present[low])

    scan_dir(folder, '')
    for sub in sorted(os.listdir(folder)):
        if not os.path.isdir(os.path.join(folder, sub)):
            continue
        if subfolders is not None and sub.lower() not in \
                {s.lower() for s in subfolders}:
            continue
        scan_dir(os.path.join(folder, sub), f'{sub}\\')
    return entries, notes


def scan_terrain(folder: str) -> Tuple[List[str], List[str], List[str]]:
    """Work out a terrain's entry list from the files in a folder.

    Returns (entries, objects, notes). An object is a picture that is not one
    of the fixed core names. Its parameters are looked for beside it -- a .inf
    or a .txt that reads as one -- and defaulted when absent, so art alone is
    enough to build with.
    """
    notes: List[str] = []
    present = {f.lower(): f for f in os.listdir(folder)
               if os.path.isfile(os.path.join(folder, f))}
    core = {c.lower() for c in CORE_ENTRIES}

    def parameters_for(stem: str) -> Optional[str]:
        for cand in (f'{stem}.inf', f'{stem}.txt'):
            real = present.get(cand)
            if real is None:
                continue
            with open(os.path.join(folder, real), 'r', encoding='latin-1') as fh:
                if parse_inf(fh.read()) is not None:
                    return real
        return None

    # Objects are found from the art, not from the parameters: a picture is
    # the part that cannot be invented, and a missing .inf just means the
    # defaults apply.
    objects: List[str] = []
    defaulted: List[str] = []
    seen: set = set()
    for low in sorted(present):
        split = split_picture(low)
        if split is None:
            continue
        stem, kind = split
        if f'{stem}.img' in core or f'{stem}.spr' in core:
            continue                # a core asset under either spelling
        if stem == 'icon':
            continue                # goes beside Level.dir as TEXT.img, not in it
        if stem == PALETTE_NAME[:-4]:
            continue                # the swatch sheet this tool writes, not art
        if kind == 'spr' or f'{stem}.spd' in present:
            continue                # a sprite of its own, handled below
        if stem in seen:
            continue                # already have it under another extension
        seen.add(stem)
        objects.append(stem)
        if parameters_for(stem) is None:
            defaulted.append(stem)
    if defaulted:
        shown = ', '.join(sorted(defaulted)[:4])
        notes.append(f'{len(defaulted)} object(s) have no .inf or .txt beside '
                     f'them and take the default parameters '
                     f'{" ".join(str(v) for v in DEFAULT_INF)}: {shown}'
                     + (f' and {len(defaulted) - 4} more'
                        if len(defaulted) > 4 else ''))

    # Always alphabetical, whatever an index.txt in the folder happens to say:
    # one folder always packs to one archive. index.txt is generated into the
    # archive from this list rather than read from disk.
    #
    # The order decides which object the generator picks for a given map seed
    # (it walks the weight table built in this order), so re-packing a terrain
    # whose index.txt was in some other order will place different objects for
    # the same seed. How often each object appears is unchanged.
    objects = sorted(objects, key=str.lower)
    if 'index.txt' in present:
        with open(os.path.join(folder, present['index.txt']),
                  'r', encoding='latin-1') as fh:
            listed = [l.strip() for l in fh if l.strip()]
        if [l.lower() for l in listed] != [o.lower() for o in objects]:
            notes.append(f'the index.txt in the folder lists {len(listed)} '
                         f'object(s); packing the {len(objects)} found on disk, '
                         f'in alphabetical order, instead')
    notes.append(f'index.txt generated: {len(objects)} object(s), alphabetical')

    entries = ['index.txt']
    entries += [c for c in CORE_ENTRIES
                if any(f'{c.lower()}{ext}' in present
                       for ext in ('', '.bmp', '.png'))
                or any(f'{c.lower()[:-4]}{ext}' in present
                       for ext in PICTURE_EXTS)]
    for obj in objects:
        entries += [f'{obj}.img', f'{obj}.inf']

    # Sprite overrides. The archive names them with a backslash whatever the
    # host separator is, so build the name rather than reusing the path.
    for sub in SPRITE_SUBFOLDERS:
        subdir = os.path.join(folder, sub)
        if not os.path.isdir(subdir):
            continue
        found = set()
        for f in os.listdir(subdir):
            low = f.lower()
            for ext in ('.spr', '.img'):
                if low.endswith(f'{ext}.bmp') or low.endswith(f'{ext}.png'):
                    found.add(f[:-4])
                elif low.endswith(ext):
                    found.add(f)
        skipped = [n for n in found
                   if n.lower().endswith('.spr')
                   and not os.path.exists(os.path.join(subdir, n + '.spd'))
                   and not os.path.exists(os.path.join(subdir, n))]
        if skipped:
            notes.append(f'{sub}: {len(skipped)} sprite(s) have neither a .spd '
                         f'nor a built .spr and cannot be rebuilt: '
                         f'{", ".join(sorted(skipped)[:3])}')
        keep = sorted(set(found) - set(skipped), key=str.lower)
        entries += [f'{sub}\\{n}' for n in keep]
        if keep:
            notes.append(f'{sub}: {len(keep)} sprite override(s)')
    missing_core = [c for c in ('text.img', 'soil.img', 'grass.img',
                                'gradient.img', 'bridge.img', 'bridge-l.img',
                                'bridge-r.img')
                    if c not in entries]
    if missing_core:
        notes.append(f'no source for {", ".join(missing_core)}; every stock '
                     f'terrain has them')
    return entries, objects, notes


def entry_role(name: str) -> Optional[str]:
    """'icon', 'texture', or None -- decided by case, which this tool
    requires even though the game itself is not consistent about it.

    The two files share a name and differ only in case, and the game reads
    both: 36 of its themes spell the icon TEXT.IMG and 94 spell it text.img.
    Rather than guess from content, this tool insists on one spelling for each
    so a wrong size can be reported instead of silently shipped.
    """
    base = os.path.basename(name)
    if base == ICON_NAME:
        return 'icon'
    if base == TEXTURE_NAME:
        return 'texture'
    if base.lower() == TEXTURE_NAME:
        return 'ambiguous'
    return None


def entry_notes(name: str, data: bytes) -> List[str]:
    """Complaints about one packed entry, in the tool's own convention.

    Reported while building rather than left for the game, where a bad icon
    surfaces only as a crash on the land generator screen.
    """
    if name.lower().endswith('.inf'):
        values = parse_inf(data.decode('latin-1'))
        if values is None:
            return [f'{name}: not a list of numbers; an object needs the six '
                    f'parameters the guide describes']
        return [f'{name}: {p}' for p in inf_problems(values)]

    role = entry_role(name)
    if role is None:
        return []
    if not _img_dims(data):
        return []
    width, height = _img_dims(data)
    out: List[str] = []
    if role == 'icon':
        out += [f'{name}: {p}' for p in icon_problems(data)]
    elif role == 'texture':
        if (width, height) == (ICON_DIM, ICON_DIM):
            out.append(f'{name}: {width}x{height} looks like an icon; an icon '
                       f'must be named {ICON_NAME}, a land texture '
                       f'{TEXTURE_NAME}')
        elif (width, height) != (TEXTURE_DIM, TEXTURE_DIM):
            out.append(f'{name}: {width}x{height}; a land texture is '
                       f'{TEXTURE_DIM}x{TEXTURE_DIM}')
    else:
        out.append(f'{name}: spell an icon {ICON_NAME} and a land texture '
                   f'{TEXTURE_NAME}; this tool tells them apart by case')
    return out


def archive_problems(entries: Dict[str, bytes]) -> Tuple[List[str], List[str]]:
    """Check the archive as a whole against the terrain guide.

    Returns (refusals, notes). A refusal is a rule the guide states outright
    and that no shipped terrain breaks, so building anyway only produces an
    archive the game rejects -- an over-long index.txt crashes it on the land
    generator screen. A note is advisory: the shipped terrains do exceed the
    palette budget and do ship objects the compression rule would forbid.
    """
    refuse: List[str] = []
    notes: List[str] = []
    lower = {n.lower(): n for n in entries}

    index = lower.get('index.txt')
    if index is None:
        notes.append('no index.txt; the terrain will have no custom objects')
        listed: List[str] = []
    else:
        listed = [l.strip() for l in entries[index].decode('latin-1').splitlines()
                  if l.strip()]
        if not listed:
            refuse.append('index.txt is empty; a terrain needs at least 1 object')
        elif len(listed) > MAX_OBJECTS:
            refuse.append(f'index.txt lists {len(listed)} objects; the limit is '
                          f'{MAX_OBJECTS}. The game crashes on the land '
                          f'generator screen with more.')
        # The game reads an object's name from index.txt up to the first
        # space, then opens that .inf. A name with a space in it sends it
        # looking for a file that is not there -- "bed bug" becomes "bed.inf"
        # -- and it dies dereferencing what it did not find. None of the 3217
        # objects in a stock install has one.
        spaced = [o for o in listed if ' ' in o]
        if spaced:
            refuse.append(
                f'{len(spaced)} object name(s) contain a space: '
                f'{", ".join(repr(o) for o in spaced[:3])}'
                + (f' and {len(spaced) - 3} more' if len(spaced) > 3 else '')
                + f'. The game reads the name only as far as the space -- '
                  f'{spaced[0].split(" ")[0]}.inf for {spaced[0]!r} -- and '
                  f'crashes when that is missing. Rename them without spaces.')
        for obj in listed:
            if f'{obj.lower()}.img' not in lower:
                refuse.append(f'index.txt names {obj!r} but {obj}.img is not in '
                              f'the archive')
            elif f'{obj.lower()}.inf' not in lower:
                refuse.append(f'index.txt names {obj!r} but {obj}.inf is not in '
                              f'the archive')

    # A back.spr that draws nothing. The game composites the background when
    # a landscape is drawn, and one with no colours in it gave RenderContext__
    # DrawLandscape something it read as a pointer and died on. No shipped
    # terrain has an empty one -- the sparsest fills its 640x160 in a single
    # colour -- and a terrain is better off with no back.spr at all, which 24
    # of them do.
    back = lower.get('back.spr')
    if back is not None:
        sprite = SpriteFile(entries[back])
        if sprite.parse():
            sheet = sprite.render_sheet()
            if sheet is not None and not any(sheet):
                refuse.append(
                    f'{back} draws nothing: every pixel is transparent. The '
                    f'game crashes compositing an empty background. Give it '
                    f'something to draw, or leave it out -- a terrain does '
                    f'not need one.')
    # The two rearmost layers hold a single frame; the two in front of them
    # animate. back.spr is loaded straight into video memory as a plain image
    # and its loader rejects an animation before it reads a pixel, and _back
    # is static too for all that it goes through the sprite loader. Across the
    # shipped terrains: back.spr 118 files and _back.spr 47, every one of them
    # a single frame, where back2.spr animates in 9 of 40 and front.spr in all
    # 3. So an animated backdrop belongs in back2 or front.
    for name in ('back.spr', '_back.spr'):
        entry = lower.get(name)
        if entry is None:
            continue
        sprite = SpriteFile(entries[entry])
        if sprite.parse() and sprite.frames > 1:
            refuse.append(
                f'{entry} has {sprite.frames} frames. That layer holds one '
                f'still picture -- no shipped terrain animates it. Use a '
                f'single frame, or move the animation to back2.spr or '
                f'front.spr.')

    # Core image dimensions.
    for base, want in CORE_IMG_DIMS.items():
        n = lower.get(base)
        if n is None:
            continue
        got = _img_dims(entries[n])
        if got and got != want:
            if base in CORE_IMG_DIMS_REQUIRED:
                refuse.append(
                    f'{n} is {got[0]}x{got[1]}. It has to be '
                    f'{want[0]}x{want[1]} -- the game reads it at that size '
                    f'whatever it holds, and crashes on anything else. '
                    f'Resize the picture and pack again.')
            else:
                notes.append(f'{n} is {got[0]}x{got[1]}; the guide says '
                             f'{want[0]}x{want[1]}')
    for base, want_w in CORE_IMG_WIDTHS.items():
        n = lower.get(base)
        if n is None:
            continue
        got = _img_dims(entries[n])
        if got and got[0] != want_w:
            notes.append(f'{n} is {got[0]} wide; the guide says {want_w}')

    # The palette the engine aggregates across every image in the terrain.
    # Everything the terrain draws, background sprites included, but not the
    # gfx0/gfx1 overrides or the icon -- see plan_shared_palette.
    shared = set()
    for n, data in entries.items():
        if not data or '\\' in n:
            continue
        if n.lower() == 'icon.img':
            continue
        if data[:4] == ImageFile.SIGNATURE:
            im = ImageFile(data)
            if im.parse():
                shared.update(bytes(im.palette[i * 3:i * 3 + 3])
                              for i in range(im.ncolours))
        elif data[:4] == SpriteFile.SIGNATURE:
            sp = SpriteFile(data)
            if sp.parse():
                shared.update(bytes(sp.palette[i * 3:i * 3 + 3])
                              for i in range(sp.ncolours))
    if len(shared) > MAX_SHARED_COLOURS:
        notes.append(f'{len(shared)} unique colours across the terrain; the '
                     f'guide budgets {MAX_SHARED_COLOURS} and warns the '
                     f'terrain will not load beyond it')

    # Objects that SpriteEditor would have corrupted. Our own encoder does not
    # have that bug, so this only matters for art meant to go back through it.
    odd = []
    for obj in listed:
        n = lower.get(f'{obj.lower()}.img')
        if n is None:
            continue
        data = entries[n]
        if len(data) > 9 and data[9] & ImageFile.COMPRESSED_FLAG:
            got = _img_dims(data)
            if got and (got[0] % 4 or got[1] % 4):
                odd.append(f'{obj} ({got[0]}x{got[1]})')
    if odd:
        notes.append(f'{len(odd)} compressed object(s) are not a multiple of 4 '
                     f'in both dimensions, which SpriteEditor corrupts: '
                     f'{", ".join(odd[:4])}'
                     + (f' and {len(odd) - 4} more' if len(odd) > 4 else ''))
    return refuse, notes


def _img_dims(data: bytes) -> Optional[Tuple[int, int]]:
    if len(data) < 16 or data[:4] != ImageFile.SIGNATURE:
        return None
    ncol = struct.unpack_from('<H', data, 10)[0]
    base = 12 + ncol * 3
    if base + 4 > len(data):
        return None
    return struct.unpack_from('<HH', data, base)


def icon_problems(data: bytes) -> List[str]:
    """What makes `data` unfit as a terrain icon; empty when it is fine."""
    out: List[str] = []
    if len(data) < 16 or data[:4] != ImageFile.SIGNATURE:
        return ['not an .img']
    ncol = struct.unpack_from('<H', data, 10)[0]
    base = 12 + ncol * 3
    if base + 4 > len(data):
        return ['truncated header']
    width, height = struct.unpack_from('<HH', data, base)
    if (width, height) != (ICON_DIM, ICON_DIM):
        out.append(f'{width}x{height}, expected {ICON_DIM}x{ICON_DIM}')
    if data[9] & ImageFile.COMPRESSED_FLAG and ncol % 4:
        out.append(f'compressed with {ncol} drawn colours ({ncol + 1} counting '
                   f'transparency), which pads the pixel data by '
                   f'{(-(base + 4)) % 4} byte(s); the drawn count must be a '
                   f'multiple of 4')
    return out


def encode_icon(pixels: bytes, palette: bytes, compress: bool = True) -> bytes:
    """Build a terrain icon in the form the game's own icons use.

    `palette` holds only the colours that are drawn. Colour 0 is the
    transparent background and is never stored, so `pixels` index 1..n against
    an n-entry palette -- an icon of 16 drawn colours has ncol 16 and is the
    "17 colours" the terrain guide asks for, counting transparency. Passing a
    spare entry for transparency instead makes it a real colour at pixel 1 and
    costs a slot.

    The palette is brought up to a multiple of four entries so the pixel data
    starts on a 4-byte boundary. Only the alignment matters: icons of 4 and of
    20 drawn colours both load and convert, so the count itself is free --
    what the game will not take is a compressed icon padded before its pixels.
    """
    ncol = len(palette) // 3
    if pixels and max(pixels) > ncol:
        raise ValueError(
            f'pixel index {max(pixels)} has no colour: the palette holds '
            f'{ncol}, addressed as 1..{ncol} because 0 is transparent')
    if compress and ncol % 4:
        padded = (ncol + 3) & ~3
        # Repeat an existing colour rather than introduce a new one, so the
        # terrain's shared palette is unaffected.
        palette = palette + palette[:3] * (padded - ncol)
    return encode_image(ICON_DIM, ICON_DIM, pixels, palette, compress=compress)


def _pad_to_multiple_of_four(width: int, height: int, pixels: bytes
                             ) -> Tuple[int, int, bytes, bool]:
    """Grow a picture with transparent pixels until both sides divide by 4.

    The guide asks for it -- "the height and width values must be divisible by
    4 if you wish to use .IMG compression... the object will appear corrupt
    in-game" -- and a terrain of objects 117, 65 and 338 wide came out with
    every one of them mangled while those 124, 40 and 108 wide were perfect.

    The new pixels go at the top and on the right. An object stands on its
    bottom edge, so adding rows underneath would lift it off the ground; the
    guide uses that deliberately elsewhere, to float an object, which is not
    what is wanted here.
    """
    new_w = (width + 3) // 4 * 4
    new_h = (height + 3) // 4 * 4
    if (new_w, new_h) == (width, height):
        return width, height, pixels, False
    top = new_h - height
    out = bytearray(new_w * new_h)          # index 0 is transparent
    for y in range(height):
        src = y * width
        dst = (y + top) * new_w
        out[dst:dst + width] = pixels[src:src + width]
    return new_w, new_h, bytes(out), True


def encode_image(width: int, height: int, pixels: bytes, palette: bytes,
                 compress: bool = True) -> bytes:
    """Build an .img from a top-down 8-bit picture."""
    if len(pixels) != width * height:
        raise ValueError(f'expected {width * height} pixels, got {len(pixels)}')
    ncol = len(palette) // 3
    flags = ImageFile.PALETTE_FLAG | (ImageFile.COMPRESSED_FLAG if compress else 0)

    out = bytearray()
    out += ImageFile.SIGNATURE
    out += struct.pack('<I', 0)                    # file length, filled below
    out += bytes((8, flags))                       # bits per pixel, flags
    out += struct.pack('<H', ncol)
    out += palette
    out += struct.pack('<HH', width, height)
    # The image data starts on a 4-byte boundary. The wiki documents this only
    # for images inside land.dat, but every .img in a level archive has it too
    # (1563 unaligned-header images across the shipped archives, all padded
    # with zeros). Omitting it makes the game start decoding one or two bytes
    # into the stream, which yields garbage rather than an error.
    out += b'\x00' * (-len(out) % 4)
    out += Team17Compressor.compress(pixels) if compress else pixels
    struct.pack_into('<I', out, 4, len(out))
    return bytes(out)


def read_spd(text: str) -> Dict[str, int]:
    """Parse the `key = value` metadata spriteEditor writes beside a sprite."""
    out: Dict[str, int] = {}
    for line in text.splitlines():
        if '=' not in line:
            continue
        key, _, value = line.partition('=')
        try:
            out[key.strip().lower()] = int(value.strip())
        except ValueError:
            pass
    return out


class DirectoryWriter:
    """Builds a .dir archive.

    Layout, matching what the game's own tools produce:

        12 bytes  "DIR\\x1A", total file length, offset of the TOC
        ...       each file's data, separated by a single 0x1A byte
        4 bytes   TOC signature, 0x0000000A
        4096      1024-entry hash table; each slot holds the offset (relative
                  to the TOC) of the first entry with that name hash, or 0
        ...       entry records: next-entry offset, data offset, data length,
                  then the NUL-terminated name padded to a 4-byte boundary

    Entries keep the order they were added, which is the order of the
    accompanying .dir.txt listing.
    """

    SIGNATURE = b'DIR\x1A'
    TOC_SIGNATURE = 0x0000000A
    HASH_SIZE = 1024

    def __init__(self) -> None:
        self.entries: List[Tuple[str, bytes]] = []

    def add(self, name: str, data: bytes) -> None:
        self.entries.append((name, data))

    @staticmethod
    def hash_name(name: str) -> int:
        """The game's name hash (see the Graphics directory documentation)."""
        bits = 10
        size = 1 << bits
        total = 0
        for ch in name.encode('latin-1', 'replace'):
            total = ((total << 1) % size) | ((total >> (bits - 1)) & 1)
            total = (total + ch) % size
        return total

    def build(self) -> bytes:
        out = bytearray(self.SIGNATURE + b'\x00' * 8)
        offsets: List[Tuple[str, int, int]] = []
        for pos, (name, data) in enumerate(self.entries):
            offsets.append((name, len(out), len(data)))
            out += data
            # A single 0x1A byte separates consecutive files -- the same EOF
            # marker Team17 uses in its signatures. The last file is not
            # followed by one; the TOC begins immediately after it.
            if pos != len(self.entries) - 1:
                out += b'\x1a'

        toc_offset = len(out)

        # Entry records come after the hash table; size them first so the
        # table can point at them.
        table_bytes = 4 + self.HASH_SIZE * 4
        records: List[Tuple[str, int, bytes]] = []
        pos = table_bytes
        for name, off, length in offsets:
            raw = name.encode('latin-1', 'replace') + b'\x00'
            pad = (4 - (12 + len(raw)) % 4) % 4
            records.append((name, pos, raw + b'\x00' * pad))
            pos += 12 + len(raw) + pad

        # Chain entries that share a hash slot. The game walks the chain, so
        # order within a slot does not matter; keep insertion order.
        buckets: Dict[int, List[int]] = {}
        for idx, (name, _p, _r) in enumerate(records):
            buckets.setdefault(self.hash_name(name), []).append(idx)

        next_of = [0] * len(records)
        head = [0] * self.HASH_SIZE
        for slot, members in buckets.items():
            head[slot] = records[members[0]][1]
            for a, b in zip(members, members[1:]):
                next_of[a] = records[b][1]

        toc = bytearray()
        toc += struct.pack('<I', self.TOC_SIGNATURE)
        for slot in range(self.HASH_SIZE):
            toc += struct.pack('<I', head[slot])
        for idx, (name, _p, raw) in enumerate(records):
            _n, off, length = offsets[idx]
            toc += struct.pack('<III', next_of[idx], off, length)
            toc += raw

        out += toc
        struct.pack_into('<I', out, 4, len(out))
        struct.pack_into('<I', out, 8, toc_offset)
        return bytes(out)


def _pack_entry(base: str, name: str, recreate: bool, compress_spr: bool,
                compress_img: bool, opaque: bool,
                shared_palette: Optional[List[Tuple[int, int, int]]] = None,
                override: Optional[str] = None,
                force_palette: bool = False
                ) -> Optional[Tuple[bytes, bool]]:
    """Produce the bytes for one archive entry.

    `base` is the path the listing points at, without any added extension.
    Returns (payload, was_encoded) or None when no source file exists.

    With `recreate` set, a sprite or image is rebuilt from its BMP whenever
    one is present, so edits to the BMP take effect. Otherwise an existing
    binary is preferred and the BMP is only a fallback.
    """
    lower = name.lower()
    is_spr = lower.endswith('.spr')
    is_img = lower.endswith('.img')
    # An object is anything that is not one of the fixed core names, the same
    # rule the folder scan uses. Only objects are padded to a multiple of 4.
    is_object = (is_img and '\\' not in name
                 and lower not in {c.lower() for c in CORE_ENTRIES}
                 and lower != 'icon.img')
    # A BMP is already indexed and is taken as authored; a PNG has to be
    # thresholded and reduced on the way in, so it is the second choice when
    # both are there.
    # toaster.img.bmp is SpriteEditor's spelling; toaster.bmp means the same
    # thing here, so both are looked for. An indexed source is preferred over
    # a PNG, being already reduced and so authored exactly.
    if override:
        source_path: Optional[str] = override
    else:
        candidates = [base + ext for ext in PICTURE_EXTS]
        if lower.endswith(('.img', '.spr')):
            candidates += [base[:-4] + ext for ext in PICTURE_EXTS]
        source_path = next((c for c in candidates if os.path.exists(c)), None)

    def existing() -> Optional[Tuple[bytes, bool]]:
        if not os.path.exists(base):
            return None
        with open(base, 'rb') as fh:
            blob = fh.read()
        # `decompress` writes a sprite's decoded pixels under the entry's own
        # name, so a folder it produced holds a raw sheet where a .spr belongs.
        # Reusing that would pack a pixel dump as a sprite, so fall back to the
        # BMP instead -- the signature is the only way to tell them apart.
        want = (SpriteFile.SIGNATURE if is_spr else
                ImageFile.SIGNATURE if is_img else None)
        if want is not None and blob[:4] != want:
            return None
        return blob, False

    if lower.endswith('.inf'):
        # An object's parameters have to reach the archive as a .inf, but the
        # file on disk need not be spelled that way: take any sibling that
        # reads as one, so obj-1.txt serves as well as obj-1.inf. Falling back
        # to the guide's example keeps a folder buildable when an object has
        # no .inf at all.
        alt = base[:-4] + '.txt'
        if os.path.exists(base):
            if os.path.exists(alt):
                # Both spellings present and they may disagree, so say which
                # one is being packed rather than leave it to be guessed.
                print(f'  note: both {os.path.basename(base)} and '
                      f'{os.path.basename(alt)} are here; packing the .inf',
                      file=sys.stderr)
            # Copied through byte for byte: most shipped .inf files carry
            # trailing blank lines, and rewriting them would churn the
            # archive for nothing.
            with open(base, 'rb') as fh:
                return fh.read(), False
        if os.path.exists(alt):
            with open(alt, 'r', encoding='latin-1') as fh:
                values = parse_inf(fh.read())
            if values is not None:
                return format_inf(values), True
        return format_inf(DEFAULT_INF), True

    if not (is_spr or is_img):
        return existing()            # .txt and friends are copied as-is

    if not recreate:
        found = existing()
        if found is not None:
            return found

    if source_path is None:
        return existing()

    with open(source_path, 'rb') as fh:
        blob = fh.read()
    if source_path.lower().endswith('.png'):
        width, height, pixels, source_palette, png_notes = read_png(
            blob, palette=shared_palette)
        for note in png_notes:
            print(f'  note: {name}: {note}', file=sys.stderr)
    else:
        width, height, pixels, source_palette = read_bmp(blob)
        if force_palette and shared_palette:
            # An indexed source is normally packed as authored, but a palette
            # handed over with --read-palette is meant to be the whole of the
            # terrain's colours -- so this one is fitted to it as well.
            counts: Dict[Tuple[int, int, int], int] = {}
            for v in pixels:
                if v:
                    c = (source_palette[v * 3], source_palette[v * 3 + 1],
                         source_palette[v * 3 + 2])
                    counts[c] = counts.get(c, 0) + 1
            if counts:
                index_of, error = _map_to_palette(counts, shared_palette)
                table = bytearray(256)
                for c, i in index_of.items():
                    for v in range(1, 256):
                        if (source_palette[v * 3], source_palette[v * 3 + 1],
                                source_palette[v * 3 + 2]) == c:
                            table[v] = i
                pixels = pixels.translate(bytes(table))
                source_palette = b'\x00\x00\x00' + b''.join(
                    bytes(c) for c in shared_palette)
                shift = error / sum(counts.values())
                if shift:
                    print(f'  note: {name}: mean colour shift {shift:.1f} of '
                          f'441 ({100 * shift / 441:.1f}%)', file=sys.stderr)

    if opaque and is_img:
        # Opaque images have no transparent index, so shift every colour up
        # by one to free index 0 rather than letting colour 0 vanish.
        pixels = bytes(min(p + 1, 255) for p in pixels)
        source_palette = b'\x00\x00\x00' + source_palette

    palette, remapped = build_palette(pixels, source_palette)

    if is_img:
        if entry_role(name) == 'icon':
            if (width, height) != (ICON_DIM, ICON_DIM):
                raise ValueError(
                    f'an icon must be {ICON_DIM}x{ICON_DIM}, this is '
                    f'{width}x{height}')
            # encode_icon rounds the palette up to a multiple of four, which
            # is what keeps the pixel data off a padded offset.
            return encode_icon(remapped, palette, compress=compress_img), True
        # Objects only. A core asset has a size the game expects -- a 64-wide
        # bridge, a 136-wide grass strip -- and growing one would be a change
        # of its own, where an object's size is the author's to choose.
        if compress_img and is_object:
            width, height, remapped, grew = _pad_to_multiple_of_four(
                width, height, remapped)
            if grew:
                print(f'  note: {name}: grown to {width}x{height}; a '
                      f'compressed object the game draws correctly needs both '
                      f'sides a multiple of 4', file=sys.stderr)
        return encode_image(width, height, remapped, palette,
                            compress=compress_img), True

    # A borrowed sprite keeps its .spd beside the art it came with.
    spd_base = (source_path[:-4] if override
                else base) if source_path else base
    spd_path = next((p for p in (spd_base + '.spd', base + '.spd',
                                 base[:-4] + '.spd')
                     if os.path.exists(p)), base + '.spd')
    if not os.path.exists(spd_path):
        # The frame count cannot be recovered from the sheet -- it is one tall
        # image either way -- so without the .spd every frame collapses into
        # one cell and the sprite is silently wrong.
        raise ValueError(f'no {os.path.basename(spd_path)}; a sprite needs one '
                         f'for its frame count and cell size')
    with open(spd_path, 'r', encoding='latin-1') as fh:
        meta = read_spd(fh.read())
    frames = meta.get('frames', 1)
    cell_w = meta.get('width', width)
    cell_h = meta.get('height', height // max(frames, 1))
    if cell_h * frames != height or cell_w != width:
        raise ValueError(f'{os.path.basename(spd_path)} says {frames} frames of '
                         f'{cell_w}x{cell_h}, which needs a {cell_w}x'
                         f'{cell_h * frames} sheet; the BMP is {width}x{height}')
    # Every sprite is compressed, this one included. back.spr and debris.spr
    # were held out for a while, on the strength of one crash and the fact
    # that the stock terrains mostly store them raw -- 115 of 130 debris and
    # 113 of 118 back. But mostly is not always, and the exceptions settle it:
    # Coral Reef ships a COMPRESSED debris.spr at 400x400 by 160 frames, and
    # it loads. Distant Planet and Hildegard ship compressed back.spr files,
    # and they load.
    #
    # Holding them out was expensive. Debris is nearly all transparent -- 0.17%
    # ink in the terrain that prompted this -- so raw storage is almost pure
    # padding: 18,100,648 bytes where compression gives 274,981, the same
    # pixels either way. That one file put the archive over wkTerrainSync's
    # 10 MB transfer limit and made the terrain unsendable.
    #
    # Compression is only safe while the stream count stays at MAX_STREAMS or
    # under, which is a question of ink, not shape. Coral Reef's debris is
    # sparse -- ~784 ink pixels a frame -- so its 160 frames pack into 7
    # streams. A dense 400x400 frame holds ~120,000 and overflows MAX_DATA_POS
    # alone, taking a stream to itself; 160 of those need 160 streams and the
    # game cannot load them. encode_sprite refuses that case.
    compress_this = compress_spr
    blob = encode_sprite(cell_w, cell_h, frames, remapped, palette,
                         meta.get('flags', 1), meta.get('framerate', 0),
                         compress=compress_this)
    if not SpriteFile(blob).parse():
        raise ValueError('the encoded sprite does not read back; this is a bug '
                         'in the encoder, please report it')
    return blob, True


def print_help():
    """Print help message"""
    print(f"wa-py-spriteHelper v{__version__}")
    print("Extract and decompress Worms Armageddon .dir files")
    print("\nUsage: wa-py-spriteHelper.py <command> [options]")
    print("\nCommands:")
    print("  extract <dir_file> [output_dir]   Extract all files from .dir")
    print("  pack <folder|name.dir.txt> [output_dir]")
    print("                                    Build a .dir of anything")
    print("  pack-terrain <folder|name.dir.txt> [output_dir]")
    print("                                    Build a terrain's Level.dir,")
    print("                                    with the guide's rules applied")
    print("                                    --no-compress-img  store images raw")
    print("                                    --no-compress-spr  store sprites raw")
    print("                                    --no-recreate      reuse existing .spr/.img")
    print("                                    --opaque-img       no transparent colour")
    print("                                    --force            write even if the")
    print("                                                       terrain would not load")
    print("                                    --defaults         take anything missing")
    print("                                                       from presets/")
    print("                                    --no-defaults      never take any of it")
    print("                                    --no-output-inf    do not write an")
    print("                                                       object's settings back")
    print("                                    --write-palette    draw the terrain's")
    print("                                                       colours to palette.png")
    print("                                    --no-palette       leave colours as")
    print("                                                       authored")
    print("                                    --read-palette     fit the art to the")
    print("                                                       colours in palette.png")
    print("  decompress <dir_file> [output_dir] [--gif]")
    print("                                    Decode sprites to raw pixels, BMP and .spd")
    print("                                    --gif also writes animated GIFs (slow)")
    print("  list <dir_file>                   List files in .dir")
    print("  version                           Show version")
    print("  help                              Show this help")


def _expand_folder(argv):
    """Turn `decompress <folder>` into one run per archive underneath it.

    Windows has no find(1), so a shell loop is not a portable way to walk a
    game's data folder. Archives commonly share a basename -- Aqua ships nine
    called Water.dir and a level.dir per terrain -- so each archive's output
    mirrors its source path; a flat output folder would silently overwrite all
    but the last.
    """
    args = [a for a in argv[2:] if not a.startswith('-')]
    if not args or not os.path.isdir(args[0]):
        return None
    root = args[0]
    base = args[1] if len(args) > 1 else "decompressed"
    opts = [a for a in argv[2:] if a.startswith('-')]

    found = []
    for here, _dirs, files in os.walk(root):
        for name in sorted(files):
            if name.lower().endswith('.dir'):
                found.append(os.path.join(here, name))
    if not found:
        print(f"No .dir archives found under {root}")
        return []

    runs = []
    for path in found:
        rel = os.path.relpath(os.path.dirname(path), root)
        out = base if rel == os.curdir else os.path.join(base, rel)
        runs.append([argv[0], 'decompress', path, out] + opts)
    return runs


def main():
    """Main entry point"""
    if len(sys.argv) < 2:
        print_help()
        return 1

    if sys.argv[1] == "decompress":
        runs = _expand_folder(sys.argv)
        base = [a for a in sys.argv[2:] if not a.startswith('-')]
        base = base[1] if len(base) > 1 else "decompressed"
        if runs is not None:
            if not runs:
                return 1
            print(f"Found {len(runs)} archives")
            failed = 0
            saved = sys.argv
            for n, run in enumerate(runs, 1):
                print(f"\n[{n}/{len(runs)}] {run[2]}")
                sys.argv = run
                try:
                    if main():
                        failed += 1
                finally:
                    sys.argv = saved
            print(f"\nDone: {len(runs) - failed} of {len(runs)} archives "
                  f"decompressed into {base}")
            return 1 if failed == len(runs) else 0

    command = sys.argv[1]

    if command == "help":
        print_help()
        return 0

    elif command == "version":
        print(f"wa-py-spriteHelper v{__version__}")
        return 0

    elif command == "extract":
        if len(sys.argv) < 3:
            print("Error: extract requires dir_file argument")
            return 1

        dir_file = sys.argv[2]
        base_output_dir = sys.argv[3] if len(sys.argv) > 3 else "extracted"

        if not os.path.exists(dir_file):
            print(f"Error: File not found: {dir_file}")
            return 1

        # Create directory structure: output_dir/filename/
        dir_basename = os.path.splitext(os.path.basename(dir_file))[0]
        sprite_output_dir = os.path.join(base_output_dir, dir_basename)

        reader = DirectoryReader(dir_file)
        if not reader.read():
            return 1

        count = reader.extract_all(sprite_output_dir)
        print(f"\nExtracted {count} files to {sprite_output_dir}")
        return 0
    


    elif command == "decompress":
        args = [a for a in sys.argv[2:] if not a.startswith('-')]
        opts = [a for a in sys.argv[2:] if a.startswith('-')]
        unknown = [o for o in opts if o != '--gif']
        if unknown:
            print(f"Error: unknown option(s): {', '.join(unknown)}")
            return 1
        want_gif = '--gif' in opts

        if not args:
            print("Error: decompress requires dir_file argument")
            return 1

        dir_file = args[0]
        base_output_dir = args[1] if len(args) > 1 else "decompressed"

        if not os.path.exists(dir_file):
            print(f"Error: File not found: {dir_file}")
            return 1

        # Create directory structure: output_dir/filename/
        dir_basename = os.path.splitext(os.path.basename(dir_file))[0]
        sprite_output_dir = os.path.join(base_output_dir, dir_basename)
        # Name the GIF folder after the source .dir so decompressing several
        # archives into one output directory does not overwrite same-named GIFs.
        gif_output_dir = os.path.join(base_output_dir, f'{dir_basename} gifs')

        reader = DirectoryReader(dir_file)
        if not reader.read():
            return 1

        os.makedirs(sprite_output_dir, exist_ok=True)
        if want_gif:
            os.makedirs(gif_output_dir, exist_ok=True)
        count = 0
        gif_count = 0
        img_count = 0
        failed = []
        img_failed = []

        # Archive order, not alphabetical: the listing written at the end has
        # to reproduce it for `pack` to rebuild the archive as it was.
        entry_order = sorted(reader.files, key=lambda n: reader.files[n][0])
        copied = 0

        def copy_through(rel: str, blob: bytes) -> None:
            """Write an entry this command does not decode straight through.

            The .inf files and index.txt are authored, not generated, and the
            archive is the only copy -- without them the output folder is not
            something `pack` can build from.
            """
            dest = os.path.join(sprite_output_dir, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, 'wb') as out:
                out.write(blob)

        try:
            with open(dir_file, 'rb') as f:
                for filename in entry_order:
                    data = reader.extract_file(f, filename)
                    # Archive names carry Windows separators; off Windows a
                    # backslash is an ordinary character, so joining the name
                    # unchanged writes one file literally called
                    # "gfx0\name.spr" instead of gfx0/name.spr.
                    relname = filename.replace('\\', os.sep)
                    if not data or len(data) < 4:
                        if data:
                            copy_through(relname, data)
                            copied += 1
                        continue
                    # Go by the signature rather than the extension: a couple
                    # of level archives ship an image under a .inf name.
                    kind = data[0:4]
                    if kind == BankFile.SIGNATURE:
                        bank = BankFile(data)
                        if not bank.parse():
                            failed.append(filename)
                            continue
                        # A bank holds hundreds of unnamed animations, so give
                        # them a folder of their own and number them. The
                        # folder keeps the bank's own path -- two banks of one
                        # name in different subdirectories would otherwise
                        # number their sprites into the same folder, and the
                        # second would overwrite the first.
                        stem = os.path.splitext(relname)[0]
                        bank_dir = os.path.join(sprite_output_dir, stem)
                        os.makedirs(bank_dir, exist_ok=True)
                        if want_gif:
                            os.makedirs(os.path.join(gif_output_dir, stem),
                                        exist_ok=True)
                        palette = bank.rgb_palette()
                        made = 0
                        for k in range(len(bank.sprites)):
                            sheet = bank.render_sheet(k)
                            if sheet is None:
                                failed.append(f'{filename}#{k}')
                                continue
                            flags, w, h, _s, fc, _u, rate = bank.sprites[k]
                            stub = os.path.join(bank_dir, f'{k:04d}')
                            with open(stub + '.spr', 'wb') as out:
                                out.write(sheet)
                            with open(stub + '.spr.spd', 'w') as out:
                                out.write(f"frames = {fc}\nheight = {h}\n"
                                          f"width = {w}\nframerate = {rate}\n"
                                          f"flags = {flags}\n")
                            bmp = SpriteFile._create_bmp(sheet, palette, w, h * fc)
                            if bmp:
                                with open(stub + '.spr.bmp', 'wb') as out:
                                    out.write(bmp)
                            if want_gif:
                                gif = SpriteFile._create_gif(sheet, palette, w, h,
                                                             fc, rate, flags)
                                if gif:
                                    gp = os.path.join(gif_output_dir, stem,
                                                      f'{k:04d}.gif')
                                    with open(gp, 'wb') as out:
                                        out.write(gif)
                                    gif_count += 1
                            made += 1
                        print(f"Decompressed bank: {filename} "
                              f"({made} sprites, {bank.ncolours} colours)")
                        count += made
                        continue
                    if kind == ImageFile.SIGNATURE:
                        image = ImageFile(data)
                        if not image.parse():
                            img_failed.append(filename)
                            continue
                        # Named the way spriteEditor names its exports, so the
                        # output folder can be fed straight back to `pack`.
                        bmp = SpriteFile._create_bmp(image.pixels,
                                                     image.rgb_palette(),
                                                     image.width, image.height)
                        if bmp is None:
                            img_failed.append(filename)
                            continue
                        out_path = os.path.join(sprite_output_dir, relname + '.bmp')
                        os.makedirs(os.path.dirname(out_path), exist_ok=True)
                        with open(out_path, 'wb') as out:
                            out.write(bmp)
                        print(f"Decoded image: {filename} "
                              f"({image.width}x{image.height}, "
                              f"{image.ncolours} colours)")
                        img_count += 1
                        continue
                    if kind != SpriteFile.SIGNATURE:
                        copy_through(relname, data)
                        copied += 1
                        continue

                    sprite = SpriteFile(data)
                    if not sprite.parse():
                        failed.append(filename)
                        continue

                    sheet = sprite.render_sheet()
                    if sheet is None:
                        failed.append(filename)
                        continue

                    output_path = os.path.join(sprite_output_dir, relname)
                    os.makedirs(os.path.dirname(output_path), exist_ok=True)

                    with open(output_path, 'wb') as out:
                        out.write(sheet)

                    with open(output_path + '.spd', 'w') as out:
                        out.write(sprite.to_metadata_string())

                    palette = sprite.rgb_palette()
                    bmp = SpriteFile._create_bmp(sheet, palette, sprite.width,
                                                 sprite.height * sprite.frames)
                    if bmp:
                        with open(output_path + '.bmp', 'wb') as out:
                            out.write(bmp)

                    if want_gif:
                        frames_top_down = b''.join(
                            sprite.render_frame(i) for i in range(sprite.frames))
                        gif = SpriteFile._create_gif(frames_top_down, palette,
                                                     sprite.width, sprite.height,
                                                     sprite.frames, sprite.framerate,
                                                     sprite.flags)
                        if gif:
                            gif_path = os.path.join(
                                gif_output_dir, os.path.splitext(relname)[0] + '.gif')
                            os.makedirs(os.path.dirname(gif_path), exist_ok=True)
                            with open(gif_path, 'wb') as out:
                                out.write(gif)
                            gif_count += 1

                    print(f"Decompressed: {filename} "
                          f"({sprite.frames} frames, {sprite.width}x{sprite.height}, "
                          f"{sprite.ncolours} colours)")
                    count += 1

        except Exception as e:
            print(f"Error: {e}")
            return 1

        # The listing `pack` takes as its argument. It is not stored in the
        # archive, so rebuild it from the entry order; CRLF and no .spd
        # entries, the form SpriteEditor writes.
        listing = os.path.join(sprite_output_dir, f'{dir_basename}.dir.txt')
        with open(listing, 'w', encoding='latin-1', newline='') as out:
            out.write(''.join(f'{n}\r\n' for n in entry_order))

        # A terrain's icon is not in the archive: it sits beside it, spelled
        # text.img in any casing the author fancied -- 102 of a stock install
        # use text.img, 38 TEXT.IMG. Bring it along under the name pack-terrain
        # looks for, or a terrain taken apart and put back together would lose
        # the icon and be offered a default in its place.
        icon_out = _emit_sibling_icon(dir_file, sprite_output_dir)
        if icon_out:
            print(f"Wrote {icon_out} from the icon beside the archive")

        print(f"\nDecompressed {count} sprites to {sprite_output_dir}")
        if img_count:
            print(f"Decoded {img_count} images to {sprite_output_dir}")
        if copied:
            print(f"Copied {copied} files through unchanged (.inf, index.txt)")
        print(f"Wrote {os.path.basename(listing)} listing {len(entry_order)} entries")
        if want_gif:
            print(f"Generated {gif_count} animated GIFs in {gif_output_dir}")
        if failed:
            print(f"Could not decode {len(failed)} sprites "
                  f"(unsupported SPR layout variant): {', '.join(failed[:5])}"
                  + (' ...' if len(failed) > 5 else ''))
        if img_failed:
            print(f"Could not decode {len(img_failed)} images: "
                  f"{', '.join(img_failed[:5])}"
                  + (' ...' if len(img_failed) > 5 else ''))
        return 0

    elif command in ("pack", "pack-terrain"):
        # `pack` builds any .dir; `pack-terrain` adds everything the terrain
        # guide asks for -- objects, index.txt, the palette budget, the
        # defaults, the icon -- none of which means anything for a Water.dir
        # or Gfx.dir.
        terrain = command == "pack-terrain"
        args = [a for a in sys.argv[2:] if not a.startswith('-')]
        opts = [a for a in sys.argv[2:] if a.startswith('-')]
        known = {'--no-compress-spr', '--no-compress-img',
                 '--no-recreate', '--opaque-img', '--force',
                 '--defaults', '--no-defaults', '--no-output-inf',
                 '--write-palette', '--read-palette', '--no-palette'}
        unknown = [o for o in opts if o not in known]
        if unknown:
            print(f"Error: unknown option(s): {', '.join(unknown)}")
            return 1

        compress_spr = '--no-compress-spr' not in opts
        compress_img = '--no-compress-img' not in opts
        recreate = '--no-recreate' not in opts
        opaque = '--opaque-img' in opts
        force = '--force' in opts
        # On by default: an object with no parameters is given the guide's,
        # and writing them out is what makes that visible rather than
        # implied. Anything already there is left alone.
        output_inf = '--no-output-inf' not in opts
        write_palette = '--write-palette' in opts
        read_palette = '--read-palette' in opts
        no_palette = '--no-palette' in opts
        if no_palette and read_palette:
            print("Error: --no-palette and --read-palette ask for opposite "
                  "things. One leaves the colours alone; the other fits every "
                  "picture to a palette you supply.")
            return 1
        # None means ask; the flags answer ahead of time, for a script.
        assume_defaults: Optional[bool] = None
        if '--defaults' in opts:
            assume_defaults = True
        if '--no-defaults' in opts:
            assume_defaults = False

        if not args:
            print("Error: pack requires a folder or a <name>.dir.txt listing")
            return 1
        target = args[0]
        if not os.path.exists(target):
            print(f"Error: File not found: {target}")
            return 1

        # Entries the folder does not hold a file for, built here instead.
        synthetic: Dict[str, bytes] = {}
        # Entries whose art comes from the tool's own defaults rather than the
        # terrain's folder, keyed by entry name.
        extra: Dict[str, str] = {}
        # ...and their names alone, which the palette planner needs so it can
        # leave the whole budget to the author's own art.
        lent_names: List[str] = []
        # Whether the entries came from reading the folder rather than from a
        # listing. Only then is the folder the terrain's own, and only then is
        # writing anything back into it right.
        scanned = False
        # A terrain's object settings, read from object_settings.txt by the
        # scan. Empty from a listing, where there is no folder to keep one in.
        obj_settings: Dict[str, List[int]] = {}

        if os.path.isdir(target):
            # Nothing to author: the folder is the input. A Level.dir.txt in
            # it still wins, so a build that has one keeps its exact order.
            source_dir = os.path.abspath(target)
            listings = [f for f in os.listdir(source_dir)
                        if f.lower().endswith('.dir.txt')]
            if listings:
                stem = listings[0][:-len('.dir.txt')]
                with open(os.path.join(source_dir, listings[0]),
                          'r', encoding='latin-1') as fh:
                    names = [ln.strip() for ln in fh if ln.strip()]
                print(f"Using {listings[0]} ({len(names)} entries)")
            else:
                if not terrain:
                    names, scan_notes = scan_archive(source_dir)
                    stem = os.path.basename(source_dir.rstrip(os.sep))
                    for note in scan_notes:
                        print(f'  note: {note}', file=sys.stderr)
                    print(f"Scanned {os.path.basename(source_dir)}: "
                          f"{len(names)} entries")
                else:
                    missing = _terrain_needs(source_dir)
                    if missing:
                        print(f"Not packing: {missing}.")
                        return 1
                    scanned = True
                    names, _objects, scan_notes = scan_terrain(source_dir)
                    stem = 'Level'
                    for note in scan_notes:
                        print(f'  note: {note}', file=sys.stderr)
                    synthetic['index.txt'] = ''.join(
                        f'{o}\r\n' for o in _objects).encode('latin-1')
                    # Not assume_defaults: that says whether to borrow art,
                    # and deleting a file the author wrote is a separate
                    # question that nothing should answer on their behalf.
                    obj_settings, trouble = _settle_object_settings(
                        source_dir, _objects, None)
                    if trouble:
                        print(f"Not packing "
                              f"{os.path.basename(source_dir)}: {trouble}")
                        return 1
                    for stem, values in obj_settings.items():
                        synthetic[f'{stem}.inf'] = format_inf(values)
                    settings = read_settings(source_dir)
                    first_run = settings is None
                    if first_run:
                        print(f"  first run: offering what "
                              f"{DEFAULTS_DIR} has for anything missing")
                    names, borrowed, refused = _fill_from_defaults(
                        names, source_dir, assume_defaults, first_run)
                    if refused:
                        print(f"Not packing "
                              f"{os.path.basename(source_dir)}: {refused}")
                        return 1
                    if borrowed:
                        # The art is in the folder now, so scan it again and
                        # let the copies be found like anything else. That
                        # lays them out where a later run would, so one folder
                        # packs to one archive whether or not this was the run
                        # that fetched them.
                        names, _objects, _ = scan_terrain(source_dir)
                    if first_run:
                        # Written whatever was decided, including nothing:
                        # the point is that the questions were asked once.
                        write_settings(source_dir, {
                            'created': _today(),
                            'borrowed': (','.join(sorted(borrowed))
                                         or 'nothing'),
                        })
                        # They keep no claim on the palette, though: the budget
                        # is the author's, and a default is fitted to what is
                        # left of it rather than shrinking their work for it.
                        lent_names = list(borrowed)
                    print(f"Scanned {os.path.basename(source_dir)}: "
                          f"{len(names)} entries, {len(_objects)} objects")
        else:
            listing = target
            if not listing.lower().endswith('.dir.txt'):
                print(f"Error: expected a folder or a file named "
                      f"<name>.dir.txt, got {os.path.basename(listing)}")
                return 1
            source_dir = os.path.dirname(os.path.abspath(listing))
            stem = os.path.basename(listing)[:-len('.dir.txt')]
            with open(listing, 'r', encoding='latin-1') as fh:
                names = [ln.strip() for ln in fh if ln.strip()]

        # A terrain is a folder the game reads -- Level.dir, TEXT.img and
        # optionally Water.dir -- so pack-terrain writes one, named after the
        # source. `pack` writes a bare archive, which is all a Water.dir or
        # Gfx.dir is.
        if terrain:
            terrain_name = os.path.basename(source_dir.rstrip(os.sep))
            out_dir = (args[1] if len(args) > 1
                       else os.path.join(os.path.dirname(source_dir),
                                         f'{terrain_name} packed'))
            out_path = os.path.join(out_dir, 'Level.dir')
        else:
            out_dir = args[1] if len(args) > 1 else source_dir
            out_path = os.path.join(out_dir, stem.lower() + '.dir')

        # Writing into the folder being read would put the archive among the
        # art it was built from, where the next run would try to pack it.
        if os.path.abspath(out_dir) == os.path.abspath(source_dir):
            print(f"Error: the output folder is the source folder "
                  f"({source_dir}).")
            print("  Give a different one, or leave it out and a "
                  "'<name> packed' folder is made beside it.")
            return 1

        writer = DirectoryWriter()
        built = reused = 0
        problems: List[str] = []
        packed: Dict[str, bytes] = {}

        # Cut one palette across the terrain before building anything: the
        # engine aggregates every picture's colours into one table, so the
        # budget is the archive's, not each picture's.
        picture_sources: Dict[str, str] = {}
        for name in names:
            if name in synthetic:
                continue
            low = name.lower()
            if not (low.endswith('.img') or low.endswith('.spr')):
                continue
            if '\\' in name or '/' in name:
                continue        # a gfx0/gfx1 override, not the terrain's art
            if low == 'icon.img':
                continue        # a land-generator icon, not drawn in play
            # The terrain's icon is not in here to exclude: it lives beside
            # Level.dir, and it is spelled TEXT.img where the land texture
            # inside is text.img -- the same name but for case.
            if name in extra:
                picture_sources[name] = extra[name]
                continue
            stem = os.path.join(source_dir, name.replace('\\', os.sep))
            # Same candidates _pack_entry will use, or the plan is drawn from
            # a different set of pictures than the one that gets built.
            for cand in ([stem + e for e in PICTURE_EXTS] +
                         [stem[:-4] + e for e in PICTURE_EXTS]):
                if os.path.exists(cand):
                    picture_sources[name] = cand
                    break
        # The shared palette budget is a terrain's; a Water.dir or Gfx.dir
        # holds art the game reaches by other routes and the guide's 112 says
        # nothing about them.
        shared_palette = None
        if terrain and no_palette:
            # No terrain-wide cut. An indexed source is then packed exactly as
            # authored -- which is the point: an author who has already fitted
            # their art to a palette they chose gets it back untouched, rather
            # than nudged to make room for the rest of the terrain.
            #
            # A PNG is still reduced if it draws more than an .img can hold,
            # because that is the format's limit and not ours to waive; the
            # difference is that the reduction now looks at one picture rather
            # than at all of them together. Whether the total lands inside the
            # guide's 112 becomes the author's business, and the count printed
            # after packing says whether it did.
            print("  --no-palette: each picture keeps its own colours")
        elif terrain and read_palette:
            # An author's own palette, taken as given rather than cut from the
            # art: every picture is fitted to it, whatever that costs them.
            given = os.path.join(source_dir, PALETTE_NAME)
            shared_palette, trouble = read_palette_sheet(given)
            if trouble:
                print(f'  note: {trouble}', file=sys.stderr)
            if not shared_palette:
                print(f"Not packing: --read-palette but no palette to read. "
                      f"Run --write-palette first, or put one at {given}.")
                return 1
            print(f"  reading {len(shared_palette)} colours from "
                  f"{PALETTE_NAME}")
        elif terrain:
            shared_palette, palette_notes = plan_shared_palette(
                picture_sources, borrowed=lent_names)
            for note in palette_notes:
                print(f'  note: {note}', file=sys.stderr)

        for name in names:
            if name in synthetic:
                payload = synthetic[name]
                for note in entry_notes(name, payload):
                    print(f'  note: {note}', file=sys.stderr)
                writer.add(name, payload)
                packed[name] = payload
                built += 1
                continue
            rel = name.replace('\\', os.sep)
            base = os.path.join(source_dir, rel)
            # A gfx0/gfx1 override is painted with the slot's own fixed table,
            # not with the terrain's palette and not with the one in its own
            # header, so it has to be fitted to that table instead. Forced,
            # because an indexed source is no more free to stray than a PNG.
            entry_palette = shared_palette
            force_this = read_palette
            slot = GFX_PALETTES.get(rel.split(os.sep)[0].lower())
            if slot is not None:
                entry_palette = list(slot)
                force_this = True
            try:
                data = _pack_entry(base, name, recreate, compress_spr,
                                   compress_img, opaque, entry_palette,
                                   extra.get(name), force_this)
            except Exception as exc:
                problems.append(f"{name}: {exc}")
                continue
            if data is None:
                problems.append(f"{name}: no source file found")
                continue
            payload, was_built = data
            for note in entry_notes(name, payload):
                print(f'  note: {note}', file=sys.stderr)
            writer.add(name, payload)
            packed[name] = payload
            built += was_built
            reused += not was_built

        if problems:
            print(f"Could not pack {len(problems)} of {len(names)} entries:")
            for p in problems[:10]:
                print(f"  {p}")
            if len(problems) > 10:
                print(f"  ... and {len(problems) - 10} more")
            return 1

        refusals, notes = archive_problems(packed) if terrain else ([], [])
        for note in notes:
            print(f'  note: {note}', file=sys.stderr)
        if refusals:
            print(f"Refusing to write {out_path}: the terrain would not load.")
            for r in refusals[:10]:
                print(f"  {r}")
            if len(refusals) > 10:
                print(f"  ... and {len(refusals) - 10} more")
            print("  (see docs/Guide.MD; pass --force to write it anyway)")
            if not force:
                return 1
            print("  --force given; writing anyway.")

        # A terrain keeps its object settings in object_settings.txt, written
        # by the scan; writing them out again one file to an object would put
        # them in two places and undo the consolidation on the next run.
        if output_inf and terrain and scanned and not obj_settings:
            # An object's parameters as packed, written back beside the art so
            # that whatever it was given -- the defaults, most often -- is
            # visible and editable rather than staying implied. Only for a
            # scanned folder: a listing may sit anywhere, and writing files
            # next to one is not what was asked for.
            #
            # Written as .txt: the archive needs the entry to be a .inf, but on
            # disk a .txt is the friendlier of the two names to open, and the
            # tool reads either.
            written = kept = 0
            for name, payload in sorted(packed.items()):
                if not name.lower().endswith('.inf'):
                    continue
                rel = name.replace('\\', os.sep)
                base = os.path.join(source_dir, rel[:-4])
                if os.path.exists(base + '.inf') or os.path.exists(base + '.txt'):
                    kept += 1           # authored, so not overwritten
                    continue
                os.makedirs(os.path.dirname(base + '.txt'), exist_ok=True)
                with open(base + '.txt', 'wb') as fh:
                    fh.write(payload)
                written += 1
            print(f"Wrote {written} parameter file(s) to "
                  f"{os.path.basename(source_dir)}"
                  + (f", left {kept} already there alone" if kept else ""))

        archive = writer.build()
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        with open(out_path, 'wb') as fh:
            fh.write(archive)

        if write_palette and terrain:
            # Into the source folder, not the output: it is for the author to
            # look at and hand back with --read-palette, where the art is.
            dest = os.path.join(source_dir if scanned else out_dir,
                                PALETTE_NAME)
            print(f"  {_write_palette_sheet(packed, dest)}")
        elif write_palette:
            print("  note: --write-palette is for a terrain; the 112-colour "
                  "budget says nothing about other archives", file=sys.stderr)

        if terrain:
            for line in _write_terrain_folder(source_dir, out_dir,
                                              compress_img):
                print(f"  {line}")

        print(f"Packed {len(names)} entries into {out_path}")
        print(f"  encoded from source images: {built}")
        print(f"  copied unchanged:           {reused}")
        print(f"  archive size:               {len(archive):,} bytes")
        if terrain and len(archive) > MAX_SYNC_BYTES:
            biggest = max(packed.items(), key=lambda kv: len(kv[1]))
            print(f"  note: past the {MAX_SYNC_BYTES:,} bytes wkTerrainSync "
                  f"will send. It plays here, but another player's game "
                  f"refuses the transfer.", file=sys.stderr)
            print(f"  note: the largest entry is {biggest[0]} at "
                  f"{len(biggest[1]):,} bytes", file=sys.stderr)
        return 0

    elif command == "land":
        if len(sys.argv) < 3:
            print("Error: land requires a .dat or mission file")
            return 1
        src = sys.argv[2]
        out_dir = sys.argv[3] if len(sys.argv) > 3 else "land"
        if not os.path.exists(src):
            print(f"Error: File not found: {src}")
            return 1
        with open(src, 'rb') as f:
            data = f.read()
        if data[0:4] not in LandFile.SIGNATURES:
            # A mission archive: FourCC + u32 length chunks, the map inside IMG.
            data = _mission_land(data)
            if data is None:
                print(f"Error: no land data in {src}")
                return 1
        land = LandFile(data)
        if not land.parse():
            print(f"Error: could not read the land data in {src}")
            return 1
        os.makedirs(out_dir, exist_ok=True)
        stem = os.path.splitext(os.path.basename(src))[0]
        names = ['', '.mask', '.background', '.extra']
        written = 0
        for k, image in enumerate(land.images):
            bmp = SpriteFile._create_bmp(image.pixels, image.rgb_palette(),
                                         image.width, image.height)
            if not bmp:
                continue
            with open(os.path.join(out_dir, f'{stem}{names[k]}.bmp'), 'wb') as fh:
                fh.write(bmp)
            written += 1
        with open(os.path.join(out_dir, f'{stem}.txt'), 'w') as fh:
            fh.write(f"width = {land.width}\nheight = {land.height}\n"
                     f"cavern border = {land.border}\nwater height = {land.water}\n"
                     f"texture = {land.texture}\nwater dir = {land.water_dir}\n"
                     f"objects = {len(land.objects)}\n")
            for x, y in land.objects:
                fh.write(f"  {x} {y}\n")
        print(f"{stem}: {land.width}x{land.height}, {len(land.objects)} objects, "
              f"texture {land.texture}")
        print(f"Wrote {written} images to {out_dir}")
        return 0

    elif command == "list":
        if len(sys.argv) < 3:
            print("Error: list requires dir_file argument")
            return 1

        dir_file = sys.argv[2]

        if not os.path.exists(dir_file):
            print(f"Error: File not found: {dir_file}")
            return 1

        reader = DirectoryReader(dir_file)
        if not reader.read():
            return 1

        reader.list_files()
        return 0

    else:
        print(f"Error: Unknown command: {command}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
