#!/usr/bin/env python3
"""A terrain's settings, kept in one TOML file: settings.spritetool.toml.

This is the single source of truth for everything editable about a terrain
folder -- each object's placement, each terrain sprite's geometry, and the
tool's own bookkeeping. It replaces the SpriteEditor-era formats the tool used
to write for hand-editing (object_settings.txt, per-object .inf, and the
.spr.spd sidecar), which are now read only to migrate them.

The file is TOML, but the project rule is stdlib + Pillow only and Python
3.8+, and the standard library's TOML reader (tomllib) is 3.11+ and read-only.
So this module carries its own reader and writer, scoped to exactly the
structure it emits: nested tables, string keys, integer / quoted-string /
string-array values, and # comments. We control both ends and only ever
produce that subset, so a full TOML implementation is neither needed nor
wanted -- anything outside the subset is reported as a problem, never raised,
the same contract parse_settings keeps.

Layout (flat tables; the tool's own keys under [spritetool]):

    [spritetool]
    created = "2026-08-24"
    borrowed = ["text", "bridge"]
    last_output = "/abs/path"

    [object.floor-1]
    probability = 5
    front = 0
    soil = 0
    collide = 1
    nostack = 1
    where = 3

    [sprite.debris]
    frames = 120
    width = 1024
    height = 410
    framerate = 0
    flags = 1
"""

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

SETTINGS_TOML_NAME = 'settings.spritetool.toml'

# The six object placement values, in the order the game's .inf stores them.
# These mirror spritetool.SETTINGS_KEYS; duplicated so this module stands
# alone (spritetool imports this, never the reverse).
OBJECT_KEYS = ('probability', 'front', 'soil', 'collide', 'nostack', 'where')
OBJECT_DEFAULT = (5, 0, 0, 1, 1, 3)

# A sprite's geometry and playback, mirroring the .spr.spd fields.
SPRITE_KEYS = ('frames', 'width', 'height', 'framerate', 'flags')

# The tool's own bookkeeping keys under [spritetool].
TOOL_KEYS = ('created', 'borrowed', 'last_output')


@dataclass
class TerrainSettings:
    """Everything editable about a terrain folder, plus read problems.

    objects: object stem -> six placement values (OBJECT_KEYS order).
    sprites: sprite entry name -> {SPRITE_KEYS key: int}.
    tool:    [spritetool] bookkeeping (created/borrowed/last_output).
    problems: anything in the file that did not read, as strings.
    """
    objects: Dict[str, List[int]] = field(default_factory=dict)
    sprites: Dict[str, Dict[str, int]] = field(default_factory=dict)
    tool: Dict[str, object] = field(default_factory=dict)
    problems: List[str] = field(default_factory=list)


# ------------------------------------------------------------------- reader --

def _unescape(text: str) -> str:
    r"""Undo the \\ and \" _fmt_value writes. The inverse of its escaping.

    A lone backslash before anything else is left as it stands: Worms sprite
    names are full of them (gfx0\cloudm), and a hand-written file is likelier
    to mean the character than an escape sequence we do not emit.
    """
    out = []
    i = 0
    while i < len(text):
        if text[i] == '\\' and i + 1 < len(text) and text[i + 1] in '\\"':
            out.append(text[i + 1])
            i += 2
        else:
            out.append(text[i])
            i += 1
    return ''.join(out)


def _parse_value(text: str):
    """One TOML value: integer, "quoted string", or ["array", "of", "strings"].

    Returns (value, error). error is '' on success.
    """
    text = text.strip()
    if not text:
        return None, 'empty value'
    if text.startswith('"'):
        if len(text) < 2 or not text.endswith('"'):
            return None, f'unterminated string {text!r}'
        return _unescape(text[1:-1]), ''
    if text.startswith('['):
        if not text.endswith(']'):
            return None, f'unterminated array {text!r}'
        inner = text[1:-1].strip()
        if not inner:
            return [], ''
        parts = [p.strip() for p in inner.split(',')]
        out = []
        for p in parts:
            if not (p.startswith('"') and p.endswith('"') and len(p) >= 2):
                return None, f'array entries must be quoted strings: {p!r}'
            out.append(_unescape(p[1:-1]))
        return out, ''
    try:
        return int(text), ''
    except ValueError:
        return None, f'not a number, string, or array: {text!r}'


def _strip_comment(line: str) -> str:
    """Drop a trailing # comment, unless the # is inside a quoted string."""
    out = []
    in_str = False
    for ch in line:
        if ch == '"':
            in_str = not in_str
        if ch == '#' and not in_str:
            break
        out.append(ch)
    return ''.join(out)


