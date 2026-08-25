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
from typing import Dict, List, Optional, Sequence, Tuple

SETTINGS_TOML_NAME = 'settings.spritetool.toml'

#: What makes a file one of ours. Anything may come before it, so a folder
#: can hold `Paradise Ruins.spritetool.toml` beside another project's.
SETTINGS_TOML_SUFFIX = '.spritetool.toml'

# The six object placement values, in the order the game's .inf stores them.
# These mirror spritetool.SETTINGS_KEYS; duplicated so this module stands
# alone (spritetool imports this, never the reverse).
OBJECT_KEYS = ('probability', 'front', 'soil', 'collide', 'nostack', 'where')
OBJECT_DEFAULT = (5, 0, 0, 1, 1, 3)

# A sprite's geometry and playback, mirroring the .spr.spd fields.
SPRITE_KEYS = ('frames', 'width', 'height', 'framerate', 'flags')

# The tool's own bookkeeping keys under [spritetool].
TOOL_KEYS = ('created', 'borrowed', 'last_output',
             'recolour', 'compress_spr', 'force')

# The three that are a choice rather than a record, with the answer the tool
# takes when the file does not say. They belong to the terrain rather than to
# the machine: a folder that has to be forced, or whose art is refitted every
# pack, is that way wherever it is opened, so they travel in the file with
# everything else about it.
OPTION_DEFAULTS = {'recolour': False, 'compress_spr': True, 'force': False}


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


def options_of(settings: Optional['TerrainSettings']) -> Dict[str, bool]:
    """The three per-project choices, defaulted for whatever the file omits.

    One place, so the window and the command line cannot drift about what a
    folder that says nothing is asking for. A value that is not a bool -- a
    hand edit of `force = 2` -- is ignored in favour of the default rather
    than counted as true, since "anything non-zero" is not what the file
    means to say.
    """
    out = dict(OPTION_DEFAULTS)
    if settings is not None:
        for key in OPTION_DEFAULTS:
            value = settings.tool.get(key)
            if isinstance(value, bool):
                out[key] = value
    return out


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
    if text in ('true', 'false'):
        # Before the int attempt, and spelled TOML's way rather than
        # Python's. _fmt_value has always written these -- bool is checked
        # ahead of int there because it is a subclass -- so without this the
        # writer emits a value its own reader calls a problem.
        return text == 'true', ''
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


def candidates(folder: str) -> List[str]:
    """Every settings file in `folder`, best-known name first.

    A folder may hold more than one: two projects over the same art, kept
    apart by name. `settings.spritetool.toml` is still what is written for a
    folder that has none, so it sorts first and a folder with only that one
    behaves as it always did. The rest follow alphabetically, which is stable
    and is the order a chooser should list them in.
    """
    try:
        here = os.listdir(folder)
    except OSError:
        return []
    found = [f for f in here
             if f.lower().endswith(SETTINGS_TOML_SUFFIX)
             and os.path.isfile(os.path.join(folder, f))]
    found.sort(key=lambda f: (f != SETTINGS_TOML_NAME, f.lower()))
    return [os.path.join(folder, f) for f in found]


def load_path(path: str) -> Optional[TerrainSettings]:
    """Read one settings file, or None when it is not there.

    The path-taking half of `load`. Everything that reads a settings file
    goes through here, so a project that knows its own filename does not have
    to be a folder with exactly one.
    """
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


def load(folder: str) -> Optional[TerrainSettings]:
    """Read a folder's settings file, or None when it has none.

    None is the thing worth knowing: it means the folder has not been set up
    as a spritetool terrain yet, so legacy sources (if any) should be read for
    migration instead. A file that exists but does not read comes back as a
    TerrainSettings carrying its problems.

    Where a folder holds several, this takes the first `candidates` names --
    which is `settings.spritetool.toml` if it is there. Choosing between them
    is a question for whoever has someone to ask; this is the answer for
    everything that just needs the folder's settings.
    """
    found = candidates(folder)
    return load_path(found[0]) if found else None


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


