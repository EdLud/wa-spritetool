# Sprite file (.SPR) — working notes

Reverse engineered against spriteEditor's output. **These are working notes,
not a finished specification** — parts are unresolved and marked as such.

Ground truth: `unpack from spriteEditor/*.bmp`, compared byte-for-byte against
rendered pixel data. Current coverage: all 770 sprites in `Gfx.dir` decode, 769
verified exact and 1 where the reference itself is corrupt. All 638 in
`Gfx0.dir` decode; they have no independent reference, but 600 of the 626
sprites common to both files agree in shape with their verified compressed
counterpart (the rest are genuinely different artwork).

When comparing against a BMP, remember its scanlines are padded to 4-byte
boundaries — sprites whose width is not a multiple of 4 (10, 30, 50, 158 …)
will appear to mismatch if the padding is not stripped first.

## Layout

```
offset  size  field
0       4     "SPR\x1A"
4       4     file length
8       2     flags        bit 0x4000 set = Team17-compressed
10      2     palette entry count
12      3*n   palette, RGB per entry, starting at colour 1
        4     stream count                    ] compressed
        12*n  stream table (three variants)   ] only
        ...   padding
        2     frame rate (immediately before the sprite record)
        2     sprite flags
        2     width
        2     height
        2     frame count
        12*n  frame table
        ...   stream data (offsets relative to here)
```

## Palette

`count` entries of 3 bytes in **RGB** order, starting at offset 12.

**Colour 0 is not stored.** It is the transparent background and is always
black; the table on disk begins at colour 1. A renderer must therefore shift
entries up by one, leaving index 0 black. Getting this wrong tints the whole
sprite and turns the background a solid colour.

Compressed sprites carry a palette of only the colours that sprite uses
(10–89 entries observed). Uncompressed sprites embed the directory's shared
palette instead — every sprite in `Gfx0.dir` carries the same 89-entry table,
identical to `Palette_gfx0_90cols.ACT` minus its leading black entry.

Because the two directories use different palettes, the *same* sprite has
different pixel indices in `Gfx.dir` and `Gfx0.dir`. Comparing indices across
directories is meaningless; compare resolved RGB instead.

## Compression flag

Bit `0x4000` of the u16 at offset 8.

| Source | Value | Meaning |
|---|---|---|
| `Gfx.dir` | `0xC008` (all 770) | compressed |
| `Gfx0.dir` | `0x8008` (all 638) | uncompressed |

Uncompressed sprites have **no stream count and no stream table** — the palette
is followed directly by the sprite record, and frame data is raw cropped pixels
laid out contiguously.

## Stream table (compressed only)

12 bytes per record, beginning at `palette_end + 4 + (ncol % 4)`.

**Three field layouts occur.** Which one applies is determined by the header:

| condition | layout |
|---|---|
| `ncol % 4` is 0 or 1 | A |
| `ncol % 4` is 2 or 3, and `stream count` <= 2 | B |
| `ncol % 4` is 2 or 3, and `stream count` >= 3 | C |

Verified across 2005 compressed sprites — all 770 in `Gfx.dir` plus 1235 from
140 independently authored level themes — with no exceptions. The split is
absolutely clean: among the packed layouts, B only ever occurs with 1-2 streams
(969 files) and C only with 3 or more (143 files).

In every layout, field 1 (the middle u32) is the decompressed length; the
variants differ only in how the stream's position is expressed.

**Variant A** — `(position, unused, decompressed_length)`. Two bytes of padding
follow the table before the sprite record. Field 2 holds the length here rather
than field 1.

Note this is *almost* the BNK Stream struct, which the sprite-bank docs give as
`(Compressed Position, Decompressed Length, Unknown)` — same three fields, but
BNK puts the length second and the unused slot last. In A the latter two are
swapped. The unused slot is zero in all 1040 A-layout stream records, matching
BNK's "Unknown, most likely useless".

**Variant B** — `(unused, decompressed_length, compressed_size)`. Positions are
not stored; each stream begins at the running total of the preceding
compressed sizes.

**Variant C** — `(unused, decompressed_length, position_of_next_stream)`. The
position field is off by one: stream *k* begins where record *k-1* points, and
stream 0 begins at 0. The final record's position field is 0.

