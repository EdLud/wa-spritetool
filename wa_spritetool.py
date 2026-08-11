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
from typing import Optional, BinaryIO, Dict, Tuple, List

try:
    from PIL import Image
except ImportError:  # optional; only affects GIF output
    Image = None

__version__ = "0.3.0"

# Sanity bound on decoded sprite dimensions. Level themes ship full-screen
# backdrops, the largest seen being 1280x370.
MAX_DIM = 8192

# Highest offset a frame may start at within its stream. The game's own
# archives never exceed it, and matching the rule reproduces their stream
# splitting exactly; see encode_sprite.
MAX_DATA_POS = 16384


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
            stream data (offsets relative to here)

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

    def parse(self) -> bool:
        if len(self.data) < 12 or self.data[0:4] != self.SIGNATURE:
            return False
        try:
            return (self._parse_compressed() if self.is_compressed
                    else self._parse_uncompressed())
        except (struct.error, IndexError, ValueError, DecompressionError):
            return False

    @property
    def is_compressed(self) -> bool:
        """Uncompressed sprites (flag bit clear) store raw cropped pixels and
        have no stream table; Gfx0.dir is entirely uncompressed."""
        return bool(struct.unpack('<H', self.data[8:10])[0] & self.COMPRESSED_FLAG)

    def _header(self):
        ncol = struct.unpack('<H', self.data[10:12])[0]
        p = 12 + ncol * 3
        nstream = struct.unpack('<I', self.data[p:p + 4])[0]
        # A level backdrop runs to 128 streams, well past anything in Gfx.dir.
        if not 0 < nstream <= 4096:
            raise ValueError('implausible stream count')
        return ncol, p, nstream

    def _finish(self, ncol, p, rec, ft, blobs) -> bool:
        """Read the sprite record at `rec` and the frame table at `ft`."""
        rate, flags, w, h, fc = struct.unpack('<HHHHH', self.data[rec:rec + 10])
        if not (0 < w <= MAX_DIM and 0 < h <= MAX_DIM and 0 < fc <= 4096):
            return False
        if ft + fc * 12 > len(self.data):
            return False
        self.framerate, self.flags = rate, flags
        self.width, self.height, self.frames = w, h, fc
        self.palette = self.data[12:p]
        self.blobs = blobs
        self.recs = [struct.unpack('<HHHHHH', self.data[ft + i * 12:ft + i * 12 + 12])
                     for i in range(fc)]
        return True

    def _parse_uncompressed(self) -> bool:
        """Uncompressed sprites (flag bit 0x4000 clear, as in Gfx0.dir).

        There is no stream count and no stream table: the palette is followed
        by a 4-byte gap (holding the frame rate at +2), the sprite record, one
        pad byte, the frame table, then the cropped frame pixels laid out
        contiguously. All frame data is treated as a single implicit stream.
        """
        ncol = struct.unpack('<H', self.data[10:12])[0]
        p = 12 + ncol * 3
        rec = p + 2
        # The frame table is aligned the way the stream table is in the
        # compressed form: pad the sprite record by ncol % 4.
        ft = rec + 10 + (ncol % 4)
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
        q = p + 4 + (ncol % 4)              # stream table, 4-byte aligned
        positional = ncol % 4 in (0, 1)
        rec = q + nstream * 12 + (2 if positional else -2)
        ft = rec + 10
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
        blobs = [Team17Decompressor.decompress(self.data[ds + pos:], dlen)
                 if dlen else b'' for pos, dlen in spans]
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
            if dest >= h:
                break
            row = src[start + y * fw:start + (y + 1) * fw]
            # A few sprites (circle25) declare a box wider than the cell;
            # the game clips it rather than wrapping into the next row.
            visible = min(fw, w - l)
            cell[dest * w + l:dest * w + l + visible] = row[:visible]
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

        if Image is not None:
            return SpriteFile._create_gif_pillow(
                frames_data, palette, width, height, frame_count, framerate)

        # GIF header
        gif = b'GIF89a'

        # Logical Screen Descriptor
        gif += struct.pack('<H', width)
        gif += struct.pack('<H', height)
        gif += bytes([0xF7])  # Global Color Table Flag=1, Color Resolution=7, Sort Flag=0, Size of Global Color Table=7 (256 colors)
        gif += bytes([0x00])  # Background Color Index
        gif += bytes([0x00])  # Pixel Aspect Ratio

        # Global Color Table (256 * 3 bytes, RGB format)
        gif += palette

        # Animation frames
        for frame_idx in range(frame_count):
            # Graphic Control Extension (animation timing)
            gif += bytes([0x21, 0xF9])  # Extension introducer and label
            gif += bytes([0x04])  # Block size
            gif += bytes([0x00])  # Packed fields (no transparency)

            # Delay in centiseconds. Anything under 2cs is clamped by most
            # viewers to ~10cs, so keep a floor; 5cs matches the Pillow path's
            # default when the sprite declares no rate.
            delay = 5 if framerate <= 0 else max(2, round(100 / framerate))

            gif += struct.pack('<H', delay)
            gif += bytes([0x00, 0x00])  # Transparent color index and block terminator

            # Image Descriptor
            gif += bytes([0x2C])  # Image separator
            gif += struct.pack('<H', 0)  # Image left
            gif += struct.pack('<H', 0)  # Image top
            gif += struct.pack('<H', width)
            gif += struct.pack('<H', height)
            gif += bytes([0x00])  # Packed fields (no local color table)

            # Compress frame data using LZW
            frame_start = frame_idx * width * height
            frame_end = frame_start + width * height
            frame_pixels = frames_data[frame_start:frame_end]

            # Simple LZW compression
            lzw_data = SpriteFile._lzw_encode(frame_pixels)
            gif += bytes([0x08])  # LZW minimum code size
            gif += SpriteFile._write_gif_data_blocks(lzw_data)

        # GIF Trailer
        gif += bytes([0x3B])

        return gif

    @staticmethod
    def _lzw_encode(data: bytes, min_code_size: int = 8) -> bytes:
        """LZW-compress a frame for GIF.

        Codes are emitted least-significant-bit first at a width that grows
        with the dictionary. The width must be bumped in the same pass that
        emits the codes -- a decoder tracks dictionary growth as it reads, so
        deciding widths separately from emission desynchronises the two and
        truncates the image partway through.
        """
        clear_code = 1 << min_code_size
        eoi_code = clear_code + 1

        out = bytearray()
        bit_buffer = 0
        bits = 0

        def emit(code, width):
            nonlocal bit_buffer, bits
            bit_buffer |= code << bits
            bits += width
            while bits >= 8:
                out.append(bit_buffer & 0xFF)
                bit_buffer >>= 8
                bits -= 8

        def reset():
            return ({bytes([i]): i for i in range(clear_code)},
                    eoi_code + 1, min_code_size + 1)

        dictionary, next_code, code_size = reset()
        emit(clear_code, code_size)

        w = b''
        for byte in data:
            wc = w + bytes([byte])
            if wc in dictionary:
                w = wc
                continue
            emit(dictionary[w], code_size)
            if next_code < 4096:
                dictionary[wc] = next_code
                next_code += 1
                # The decoder adds its matching entry one step later, so widen
                # only once next_code passes the current width's capacity.
                if next_code > (1 << code_size) and code_size < 12:
                    code_size += 1
            else:
                emit(clear_code, code_size)
                dictionary, next_code, code_size = reset()
            w = bytes([byte])

        if w:
            emit(dictionary[w], code_size)
        emit(eoi_code, code_size)

        if bits:
            out.append(bit_buffer & 0xFF)
        return bytes(out)

    @staticmethod
    def _write_gif_data_blocks(data: bytearray) -> bytes:
        """Write GIF data sub-blocks (max 255 bytes each)"""
        blocks = b''
        for i in range(0, len(data), 255):
            block = data[i:i+255]
            blocks += bytes([len(block)]) + block
        blocks += bytes([0x00])  # Block terminator
        return blocks


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

    def __init__(self, data: bytes):
        self.data = data
        self.width = 0
        self.height = 0
        self.bpp = 0
        self.flags = 0
        self.palette: bytes = b''
        self.pixels: bytes = b''
        self.description = ''

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
        i += -i % 4                     # image data starts 4-byte aligned
        need = self.width * self.height
        if self.flags & self.COMPRESSED_FLAG:
            try:
                self.pixels = Team17Decompressor.decompress(d[i:], need)
            except DecompressionError:
                return False
        else:
            raw = d[i:i + need]
            if len(raw) < need:
                return False
            self.pixels = raw
        return True

    @property
    def ncolours(self) -> int:
        return len(self.palette) // 3

    def rgb_palette(self) -> bytes:
        """256-entry RGB table; colour 0 is the unstored transparent black."""
        out = bytearray(768)
        for k in range(min(self.ncolours, 255)):
            out[(k + 1) * 3:(k + 1) * 3 + 3] = self.palette[k * 3:k * 3 + 3]
        return bytes(out)


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
            # A frame with no ink still needs a non-empty box. None of the
            # reference archive's 9181 frames has a zero-area one; blank
            # frames get 1x1 at the origin, so do the same.
            left = top = 0
            right = bottom = 1
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
        # Uncompressed sprites use a different layout entirely: no stream
        # count, no stream table, and the frame offset split across two
        # fields (see SPR_FORMAT.md). Writing it is not implemented -- every
        # sprite the game ships in a .dir is compressed.
        raise NotImplementedError(
            'writing uncompressed sprites is not supported; pass compress=True')

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
                compress_img: bool, opaque: bool) -> Optional[Tuple[bytes, bool]]:
    """Produce the bytes for one archive entry.

    `base` is the path the listing points at, without any added extension.
    Returns (payload, was_encoded) or None when no source file exists.

    With `recreate` set, a sprite or image is rebuilt from its BMP whenever
    one is present, so edits to the BMP take effect. Otherwise an existing
    binary is preferred and the BMP is only a fallback.
    """
    bmp_path = base + '.bmp'
    lower = name.lower()
    is_spr = lower.endswith('.spr')
    is_img = lower.endswith('.img')

    def existing() -> Optional[Tuple[bytes, bool]]:
        if os.path.exists(base):
            with open(base, 'rb') as fh:
                return fh.read(), False
        return None

    if not (is_spr or is_img):
        return existing()            # .inf, .txt and friends are copied as-is

    if not recreate:
        found = existing()
        if found is not None:
            return found

    if not os.path.exists(bmp_path):
        return existing()

    with open(bmp_path, 'rb') as fh:
        width, height, pixels, source_palette = read_bmp(fh.read())

    if opaque and is_img:
        # Opaque images have no transparent index, so shift every colour up
        # by one to free index 0 rather than letting colour 0 vanish.
        pixels = bytes(min(p + 1, 255) for p in pixels)
        source_palette = b'\x00\x00\x00' + source_palette

    palette, remapped = build_palette(pixels, source_palette)

    if is_img:
        return encode_image(width, height, remapped, palette,
                            compress=compress_img), True

    meta = {}
    spd_path = base + '.spd'
    if os.path.exists(spd_path):
        with open(spd_path, 'r', encoding='latin-1') as fh:
            meta = read_spd(fh.read())
    frames = meta.get('frames', 1)
    cell_w = meta.get('width', width)
    cell_h = meta.get('height', height // max(frames, 1))
    return encode_sprite(cell_w, cell_h, frames, remapped, palette,
                         meta.get('flags', 1), meta.get('framerate', 0),
                         compress=compress_spr), True


def print_help():
    """Print help message"""
    print(f"wa-py-spriteHelper v{__version__}")
    print("Extract and decompress Worms Armageddon .dir files")
    print("\nUsage: wa-py-spriteHelper.py <command> [options]")
    print("\nCommands:")
    print("  extract <dir_file> [output_dir]   Extract all files from .dir")
    print("  pack <name>.dir.txt [output_dir]")
    print("                                    Build <name>.dir from a listing file")
    print("                                    --no-compress-img  store images raw")
    print("                                    --no-recreate      reuse existing .spr/.img")
    print("                                    --opaque-img       no transparent colour")
    print("  decompress <dir_file> [output_dir] [--gif]")
    print("                                    Decode sprites to raw pixels, BMP and .spd")
    print("                                    --gif also writes animated GIFs (slow)")
    print("  list <dir_file>                   List files in .dir")
    print("  version                           Show version")
    print("  help                              Show this help")


def main():
    """Main entry point"""
    if len(sys.argv) < 2:
        print_help()
        return 1

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
        failed = []

        try:
            with open(dir_file, 'rb') as f:
                for filename in sorted(reader.files.keys()):
                    if not filename.endswith('.spr'):
                        continue
                    data = reader.extract_file(f, filename)
                    if not data:
                        continue

                    sprite = SpriteFile(data)
                    if not sprite.parse():
                        failed.append(filename)
                        continue

                    sheet = sprite.render_sheet()
                    if sheet is None:
                        failed.append(filename)
                        continue

                    output_path = os.path.join(sprite_output_dir, filename)
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
                                gif_output_dir, os.path.splitext(filename)[0] + '.gif')
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

        print(f"\nDecompressed {count} sprites to {sprite_output_dir}")
        if want_gif:
            print(f"Generated {gif_count} animated GIFs in {gif_output_dir}")
        if failed:
            print(f"Could not decode {len(failed)} sprites "
                  f"(unsupported SPR layout variant): {', '.join(failed[:5])}"
                  + (' ...' if len(failed) > 5 else ''))
        return 0

    elif command == "pack":
        args = [a for a in sys.argv[2:] if not a.startswith('-')]
        opts = [a for a in sys.argv[2:] if a.startswith('-')]
        known = {'--no-compress-spr', '--no-compress-img',
                 '--no-recreate', '--opaque-img'}
        unknown = [o for o in opts if o not in known]
        if unknown:
            print(f"Error: unknown option(s): {', '.join(unknown)}")
            return 1

        compress_spr = '--no-compress-spr' not in opts
        compress_img = '--no-compress-img' not in opts
        recreate = '--no-recreate' not in opts
        opaque = '--opaque-img' in opts

        if not args:
            print("Error: pack requires a <name>.dir.txt listing file")
            return 1
        listing = args[0]
        if not listing.lower().endswith('.dir.txt'):
            print(f"Error: expected a file named <name>.dir.txt, got "
                  f"{os.path.basename(listing)}")
            return 1
        if not os.path.exists(listing):
            print(f"Error: File not found: {listing}")
            return 1

        source_dir = os.path.dirname(os.path.abspath(listing))
        stem = os.path.basename(listing)[:-len('.dir.txt')]
        out_path = os.path.join(args[1] if len(args) > 1 else source_dir,
                                stem.lower() + '.dir')

        with open(listing, 'r', encoding='latin-1') as fh:
            names = [ln.strip() for ln in fh if ln.strip()]

        writer = DirectoryWriter()
        built = reused = 0
        problems: List[str] = []
        for name in names:
            rel = name.replace('\\', os.sep)
            base = os.path.join(source_dir, rel)
            try:
                data = _pack_entry(base, name, recreate, compress_spr,
                                   compress_img, opaque)
            except Exception as exc:
                problems.append(f"{name}: {exc}")
                continue
            if data is None:
                problems.append(f"{name}: no source file found")
                continue
            payload, was_built = data
            writer.add(name, payload)
            built += was_built
            reused += not was_built

        if problems:
            print(f"Could not pack {len(problems)} of {len(names)} entries:")
            for p in problems[:10]:
                print(f"  {p}")
            if len(problems) > 10:
                print(f"  ... and {len(problems) - 10} more")
            return 1

        archive = writer.build()
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        with open(out_path, 'wb') as fh:
            fh.write(archive)

        print(f"Packed {len(names)} entries into {out_path}")
        print(f"  encoded from source images: {built}")
        print(f"  copied unchanged:           {reused}")
        print(f"  archive size:               {len(archive):,} bytes")
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