def save_path(path: str, settings: TerrainSettings) -> str:
    """Write one settings file. Returns the path written."""
    with open(path, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(to_toml(settings))
    return path


def save(folder: str, settings: TerrainSettings) -> str:
    """Write a folder's settings file. Returns the path written.

    Back where it was read from when the folder already has one, so a project
    named something else is not silently duplicated under the default name;
    `settings.spritetool.toml` when there is nothing there yet.
    """
    found = candidates(folder)
    return save_path(found[0] if found
                     else os.path.join(folder, SETTINGS_TOML_NAME), settings)


# ------------------------------------------------------------------ project --

class Project:
    """One terrain's settings, held in memory until saved.

    The file was the model before this: every edit wrote through immediately,
    so there was nothing to save and nothing to lose -- but also three
    separate dirty flags in the window, one per table, each guarded by hand.
    A project is the one thing that is edited and the one thing that is
    dirty.

    That trade is worth naming: a crash used to cost nothing because the file
    was always current, and now costs whatever was not saved. It is the
    ordinary bargain of a document editor, and the reason Save, Save As and a
    warning on close come with it.

    `path` is the file it came from, or None for a project that has never
    been saved -- which is what a folder with no settings file gives.
    """

    def __init__(self, folder: str, path: Optional[str] = None,
                 settings: Optional[TerrainSettings] = None):
        self.folder = folder
        self.path = path
        settings = settings if settings is not None else TerrainSettings()
        self.objects = settings.objects
        self.sprites = settings.sprites
        self.tool = settings.tool
        self.problems = list(settings.problems)
        #: Settings describing art that is no longer in the folder. Kept, so
        #: nothing is dropped behind the author's back, and written out of
        #: the file by the next save.
        self.orphans: Dict[str, List[str]] = {'objects': [], 'sprites': []}
        self._dirty = False

    # -- state ---------------------------------------------------------

    @property
    def dirty(self) -> bool:
        return self._dirty

    def touch(self) -> None:
        """Mark the project as differing from what is on disk."""
        self._dirty = True

    @property
    def name(self) -> str:
        """What to call this project. The filename, less our suffix."""
        if not self.path:
            return os.path.basename(self.folder.rstrip(os.sep)) or 'terrain'
        base = os.path.basename(self.path)
        if base.lower().endswith(SETTINGS_TOML_SUFFIX):
            base = base[:-len(SETTINGS_TOML_SUFFIX)]
        return base or 'settings'

    def options(self) -> Dict[str, bool]:
        return options_of(self.as_settings())

    def as_settings(self) -> TerrainSettings:
        """The plain record this project would be written as."""
        return TerrainSettings(objects=self.objects, sprites=self.sprites,
                               tool=self.tool, problems=list(self.problems))

    # -- opening and saving --------------------------------------------

    @classmethod
    def open(cls, path: str) -> 'Project':
        """Read one settings file into a project."""
        settings = load_path(path) or TerrainSettings()
        return cls(os.path.dirname(os.path.abspath(path)), path, settings)

    @classmethod
    def for_folder(cls, folder: str,
                   path: Optional[str] = None) -> 'Project':
        """A project for `folder`: the named file, its only one, or a new one.

        `path` picks between several; without it the first candidate wins,
        which is `settings.spritetool.toml` where that exists. A folder with
        no settings file at all gives a project with no path -- unsaved, not
        empty-and-saved -- so the first save has somewhere to ask about.
        """
        if path is None:
            found = candidates(folder)
            path = found[0] if found else None
        if path is None:
            return cls(folder)
        return cls.open(path)

    def save(self, path: Optional[str] = None) -> str:
        """Write the project. `path` given is Save As, and is owned after.

        Orphaned settings go here rather than at open: the file is not
        rewritten behind the author's back, and what is dropped is dropped at
        a moment they asked for.
        """
        target = path or self.path
        if target is None:
            target = os.path.join(self.folder, SETTINGS_TOML_NAME)
        self.drop_orphans()
        save_path(target, self.as_settings())
        self.path = target
        self._dirty = False
        return target

    # -- stale data ----------------------------------------------------

    def find_orphans(self, object_stems: Sequence[str],
                     sprite_names: Sequence[str]) -> List[str]:
        """Note settings whose art is gone. Returns them, as descriptions.

        Not removed here. An author opening a project to look at it should
        not have it edited by the looking; the entries stay, the project is
        marked dirty, and the next save is what drops them.
        """
        have_objects = {s.lower() for s in object_stems}
        have_sprites = {s.lower() for s in sprite_names}
        self.orphans = {
            'objects': sorted(k for k in self.objects
                              if k.lower() not in have_objects),
            'sprites': sorted(k for k in self.sprites
                              if k.lower() not in have_sprites),
        }
        said = ([f'object {k}' for k in self.orphans['objects']]
                + [f'sprite {k}' for k in self.orphans['sprites']])
        if said:
            self._dirty = True
        return said

    def drop_orphans(self) -> int:
        """Forget the noted orphans. Returns how many went."""
        gone = 0
        for key in self.orphans['objects']:
            gone += self.objects.pop(key, None) is not None
        for key in self.orphans['sprites']:
            gone += self.sprites.pop(key, None) is not None
        self.orphans = {'objects': [], 'sprites': []}
        return gone
