# test

One archive from each game, and what this tool makes of it. If a change to the
decoder alters any of these, it changed how a real file is read.

```
test/
  wa/            Worms Armageddon
  wwp online/    Online Worms (2002-03-23 build)
  wwp aqua/      World Party Aqua
```

Each holds `Water.dir` as the game ships it, and `decompressed/` as this tool
writes it: a `.spr` per sprite, a `.bmp` of its frames stacked, a `.spd` of its
geometry, and the listing that rebuilds the archive.

`Water.dir` was chosen because all three games have one, they are small, and
they are almost entirely sprites -- which is the part of the format with no
public specification and the most room to get wrong.

## Checking against them

```bash
python3 spritetool.py decompress "test/wa/Water.dir" /tmp/out
diff -r "test/wa/decompressed" /tmp/out
```

Silence means the decoder still reads that game the same way. All three
reproduce byte for byte as committed.

The three are not interchangeable. Aqua writes `LND\x1B` where the others
write `LND\x1A`, its sprite bank is arranged differently, and Online Worms
sits between the two -- so a change that looks right against one can still be
wrong against another. That is the reason for keeping all three.