def parse_toml(text: str) -> Tuple[Dict[str, Dict[str, object]], List[str]]:
    """Read our TOML subset into {table_path: {key: value}}.

    Table paths are dotted strings ('spritetool', 'object.floor-1',
    'sprite.debris'). Returns (tables, problems); never raises.
    """
    tables: Dict[str, Dict[str, object]] = {}
    problems: List[str] = []
    current: Optional[str] = None
    for n, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        # A table header runs to its closing bracket: an object called
        # "hash#name" is a legal filename, and treating its # as a comment
        # would truncate the header and drop the object's settings silently.
        # Only what follows the ] can be a comment.
        if stripped.startswith('[') and ']' in stripped:
            close = stripped.rindex(']')
            line = stripped[:close + 1]
            trailing = _strip_comment(stripped[close + 1:]).strip()
            if trailing:
                problems.append(f'line {n}: trailing text after table header: '
                                f'{stripped!r}')
                current = None
                continue
        else:
            line = _strip_comment(raw).strip()
        if not line:
            continue
        if line.startswith('['):
            if not line.endswith(']'):
                problems.append(f'line {n}: malformed table header {raw.strip()!r}')
                current = None
                continue
            current = line[1:-1].strip()
            if not current:
                problems.append(f'line {n}: empty table header')
                current = None
                continue
            tables.setdefault(current, {})
            continue
        if '=' not in line:
            problems.append(f'line {n}: not a table header or key = value: '
                            f'{raw.strip()!r}')
            continue
        if current is None:
            problems.append(f'line {n}: {line!r} before any table header')
            continue
        key, _, value = line.partition('=')
        key = key.strip()
        parsed, err = _parse_value(value)
        if err:
            problems.append(f'line {n}: {err}')
            continue
        tables[current][key] = parsed
    return tables, problems


def load(folder: str) -> Optional[TerrainSettings]:
    """Read settings.spritetool.toml, or None when the folder has none.

    None is the thing worth knowing: it means the folder has not been set up
    as a spritetool terrain yet, so legacy sources (if any) should be read for
    migration instead. A file that exists but does not read comes back as a
    TerrainSettings carrying its problems.
    """
    path = os.path.join(folder, SETTINGS_TOML_NAME)
    if not os.path.exists(path):
        return None
    with open(path, 'r', encoding='utf-8') as fh:
        tables, problems = parse_toml(fh.read())
    settings = TerrainSettings(problems=problems)
    for table, entries in tables.items():
        if table == 'spritetool':
            settings.tool.update(entries)
        elif table.startswith('object.'):
            stem = table[len('object.'):]
            values = list(OBJECT_DEFAULT)
            for i, key in enumerate(OBJECT_KEYS):
                v = entries.get(key)
                if isinstance(v, int):
                    values[i] = v
                elif v is not None:
                    settings.problems.append(
                        f'[{table}] {key}: expected a number, got {v!r}')
            settings.objects[stem] = values
        elif table.startswith('sprite.'):
            name = table[len('sprite.'):]
            rec: Dict[str, int] = {}
            for key in SPRITE_KEYS:
                v = entries.get(key)
                if isinstance(v, int):
                    rec[key] = v
                elif v is not None:
                    settings.problems.append(
                        f'[{table}] {key}: expected a number, got {v!r}')
            settings.sprites[name] = rec
        else:
            settings.problems.append(f'unknown table [{table}]')
    return settings


# ------------------------------------------------------------------- writer --

def _fmt_value(value: object) -> str:
    if isinstance(value, bool):        # before int: bool is an int subclass
        return 'true' if value else 'false'
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'
    if isinstance(value, (list, tuple)):
        return '[' + ', '.join(_fmt_value(v) for v in value) + ']'
    raise ValueError(f'cannot write {type(value).__name__} to TOML: {value!r}')


def to_toml(settings: TerrainSettings) -> str:
    """Serialise a TerrainSettings, deterministically.

    [spritetool] first, then objects and sprites alphabetical, so the file
    agrees with the alphabetical order the archive is packed in.
    """
    lines = ['# Written by spritetool. Editable; the GUI and CLI both read '
             'and write this file.', '']
    lines.append('[spritetool]')
    for key in TOOL_KEYS:
        if key in settings.tool:
            lines.append(f'{key} = {_fmt_value(settings.tool[key])}')
    lines.append('')
    for stem in sorted(settings.objects, key=str.lower):
        lines.append(f'[object.{stem}]')
        for key, value in zip(OBJECT_KEYS, settings.objects[stem]):
            lines.append(f'{key} = {_fmt_value(value)}')
        lines.append('')
    for name in sorted(settings.sprites, key=str.lower):
        lines.append(f'[sprite.{name}]')
        for key in SPRITE_KEYS:
            if key in settings.sprites[name]:
                lines.append(f'{key} = {_fmt_value(settings.sprites[name][key])}')
        lines.append('')
    return '\n'.join(lines)


def save(folder: str, settings: TerrainSettings) -> str:
    """Write settings.spritetool.toml. Returns the path written."""
    path = os.path.join(folder, SETTINGS_TOML_NAME)
    with open(path, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(to_toml(settings))
    return path
