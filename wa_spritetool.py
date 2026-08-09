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

# Upper bound used to sanity-check decoded sprite dimensions. Custom level
# themes ship full-screen backdrops (1280x370, 1024x250), so a limit tuned to
# Gfx.dir's 60x60 cells silently rejects perfectly good files.
MAX_DIM = 8192


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


class SpriteFile:
    """A Worms Armageddon sprite file (.spr).

    The layout was reverse engineered against spriteEditor's output; see
    SPR_FORMAT.md. Three compressed stream-table layouts exist; which applies
    is decided by the palette length and stream count. Uncompressed sprites
    have no stream table at all and take a separate path.

        0   "SPR\\x1A"
        4   u32  file length
        8   u16  flags        bit 0x4000 set = Team17-compressed
        10  u16  palette entry count
        12  palette, 3 bytes per entry, RGB, starting at colour 1
            u32  stream count
            Stream[] 12 bytes each -- field order differs per variant
            Sprite   u16 flags, width, height, frame count
            Frame[]  u16 data_pos, stream_selector, left, up, right, down
            stream data (offsets relative to here)

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
        if not self.is_compressed:
            try:
                return self._parse_uncompressed()
            except (struct.error, IndexError, ValueError):
                return False
        # The stream-table layout is predictable from the header: the palette
        # length decides whether positions are stored outright, and for the
        # packed layouts the stream count decides which of the two is used.
        # Verified against 2005 sprites (Gfx.dir plus 140 third-party level
        # themes) with no exceptions -- see SPR_FORMAT.md. The remaining
        # variants are still tried as a fallback so an unseen file degrades to
        # the old search rather than failing outright.
        try:
            ncol, _p, nstream = self._header()
            if ncol % 4 in (0, 1):
                order = (self._parse_positional, self._parse_cumulative,
                         self._parse_next_position)
            elif nstream <= 2:
                order = (self._parse_cumulative, self._parse_next_position,
                         self._parse_positional)
            else:
                order = (self._parse_next_position, self._parse_cumulative,
                         self._parse_positional)
        except (struct.error, IndexError, ValueError):
            order = (self._parse_positional, self._parse_cumulative,
                     self._parse_next_position)

        for variant in order:
            try:
                if variant():
                    return True
            except (struct.error, IndexError, ValueError, DecompressionError):
                continue
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
        # Custom level themes ship far larger sprites than Gfx.dir: the Coral
        # Reef backdrop is 1024x250 with 128 streams. Keep a sanity bound, but
        # a low one silently rejects legitimate files.
        if not 0 < nstream <= 4096:
            raise ValueError('implausible stream count')
        return ncol, p, nstream

    def _finish(self, ncol, p, so, blobs, sprite_size: int = 8) -> bool:
        flags, w, h, fc = struct.unpack('<HHHH', self.data[so:so + 8])
        if not (0 < w <= MAX_DIM and 0 < h <= MAX_DIM and 0 < fc <= 4096):
            return False
        ft = so + sprite_size
        if ft + fc * 12 > len(self.data):
            return False
        self.flags, self.width, self.height, self.frames = flags, w, h, fc
        # The frame rate sits in the u16 immediately before the sprite record
        # (verified against every .spd spriteEditor writes).
        self.framerate = (struct.unpack('<H', self.data[so - 2:so])[0]
                          if so >= 2 else 0)
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
        so = p + 4
        if so + 8 > len(self.data):
            return False
        _f, w, h, fc = struct.unpack('<HHHH', self.data[so:so + 8])
        if not (0 < w <= MAX_DIM and 0 < h <= MAX_DIM and 0 < fc <= 4096):
            return False
        # The frame table is aligned the same way the stream table is in the
        # compressed variant: pad the 8-byte sprite record by ncol % 4.
        sprite_size = 8 + (ncol % 4)
        ds = so + sprite_size + fc * 12
        if ds > len(self.data):
            return False
        return self._finish(ncol, p, so, [self.data[ds:]], sprite_size=sprite_size)

    def _parse_positional(self) -> bool:
        """Stream record = (position, unused, decompressed_length)."""
        ncol, p, nstream = self._header()
        q = p + 4 + (ncol % 4)
        so = q + nstream * 12 + 4
        if so + 8 > len(self.data):
            return False
        _f, w, h, fc = struct.unpack('<HHHH', self.data[so:so + 8])
        if not (0 < w <= MAX_DIM and 0 < h <= MAX_DIM and 0 < fc <= 4096):
            return False
        ds = so + 8 + fc * 12
        recs = [struct.unpack('<III', self.data[q + k * 12:q + k * 12 + 12])
                for k in range(nstream)]
        blobs = [Team17Decompressor.decompress(self.data[ds + pos:], dlen) if dlen else b''
                 for (pos, _u, dlen) in recs]
        return self._finish(ncol, p, so, blobs)

    def _parse_cumulative(self) -> bool:
        """Stream record = (unused, decompressed_length, compressed_size);
        stream positions are the running total of the compressed sizes."""
        ncol, p, nstream = self._header()
        q = p + 4 + (ncol % 4)
        so = q + nstream * 12
        if so + 8 > len(self.data):
            return False
        _f, w, h, fc = struct.unpack('<HHHH', self.data[so:so + 8])
        if not (0 < w <= MAX_DIM and 0 < h <= MAX_DIM and 0 < fc <= 4096):
            return False
        ds = so + 8 + fc * 12
        if ds > len(self.data):
            return False
        recs = [struct.unpack('<III', self.data[q + k * 12:q + k * 12 + 12])
                for k in range(nstream)]
        blobs = []
        pos = 0
        for (_u, dlen, csize) in recs:
            blobs.append(Team17Decompressor.decompress(self.data[ds + pos:], dlen)
                         if dlen else b'')
            pos += csize
        return self._finish(ncol, p, so, blobs)

    def _parse_next_position(self) -> bool:
        """Stream record = (unused, decompressed_length, position_of_NEXT stream).

        The position field is off by one: stream k begins where record k-1 says
        the next stream starts, and stream 0 begins at 0. The final record's
        position is 0 (unused). Field 1 is corroborated independently by the
        frame table -- the largest `data_pos + box area` referring to stream k
        equals record k's length exactly.
        """
        ncol, p, nstream = self._header()
        q = p + 4 + (ncol % 4)
        so = q + nstream * 12
        if so + 8 > len(self.data):
            return False
        _f, w, h, fc = struct.unpack('<HHHH', self.data[so:so + 8])
        if not (0 < w <= MAX_DIM and 0 < h <= MAX_DIM and 0 < fc <= 4096):
            return False
        ds = so + 8 + fc * 12
        if ds > len(self.data):
            return False
        recs = [struct.unpack('<III', self.data[q + k * 12:q + k * 12 + 12])
                for k in range(nstream)]
        blobs = []
        for k in range(nstream):
            pos = 0 if k == 0 else recs[k - 1][2]
            dlen = recs[k][1]
            blobs.append(Team17Decompressor.decompress(self.data[ds + pos:], dlen)
                         if dlen else b'')
        return self._finish(ncol, p, so, blobs)

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


def print_help():
    """Print help message"""
    print(f"wa-py-spriteHelper v{__version__}")
    print("Extract and decompress Worms Armageddon .dir files")
    print("\nUsage: wa-py-spriteHelper.py <command> [options]")
    print("\nCommands:")
    print("  extract <dir_file> [output_dir]   Extract all files from .dir")
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