All three are the same 12-byte record with one always-zero slot in a different
place: it is field 1 in A and field 0 in B and C. The slot is zero in **every**
stream record across the shipped archives (4202 A-records and 2974 B/C-records,
checked exhaustively) — so it carries no data in any file the game ships, and
what it was reserved for remains unknown.

**Why two layouts?** Not alignment. The `ncol % 4` pad after the stream count
already brings the stream table to a 4-byte boundary for *every* value of
`ncol % 4` (verified across all shipped sprites), and both the A tail (+2) and
the B/C tail (−2) then land the sprite record at an offset congruent to 2 mod
4 — which all 3039 compressed sprites satisfy. So either tail would work for
any palette length; the `ncol % 4` split is not forced by alignment. The two
forms are simply different encoder conventions, and the correlation with
`ncol % 4` reflects which tool wrote the file, not a structural requirement.

Field 1 can be corroborated without decoding anything: for each stream, the
largest `data_pos + box_area` among the frames referring to it equals that
stream's decompressed length exactly. This is what identified the field
originally and is a useful sanity check when adding new variants.

Stream payloads use the Team17 LZ77 routine (see `Team17 compression.md`).

## Uncompressed sprites

When bit `0x4000` is clear there is no stream count and no stream table. The
layout is:

```
12          palette, 3 bytes per entry
p           4 bytes -- frame rate at p+2
p+4         sprite record (flags, width, height, frame count)
p+4+8+pad   frame table, pad = ncol % 4
            frame pixels, contiguous, one implicit stream
```

Frame records have the same shape as in the compressed form, but there is only
one contiguous block of pixels and `data_pos` is a 16-bit field, so it wraps
for sprites larger than 64 KB. The stream-selector field carries the high bits:

```
frame_offset = stream_selector * 65536 + data_pos
```

This was found by brute-forcing the base offset for the frames that decoded
wrongly in `cloudm.spr`; the answer came out at exactly 65536.

The sum of all frame box areas equals the remaining data length (within a byte
or two of trailing padding), which is a reliable way to confirm the frame table
is correctly located.

## Frame table

12 bytes per frame:

```
0   2   data position within the stream
2   2   stream selector   -- stream index = value / 256
4   2   left
6   2   up
8   2   right
10  2   down
```

Each frame stores only its cropped bounding box, not a full cell. This is what
makes the format compact: `canon.spr` holds 125 frames of 60×60 in 2,876 bytes.

Frames and rows are both in natural order: frame record `i` is animation frame
`i`, and `up`/`down` are measured downward from the top of the cell. No
reversal is needed anywhere.

This is worth stating explicitly because it is easy to conclude otherwise. The
reference BMPs *appear* to be bottom-up because BMP itself stores scanlines
bottom-up; a decoder that also reverses frame order and row order produces
output that matches a naive byte-for-byte BMP comparison while being upside
down on screen. Compare what a viewer renders, not the raw pixel bytes.

The selector's `/256` scaling is now understood. The two leading u16 fields of
the frame record are really one little-endian 32-bit word, and the stream
number sits in its **top byte**: `selector / 256` is `(high16 >> 8)`. In every
compressed sprite the low byte of the selector word is 0 (all observed values
are multiples of 256 — 0, 256, 512, … up to 37376), so bits 16–23 of the word
are always 0 and only the top byte carries the stream index.

This unifies with the uncompressed form. There the same 32-bit word is a full
frame offset — `selector * 65536 + data_pos` — and the selector takes small
values (0–20 observed), the high word of offsets that run past 64 KB. One
field, two readings: a 32-bit offset when there is a single implicit stream,
or a 16-bit offset plus a stream number in the top byte when streams are
split. Verified across 8611 sprites: 0 compressed frames with a non-multiple
of 256, and 5572 uncompressed sprites whose frames all land at
`selector * 65536 + data_pos` within the blob.

The BNK sprite-bank format documents its Frame struct as `int16 Stream` then
`int16 Data Position`, i.e. a plain stream number in the first field. That does
**not** apply to `.spr`: reading it that way yields `stream = 256` on a
six-stream file. In `.spr` the two fields are ordered `data_pos` then
`stream << 8`. The formats are clearly related but not identical here, so the
BNK spec is a useful guide rather than an authority for `.spr`.

### At most 128 streams

A sprite may not have more than **128 streams**. The selector holds
`stream << 8`, so stream 128 encodes as 32768 — which the game reads as a
*signed* 16-bit value, i.e. −32768. Every frame in stream 128 or beyond thus
resolves to a negative stream index and a wild pointer, and the game dies with
an access violation. This was hit in practice with a dense 400×400×160
`debris.spr`: each frame overflowed `MAX_DATA_POS` alone and took its own
stream, so 160 frames needed 160 streams.

Nothing shipped comes near the limit — the largest are the 128-frame
`front.spr` files, and `Gfx.dir` tops out at 25 streams (`cdrom.spr`) — which
is the game's own tools observing the same bound. The encoder refuses to write
more than 128 streams; the levers are fewer frames, a smaller cell, or sparser
frames, all of which cut the stream count.

## Sprite flags

The u16 at the start of the sprite record is the playback mode, matching the
`flags` field spriteEditor writes to `.spd`:

| Value | Meaning | Count in `Gfx.dir` |
|---|---|---|
| 0 | play once and stop | 503 |
| 1 | loop continuously | 156 |
| 2 | play forwards then backwards, then stop | 0 |
| 3 | ping-pong continuously | 111 |

This matters for any exporter. A ping-pong sprite's last frame is not close to
its first — `cloudm.spr` grows monotonically from 3160 to 3500 ink pixels — so
playing frames 0..n and looping straight back produces a visible jump. GIF has
no ping-pong mode, so the return leg (frames n-1 down to 1) has to be written
out as real frames.

This tool materialises the return leg for flags 2 and 3, but always sets the
GIF to loop forever regardless of flag — a preview that halts on its last frame
is awkward to browse. The true playback mode is preserved in the `.spd` file.

## Frame rate

The u16 immediately *preceding* the sprite record holds the frame rate that
spriteEditor writes to `.spd`. Most sprites store 0, meaning "use the game
default"; `airjetb.spr` stores 25. Reading it makes the generated `.spd` files
byte-identical to spriteEditor's for all 770 sprites.

When writing a GIF, note that GIF delays are in centiseconds: a delay under
about 20ms rounds to 0-1cs, and browsers clamp that to ~100ms, which makes
playback erratic rather than fast. Clamp to 20ms minimum, and use ~50ms when
the sprite declares no rate.

Two edge cases in the frame table, both encountered in real files:

- A **zero-area box** (`(0,0)-(0,0)`) with `data_pos` at the end of the stream
  is a legitimate empty terminator frame, not corruption. `flame1`, `spangle*`
  and `wteldsv*` all end with one.
- A box may be **wider than the sprite cell** — `circle25.spr` frame 7 declares
  52 pixels of width in a 50-pixel cell. It should be clipped. spriteEditor
  does not clip and emits uninitialised memory for that frame.

## Unresolved

- **Which encoders wrote which layout, and why the unused u32 exists.** The
  stream-table layout is *not* selected by alignment — the `ncol % 4` pad
  already aligns the table for every palette length, and either tail lands the
  sprite record at the 2-mod-4 offset every shipped sprite uses (see "Stream
  table"). So the A/B/C split reflects different encoder builds, but which
  tools produced which form, and what the always-zero u32 was reserved for,
  are unknown. The slot is zero in every shipped record (7176 checked), so a
  decoder may ignore it.
- Whether the 3 bytes of slack the decompressor allocates are genuinely needed.
  For sprites they are not: across all 7176 shipped sprite streams, every one
  ends exactly at its decompressed length and none writes into the slack. The
  slack originates with the W:A `Training*.img` maps the compression page
  mentions, a different file type; whether any of those overruns is untested
  here.

## Resolved since these notes began

- **The `/256` scaling on the stream selector** — the two leading u16s are one
  32-bit word and the stream number is its top byte; see "Frame table". The
  same word is a full 32-bit offset in uncompressed sprites. The field is read
  as signed, which is what caps a sprite at 128 streams (see "At most 128
  streams").
- **The 3-byte decompressor slack for sprites** — not needed; no shipped
  sprite stream overruns (above).
