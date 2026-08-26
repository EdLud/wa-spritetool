"""The window: drop a folder in, press Pack.

Deliberately thin. Everything it knows about terrains it asks `spritetool`
for, and the packing itself happens in another process (see `job`/`bridge`),
so this file is layout, wiring, and the few decisions a window has that a
command line does not -- chiefly that a question becomes a dialog and that
the folder's own files are worth showing before anything is built.
"""

import contextlib
import os
import sys

from PySide6.QtCore import QEvent, QSettings, Qt, QSize
from PySide6.QtGui import QAction, QFont, QKeySequence, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFrame, QHBoxLayout,
    QHeaderView, QLabel, QMainWindow, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QSpinBox, QSplitter, QTabWidget, QTableWidget,
    QInputDialog, QTableWidgetItem, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget)

from .bridge import PackJob
from .job import ANSWER_CANCEL, ANSWER_NO, ANSWER_YES

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import spritetool as st                                        # noqa: E402
import settings_toml                                          # noqa: E402


APP_NAME = 'spritetool'


def _human(n):
    for unit in ('bytes', 'KB', 'MB'):
        if n < 1024 or unit == 'MB':
            return f'{n:,.0f} {unit}' if unit == 'bytes' else f'{n:,.1f} {unit}'
        n /= 1024


def _hidden(name):
    """Whether a file is the filesystem's business rather than the author's.

    .DS_Store is the one that actually turns up -- the Finder writes one into
    every folder it has been looked at in -- and listing it as part of someone's
    terrain is noise. The packer ignores these too, so showing them would also
    be untrue about what is going to be packed.
    """
    return os.path.basename(name).startswith('.')


def _elide(path, keep=52):
    """A path short enough for a label, with the end kept.

    The end is the part that says which folder this is; the middle is usually
    somebody's home directory and says nothing.
    """
    path = os.path.expanduser(path)
    home = os.path.expanduser('~')
    if path.startswith(home):
        path = '~' + path[len(home):]
    return path if len(path) <= keep else '...' + path[-(keep - 3):]


#: How many recent projects are remembered. Ten is the most a Cmd+number
#: shortcut can reach -- Cmd+0 would be the tenth, and a list longer than the
#: keys that open it is a list whose tail nobody uses.
RECENT_LIMIT = 10


class Recents:
    """The projects opened before, newest first, kept between runs.

    A list of settings-file paths rather than folders: a folder may hold
    several projects, and reopening the folder would lose which one was
    being edited. QSettings puts them where each platform keeps such things
    -- the registry on Windows, a plist on macOS -- so nothing of ours has
    to be written beside the terrains.
    """

    KEY = 'recent/projects'

    def __init__(self, store=None):
        self._store = store if store is not None else QSettings(APP_NAME,
                                                                APP_NAME)

    def paths(self):
        """The remembered projects that are still there.

        Checked on the way out rather than pruned on the way in: a project
        on a drive that is not mounted right now has not been forgotten, it
        is merely absent, and it comes back when the drive does.
        """
        got = self._store.value(self.KEY) or []
        if isinstance(got, str):
            # A single value comes back as a bare string on some backends.
            got = [got]
        return [p for p in got if isinstance(p, str) and p]

    def add(self, path):
        """Put `path` at the front, without letting it appear twice."""
        path = os.path.abspath(path)
        kept = [p for p in self.paths() if os.path.abspath(p) != path]
        self._store.setValue(self.KEY, [path] + kept[:RECENT_LIMIT - 1])

    def forget(self, path):
        path = os.path.abspath(path)
        self._store.setValue(
            self.KEY, [p for p in self.paths() if os.path.abspath(p) != path])


class DropZone(QFrame):
    """Where a folder or an archive lands. Also a button, for people who do
    not drag.

    Two things can be dropped and they lead opposite ways: a folder is a
    terrain to build, an archive is one to take apart. Both are accepted here
    rather than in two places, because from the author's side it is one
    gesture -- this is the thing, do what it needs.
    """

    #: What an archive is called. `.dir` is the only one extract and
    #: decompress read, and the only one worth accepting for them.
    ARCHIVE_EXTS = ('.dir',)

    #: Pictures that are not in an archive: a terrain's TEXT.img beside its
    #: Level.dir, an .spr pulled out with `extract`. Several may be dropped
    #: at once, which an archive may not -- one archive is a question about
    #: where it should go, a handful of pictures is one answer for all.
    PICTURE_EXTS = ('.img', '.spr')

    @classmethod
    def is_archive(cls, path):
        return (os.path.isfile(path)
                and path.lower().endswith(cls.ARCHIVE_EXTS))

    @classmethod
    def is_picture(cls, path):
        return (os.path.isfile(path)
                and path.lower().endswith(cls.PICTURE_EXTS))

    @classmethod
    def is_project(cls, path):
        """A settings file, which opens the terrain it describes.

        Dropping one is how a folder with two projects is opened on the one
        that is wanted, without being asked afterwards which it was.
        """
        return (os.path.isfile(path) and path.lower().endswith(
            settings_toml.SETTINGS_TOML_SUFFIX.lower()))

    def __init__(self, on_folder, on_archive=None, on_pictures=None,
                 on_project=None):
        super().__init__()
        self._on_folder = on_folder
        self._on_archive = on_archive
        self._on_pictures = on_pictures
        self._on_project = on_project
        self.setAcceptDrops(True)
        self.setObjectName('dropzone')
        self.setMinimumHeight(96)

        self._label = QLabel(self.IDLE_TEXT)
        self._label.setAlignment(Qt.AlignCenter)
        self._label.setObjectName('droplabel')
        self._label.setTextFormat(Qt.RichText)
        self._label.setToolTip(self.IDLE_TIP)
        self.setToolTip(self.IDLE_TIP)

        browse = QPushButton('Choose folder...')
        browse.setFixedWidth(150)
        browse.clicked.connect(self._browse)

        box = QVBoxLayout(self)
        box.setSpacing(8)
        box.addWidget(self._label)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(browse)
        row.addStretch(1)
        box.addLayout(row)

    #: Said in the label when nothing has been dropped, and as the tooltip
    #: wherever the zone is described. One sentence per thing that can be
    #: dropped, because the two do opposite jobs.
    IDLE_TEXT = ('Drop a folder to set-up a terrain, a project file to '
                 'open it, a .dir archive to extract it, or .img/.spr '
                 'pictures to decode.')
    IDLE_TIP = ('A folder is prepared as a terrain project.\n'
                f'A {settings_toml.SETTINGS_TOML_SUFFIX} file opens the '
                'terrain it describes, on that project rather than another '
                'in the same folder.\n'
                'A .dir archive is decompressed and its contents written to '
                'a folder.\n'
                'Loose .img or .spr pictures are decoded to BMP; several at '
                'once is fine.')

    def show_folder(self, folder, project=None):
        """Say what is open: the project by name, over the folder it is in.

        The project is the thing being edited -- a folder may hold several,
        and which one is loaded decides what every table shows -- so it is
        the line in large type, with the folder underneath. A folder not set
        up yet has no project to name, and falls back to its own name.
        """
        if not folder:
            self._label.setText(self.IDLE_TEXT)
            self._label.setToolTip(self.IDLE_TIP)
            return
        parent, name = os.path.split(folder.rstrip(os.sep))
        # The build folder is usually several levels down and the full path
        # crowds out the part that identifies it, so the parent is elided.
        under = os.path.join(_elide(parent), name) if project else _elide(parent)
        self._label.setText(
            f'<div style="font-size:16px;font-weight:600">'
            f'{project or name}</div>'
            f'<div style="font-size:11px;color:gray">{under}</div>')
        self._label.setToolTip(folder)

    def _browse(self):
        folder = QFileDialog.getExistingDirectory(self, 'Choose a build folder')
        if folder:
            self._on_folder(folder)

    # One thing at a time, and only a folder or an archive. Anything else is
    # a mistake worth refusing at the door rather than reporting later: the
    # zone simply does not light up, which says no before the mouse is let go.
    def _accepts(self, urls):
        paths = [u.toLocalFile() for u in urls]
        if not paths:
            return None
        # Several at once only for pictures. A folder is a terrain and an
        # archive is a question about where to unpack it; neither means
        # anything in a handful.
        if all(self.is_picture(p) for p in paths) \
                and self._on_pictures is not None:
            return 'pictures'
        if len(paths) != 1:
            return None
        path = paths[0]
        if os.path.isdir(path):
            return 'folder'
        if self.is_project(path) and self._on_project is not None:
            return 'project'
        if self.is_archive(path) and self._on_archive is not None:
            return 'archive'
        return None

    def dragEnterEvent(self, event):
        if self._accepts(event.mimeData().urls()):
            event.acceptProposedAction()
            self.setProperty('hot', True)
            self._restyle()

    def dragLeaveEvent(self, event):
        self.setProperty('hot', False)
        self._restyle()

    def dropEvent(self, event):
        self.setProperty('hot', False)
        self._restyle()
        urls = event.mimeData().urls()
        kind = self._accepts(urls)
        if kind is None:
            return
        paths = [u.toLocalFile() for u in urls]
        if kind == 'pictures':
            self._on_pictures(paths)
        elif kind == 'archive':
            self._on_archive(paths[0])
        elif kind == 'project':
            self._on_project(paths[0])
        else:
            self._on_folder(paths[0])

    def _restyle(self):
        self.style().unpolish(self)
        self.style().polish(self)


class _Row(QTreeWidgetItem):
    """A file row that sorts by the number behind a column, not its text.

    Without this "1,021 bytes" sorts before "83.8 KB" and a colour count of
    100 before one of 9, which makes the sortable header worse than none.
    """

    def __lt__(self, other):
        col = self.treeWidget().sortColumn() if self.treeWidget() else 0
        mine, theirs = self.data(col, Qt.UserRole), other.data(col, Qt.UserRole)
        if mine is None or theirs is None:
            return self.text(col).lower() < other.text(col).lower()
        return mine < theirs


def _in_budget(name):
    """Whether a picture's colours count against the terrain's 112.

    Two in a build folder do not. The icon is not an archive entry and the
    engine does not aggregate it -- archive_problems and the palette sheet
    both skip it -- and palette.png is something the tool wrote about the
    colours rather than any of them. Counting either inflates the total
    against the one number it is meant to be compared with.
    """
    low = os.path.basename(name).lower()
    if low == st.PALETTE_NAME.lower():
        return False
    return not any(low == f'icon{suffix}'
                   for suffix in ('.png', '.img.png', '.bmp', '.img.bmp'))


def _picture_colours(path):
    """The distinct drawn colours in a picture, or None if it is not one.

    The set rather than its size, because the caller wants both: how many
    this picture draws, and how many the folder draws between them. Those are
    different numbers -- the budget counts a colour once however many pictures
    use it -- and reading every file twice to get them would double the wait.

    Counted the way the terrain's budget counts: unique RGB among the pixels
    actually drawn, index 0 being the transparent one, so a PNG and an indexed
    BMP give comparable numbers.
    """
    low = path.lower()
    try:
        with open(path, 'rb') as fh:
            blob = fh.read()
        if low.endswith('.png'):
            return set(st.png_colour_counts(blob))
        if low.endswith('.bmp'):
            _w, _h, pixels, palette = st.read_bmp(blob)
            return {tuple(palette[v * 3:v * 3 + 3])
                    for v in set(pixels) - {0}
                    if (v + 1) * 3 <= len(palette)}
    except Exception:
        return None
    return None


def _connect_steps(spin, handler):
    """Call `handler(delta)` when a spin box is stepped, not merely changed.

    QAbstractSpinBox.stepBy is what the arrows and the up/down keys go
    through, and it is handed the number of steps -- which is exactly the
    thing a multi-row edit needs and the thing valueChanged does not carry.
    Wrapping it is more honest than watching values and inferring a
    difference after the fact.
    """
    original = spin.stepBy

    def stepBy(steps):
        before = spin.value()
        original(steps)
        moved = spin.value() - before
        if moved:
            handler(moved)

    spin.stepBy = stepBy
    return spin


def _centred(widget):
    """A cell widget that sits in the middle of its cell."""
    holder = QWidget()
    lay = QHBoxLayout(holder)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setAlignment(Qt.AlignCenter)
    lay.addWidget(widget)
    return holder


class _MultiEdit:
    """Applies one row's edit to every other selected row.

    Selecting five objects and setting them all to `floor` should be one
    action, not five. A checkbox or a choice is copied across as it stands;
    a number is applied as the *step* that was just taken, so weights of 4
    and 6 nudged up become 5 and 7 rather than both becoming 5. Their
    relationship is usually the thing the author arranged on purpose.

    Reentrancy matters: setting the other rows fires their own signals, which
    would come back here and try to spread again. `_spreading` stops that.
    """

    def __init__(self, *args, **kwargs):
        # Cooperative: the mixin sits ahead of QTableWidget in the MRO, so
        # the table's own arguments pass through rather than stopping here.
        super().__init__(*args, **kwargs)
        self._spreading = False
        #: While a checkbox drag is in progress: the column being dragged and
        #: the value the first box was set to, which every box dragged over is
        #: set to as well. None when no drag is running.
        self._dragging = None
        #: Cells already dealt with in this drag, so crossing one twice --
        #: which a wavering pointer does constantly -- does not toggle it back.
        self._dragged = set()
        # A drag is watched in two places, because no single widget sees all
        # of it. A cell widget sits on top of the viewport and swallows the
        # press outright -- the viewport never hears it -- so the press is
        # taken from the box. The movement cannot come from the box: it is
        # about 18 pixels square in a 30-pixel row, so the pointer leaves it
        # almost immediately, and the moves that matter arrive at the
        # viewport instead. Watching only one of the two is why earlier
        # versions of this did nothing.
        self.viewport().installEventFilter(self)

    def _watch_box(self, box, row, col):
        """Let a checkbox start a drag down its column.

        Dragging down a column of tick boxes is the obvious way to switch a
        run of objects off, and doing it one click at a time is the kind of
        work a window is supposed to save. The first box decides: whatever it
        is about to become is what the rest become, so a drag never toggles
        some on and others off depending on where they started.
        """
        box.installEventFilter(self)
        box.setProperty('cellRow', row)
        box.setProperty('cellCol', col)

    def eventFilter(self, watched, event):
        kind = event.type()
        if kind == QEvent.MouseButtonPress:
            # From the box itself: the viewport under it never sees this.
            row = watched.property('cellRow')
            col = watched.property('cellCol')
            if row is not None and col is not None:
                self._press_drag(int(row), int(col), watched)
        elif kind == QEvent.MouseMove and self._dragging is not None:
            # From the viewport, where the pointer still is once it has left
            # the small box it started on. A Leave on the box means only that
            # the pointer moved on, so it is not treated as the end of
            # anything -- taking it for one was the original bug.
            if watched is self.viewport():
                self._apply_drag(event.position().toPoint())
            else:
                self._apply_drag(
                    watched.mapTo(self.viewport(), event.position().toPoint()))
        elif kind == QEvent.MouseButtonRelease:
            dragged = self._dragging is not None and self._dragged
            self._dragging = None
            self._dragged = set()
            if dragged and isinstance(watched, QCheckBox):
                # The box the drag ended over already holds the drag's value.
                # Letting the release through would toggle it a second time,
                # undoing the one row the author finished on -- so the release
                # is eaten. A plain click never gets here: nothing was dragged.
                watched.setDown(False)
                return True
        return super().eventFilter(watched, event)

    def _include_changed(self, row, col, state):
        """A checkbox moved: spread it across the selection.

        Boxes the drag itself set are skipped: their value came from the drag,
        not from an author picking rows, so there is nothing to spread.
        """
        if self._dragging is not None and (row, col) in self._dragged:
            self._touch()
            return
        self._spread(row, col)

    def _press_drag(self, row, col, box):
        """Arm a drag from the press, before the box under it has toggled.

        A QCheckBox toggles on *release*, not on press -- and a drag ends its
        release somewhere else entirely, so the box it started on never
        toggles and never emits its signal. Waiting on that signal to learn
        what the drag carries therefore waits forever. The value is worked out
        here instead: the box is about to become the opposite of what it reads
        now, and that is what every box dragged over becomes.

        The row is only armed, not set. A press that turns out to be a plain
        click still ends on this box and toggles it in the ordinary way; were
        it set here as well it would be toggled twice and land back where it
        started. `_apply_drag` sets it as soon as the pointer moves off, which
        is the moment the gesture is known to be a drag rather than a click.
        """
        self._dragging = (col, not box.isChecked(), row)
        self._dragged = set()

    def _box_at(self, row, col):
        """The checkbox in a cell, or None where the cell holds something else."""
        holder = self.cellWidget(row, col)
        return holder.findChild(QCheckBox) if holder is not None else None

    def _apply_drag(self, pos):
        """Set whatever box the pointer is over to the drag's value.

        `pos` is already in viewport coordinates, which is what `indexAt`
        wants -- the viewport is where the gesture is watched.
        """
        col, value, first = self._dragging
        if first is not None:
            # The pointer has moved, so this is a drag: the box it started on
            # is set here rather than waiting for a release that will land
            # somewhere else. Marked done so its own release cannot toggle it
            # back.
            self._dragging = (col, value, None)
            self._set_box(first, col, value)
        row = self.indexAt(pos).row()
        if row < 0:
            return
        self._set_box(row, col, value)

    def _set_box(self, row, col, value):
        """Set one box to the drag's value, once."""
        if (row, col) in self._dragged:
            return
        box = self._box_at(row, col)
        if box is None:
            return
        self._dragged.add((row, col))
        if box.isChecked() != value:
            box.setChecked(value)

    def _rows_with(self, row):
        """The selected rows, when `row` is one of them. Otherwise just it."""
        chosen = {i.row() for i in self.selectedIndexes()}
        return sorted(chosen) if row in chosen and len(chosen) > 1 else [row]

    def _spread(self, row, col, step=None):
        """Apply row `row`'s column `col` to the rest of the selection.

        `step` is how far a number moved, where the caller knows -- a spin
        box reports that itself, so nothing here has to remember the value it
        held a moment ago and guess.
        """
        if self._spreading:
            return
        source = self.cellWidget(row, col)
        rows = self._rows_with(row)
        if len(rows) > 1:
            self._spreading = True
            try:
                for other in rows:
                    if other == row:
                        continue
                    self._copy_cell(source, other, col, step)
            finally:
                self._spreading = False
        self._touch()

    def _copy_cell(self, source, row, col, step):
        target = self.cellWidget(row, col)
        if target is None:
            return
        if isinstance(source, QSpinBox) and isinstance(target, QSpinBox):
            # By the step, not to the value: two rows set apart on purpose
            # stay set apart.
            if step:
                target.setValue(target.value() + step)
            return
        if isinstance(source, QComboBox) and isinstance(target, QComboBox):
            target.setCurrentIndex(source.currentIndex())
            return
        box = source.findChild(QCheckBox) if source else None
        other = target.findChild(QCheckBox) if target else None
        if box is not None and other is not None:
            other.setChecked(box.isChecked())


class ObjectTable(_MultiEdit, QTableWidget):
    """The six settings the guide gives every object, one row each.

    The lowest-risk useful thing a window can do that a text editor cannot:
    the values are small integers with meanings, so they get spin boxes and
    named choices instead of a column of digits.
    """

    COLUMNS = ('Object', 'Include', 'Weight', 'In front', 'Soil', 'Collision',
               'No stacking', 'Location')
    WHERE = ('side (left)', 'side (right)', 'ceiling', 'floor')

    #: Which column holds what, so the row-building and the reading agree
    #: without counting on their fingers.
    COL_NAME, COL_INCLUDE, COL_WEIGHT = 0, 1, 2
    COL_FLAGS = (3, 4, 5, 6)          # front, soil, collide, nostack
    COL_WHERE = 7

    def __init__(self):
        super().__init__(0, len(self.COLUMNS))
        self.setHorizontalHeaderLabels(self.COLUMNS)
        self.verticalHeader().setVisible(False)
        self.setAlternatingRowColors(True)
        self.setSelectionBehavior(QTableWidget.SelectRows)
        # Several rows at once, so one edit can settle a whole group.
        self.setSelectionMode(QTableWidget.ExtendedSelection)
        head = self.horizontalHeader()
        head.setSectionResizeMode(0, QHeaderView.Stretch)
        for i in range(1, len(self.COLUMNS)):
            head.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        self._dirty = False
        #: Why the table is empty, when it is empty for a reason worth saying.
        self.problems = []

    @property
    def dirty(self):
        return self._dirty

    def load(self, folder, keep=None):
        """Read the folder's objects and whatever settings they have.

        `keep` is the settings to use instead of the file's: what the table
        already holds, handed back on a refresh. The folder is re-read for
        its pictures -- that is what a refresh is for -- but the settings are
        the session's, so an edit made here is not undone by a file that has
        not been saved to yet. Editing the file by hand mid-session is not a
        thing the window tries to notice.
        """
        self.setRowCount(0)
        self._dirty = False
        self.problems = []
        try:
            _names, objects, _notes = st.scan_terrain(folder)
        except Exception as exc:
            self.problems = [str(exc)]
            return 0

        # settings.spritetool.toml is the source of truth; the SpriteEditor-
        # era files are read only to show what would be migrated. The stems
        # the scan hands back and the keys the legacy file used are brought
        # together through st.object_stem, the same normalisation the packer
        # uses -- matching on the raw name instead silently misses every
        # object, and the table would show defaults it then saves over the
        # author's real settings.
        settings = {}
        excluded = {}
        toml = keep if keep is not None else settings_toml.load(folder)
        if toml is not None:
            excluded = dict(toml.excluded)
            if toml.problems:
                self.problems = toml.problems
                return 0
            settings = {stem.lower(): list(values)
                        for stem, values in toml.objects.items()}
        else:
            combined = os.path.join(folder, st.SETTINGS_NAME)
            if os.path.exists(combined):
                with open(combined, encoding='latin-1') as fh:
                    by_picture, problems = st.parse_settings(fh.read())
                if problems:
                    self.problems = problems
                    return 0
                for picture, values in by_picture.items():
                    settings[st.object_stem(picture)] = values
        for stem in objects:
            if stem.lower() in settings:
                continue
            loose = os.path.join(folder, f'{stem}.inf')
            if os.path.exists(loose):
                with open(loose, encoding='latin-1') as fh:
                    values = st.parse_inf(fh.read())
                if values:
                    settings[stem.lower()] = values

        self.setRowCount(len(objects))
        for row, stem in enumerate(objects):
            values = settings.get(stem.lower(), list(st.DEFAULT_INF))
            name = QTableWidgetItem(stem)
            name.setFlags(name.flags() & ~Qt.ItemIsEditable)
            self.setItem(row, self.COL_NAME, name)

            keep = QCheckBox()
            keep.setChecked(not excluded.get(stem.lower(), False))
            keep.setToolTip('Off leaves this object out of the next pack. '
                            'The picture stays in the folder.')
            keep.stateChanged.connect(
                lambda state, r=row: self._include_changed(
                    r, self.COL_INCLUDE, state))
            self.setCellWidget(row, self.COL_INCLUDE, _centred(keep))
            self._watch_box(keep, row, self.COL_INCLUDE)

            weight = QSpinBox()
            weight.setRange(1, 10)
            weight.setValue(values[0])
            # Two signals, because they mean different things. A press on
            # the arrows is a step, and every selected row should move by it.
            # Typing a number outright is not a step -- there is nothing to
            # move the others by -- so it only marks the table dirty.
            weight.setKeyboardTracking(False)
            weight.valueChanged.connect(self._touch)
            _connect_steps(weight,
                           lambda by, r=row: self._spread(r, self.COL_WEIGHT,
                                                          by))
            self.setCellWidget(row, self.COL_WEIGHT, weight)

            for col, idx in zip(self.COL_FLAGS, (1, 2, 3, 4)):
                box = QCheckBox()
                box.setChecked(bool(values[idx]))
                box.stateChanged.connect(
                    lambda state, r=row, c=col: self._include_changed(
                        r, c, state))
                self.setCellWidget(row, col, _centred(box))
                self._watch_box(box, row, col)

            where = QComboBox()
            where.addItems(self.WHERE)
            where.setCurrentIndex(min(values[5], 3))
            where.currentIndexChanged.connect(
                lambda index, r=row: self._spread(r, self.COL_WHERE))
            self.setCellWidget(row, self.COL_WHERE, where)

        self._dirty = False
        return len(objects)

    def _touch(self, *_):
        self._dirty = True
        window = self.window()
        if hasattr(window, '_sync_save_actions'):
            window._sync_save_actions()

    def values(self):
        """[(stem, six values)], in the table's order."""
        out = []
        for row in range(self.rowCount()):
            stem = self.item(row, self.COL_NAME).text()
            vals = [self.cellWidget(row, self.COL_WEIGHT).value()]
            for col in self.COL_FLAGS:
                box = self.cellWidget(row, col).findChild(QCheckBox)
                vals.append(1 if box.isChecked() else 0)
            vals.append(self.cellWidget(row, self.COL_WHERE).currentIndex())
            out.append((stem, vals))
        return out

    def excluded(self):
        """{stem: True} for every object switched off. Only the offs.

        A folder that has never switched anything off writes no table at all,
        so the file says nothing rather than listing every object as kept.
        """
        out = {}
        for row in range(self.rowCount()):
            box = self.cellWidget(row, self.COL_INCLUDE).findChild(QCheckBox)
            if box is not None and not box.isChecked():
                out[self.item(row, self.COL_NAME).text().lower()] = True
        return out

    def save(self, folder, path=None):
        """Write the settings file. Returns what it wrote to.

        `path` is the project being edited, where the folder holds more than
        one; without it the folder's own is found as before. Saving by folder
        alone writes whichever file sorts first, which for a folder with two
        projects is not necessarily the one on screen.

        The TOML is keyed by the object's stem and written deterministically,
        so a save only shows as a change where a value actually moved.
        """
        toml = (settings_toml.load_path(path) if path
                else settings_toml.load(folder)) or settings_toml.TerrainSettings()
        toml.problems = []
        toml.objects = {stem: list(values) for stem, values in self.values()}
        # Only this table's names, so a sprite switched off on the other tab
        # is not forgotten by an object save.
        mine = {self.item(r, self.COL_NAME).text().lower()
                for r in range(self.rowCount())}
        toml.excluded = {k: v for k, v in toml.excluded.items()
                         if k not in mine}
        toml.excluded.update(self.excluded())
        written = (settings_toml.save_path(path, toml) if path
                   else settings_toml.save(folder, toml))
        self._dirty = False
        return written


class SpriteTable(_MultiEdit, QTableWidget):
    """The sprite record: what a sheet of frames says about itself.

    A sheet is one tall picture and says nothing about how it is cut up, so
    the frame count and cell size are data the terrain has to carry --
    settings.spritetool.toml carries it, and until now nothing showed it. A
    wrong record is not a crash: the art comes out sliced in the wrong
    places, or the pack refuses on the first sprite it reaches and says
    nothing about the rest.

    So every sprite is listed with its record and its sheet side by side, and
    anything that does not add up is said in the row rather than saved for
    the build.

    Playback is a named choice rather than a number to look up. The frame
    count and cell size are editable, since they are what an author has to
    get right and a sheet says nothing about how it is cut up; the row stays
    red until they agree with it.

    Two things are read but not shown. `framerate` does nothing for debris
    and is ignored elsewhere too, so a column of zeroes would be a question
    the author cannot usefully answer. Where a record came from is the
    tool's own bookkeeping, not a setting -- it decided nothing an author
    could act on, and took a column to say so. Both are written back
    untouched: hidden, not dropped.
    """

    COLUMNS = ('Sprite', 'Include', 'Frames', 'Cell width', 'Cell height',
               'Sheet', 'Playback')

    COL_NAME, COL_INCLUDE = 0, 1
    COL_FRAMES, COL_CELL_W, COL_CELL_H = 2, 3, 4
    COL_SHEET, COL_PLAYBACK = 5, 6

    #: flags, from the terrain guide. The index is the value.
    PLAYBACK = ('play once and stop',
                'loop',
                'forwards then backwards, stop',
                'ping pong')

    def __init__(self):
        super().__init__(0, len(self.COLUMNS))
        self.setHorizontalHeaderLabels(self.COLUMNS)
        self.verticalHeader().setVisible(False)
        self.setAlternatingRowColors(True)
        self.setSelectionBehavior(QTableWidget.SelectRows)
        self.setSelectionMode(QTableWidget.ExtendedSelection)
        self.setEditTriggers(QTableWidget.NoEditTriggers)
        head = self.horizontalHeader()
        head.setSectionResizeMode(0, QHeaderView.Stretch)
        for i in range(1, len(self.COLUMNS)):
            head.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        self.problems = []
        self._dirty = False
        #: The colour a row is when its numbers add up. Taken from the table
        #: rather than named, so it follows a dark theme as well as a light.
        self._plain = self.palette().text()
        #: The record as read, by sprite name. Kept so a save writes back the
        #: fields the table does not show -- framerate above all -- rather
        #: than dropping them because they were not on screen.
        self._records = {}

    @property
    def dirty(self):
        return self._dirty

    def load(self, folder, keep=None):
        """Fill from `folder`. Returns the number of sprites found.

        `keep` is the settings to use instead of the file's -- see
        ObjectTable.load. The sheets are measured again; the records are the
        session's.
        """
        self.setRowCount(0)
        self.problems = []
        self._records = {}
        settled = keep if keep is not None else settings_toml.load(folder)
        excluded = dict(settled.excluded) if settled else {}
        try:
            # The session's records, not the file's: sprite_records reads
            # the settings file when given nothing, which on a refresh would
            # undo a playback change that has not been saved yet.
            rows = st.sprite_records(folder, settled)
        except Exception as exc:
            self.problems = [str(exc)]
            self._dirty = False
            return 0

        self.setRowCount(len(rows))
        for r, row in enumerate(rows):
            name = str(row['name'])
            self._records[name] = row
            size = row['size']
            cell = ('--' if row['width'] is None or row['height'] is None
                    else f"{row['width']}x{row['height']}")
            sheet = '--' if size is None else f'{size[0]}x{size[1]}'
            cells = {self.COL_NAME: name,
                     self.COL_SHEET: sheet}
            for c, text in cells.items():
                item = QTableWidgetItem(text)
                if c:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                item.setToolTip(str(row['sheet']))
                self.setItem(r, c, item)

            # Frames and the cell size are the record, and the record is what
            # an author has to get right -- a sheet says nothing about how it
            # is cut up. So they are edited here rather than in the file, and
            # the row says whether the numbers currently add up.
            for col, key, top in ((self.COL_FRAMES, 'frames', 9999),
                                  (self.COL_CELL_W, 'width', 9999),
                                  (self.COL_CELL_H, 'height', 99999)):
                spin = QSpinBox()
                spin.setRange(0, top)
                spin.setValue(int(row[key] or 0))
                spin.setToolTip(
                    'Frames stacked down the sheet' if key == 'frames'
                    else f"Each frame's {key}")
                spin.valueChanged.connect(
                    lambda value, rr=r: self._recheck(rr))
                self.setCellWidget(r, col, spin)

            play = QComboBox()
            play.addItems(self.PLAYBACK)
            flags = row['flags']
            if flags is None or not 0 <= int(flags) < len(self.PLAYBACK):
                # No record, or a value the guide does not describe. Shown as
                # itself rather than rounded into a meaning it may not have.
                play.addItem('--' if flags is None else f'unknown ({flags})')
                play.setCurrentIndex(len(self.PLAYBACK))
            else:
                play.setCurrentIndex(int(flags))
            play.currentIndexChanged.connect(
                lambda index, rr=r: self._spread(rr, self.COL_PLAYBACK))
            self.setCellWidget(r, self.COL_PLAYBACK, play)

            keep = QCheckBox()
            keep.setChecked(not excluded.get(name.lower(), False))
            keep.setToolTip('Off leaves this sprite out of the next pack. '
                            'The picture stays in the folder.')
            keep.stateChanged.connect(
                lambda state, rr=r: self._include_changed(
                    rr, self.COL_INCLUDE, state))
            self.setCellWidget(r, self.COL_INCLUDE, _centred(keep))
            self._watch_box(keep, r, self.COL_INCLUDE)

            # Judged from the widgets, so what is shown and what is
            # complained about cannot disagree.
            problem = self._recheck(r)
            if problem:
                self.problems.append(f'{name}: {problem}')
        self._dirty = False
        return len(rows)

    def _recheck(self, row):
        """Re-judge one row against its sheet, and colour it accordingly.

        Live rather than at pack time: a frame count that does not divide the
        sheet is the mistake this tab exists to catch, and an author typing a
        number should see it stop being wrong as they get it right. The row
        stays red until the arithmetic works.
        """
        record = self._records.get(self.item(row, self.COL_NAME).text())
        size = record.get('size') if record else None
        frames = self.cellWidget(row, self.COL_FRAMES).value()
        cell_w = self.cellWidget(row, self.COL_CELL_W).value()
        cell_h = self.cellWidget(row, self.COL_CELL_H).value()
        problem = ('' if size is None
                   else st.sprite_geometry_problem(frames, cell_w, cell_h,
                                                   size))
        self._paint(row, problem)
        self._touch()
        return problem

    def _paint(self, row, problem):
        """Red across the row while its numbers do not add up.

        The whole row, not the column at fault: a frame count and a sheet
        disagree with each other, and colouring one of them says the other is
        right.
        """
        for col in range(self.columnCount()):
            item = self.item(row, col)
            if item is None:
                continue
            item.setForeground(Qt.red if problem else self._plain)
            if problem:
                item.setToolTip(problem)
        for col in (self.COL_FRAMES, self.COL_CELL_W, self.COL_CELL_H):
            widget = self.cellWidget(row, col)
            if widget is not None:
                widget.setStyleSheet('color: red;' if problem else '')
                if problem:
                    widget.setToolTip(problem)

    def _touch(self, *_):
        self._dirty = True
        window = self.window()
        if hasattr(window, '_sync_save_actions'):
            window._sync_save_actions()

    def save(self, folder, path=None):
        """Write the playback values back. Returns the path, or None.

        `path` is the project being edited -- see ObjectTable.save.

        Only sprites whose record the TOML already owns are written: a
        folder still carrying .spr.spd sidecars is one the author has not
        converted, and quietly starting a TOML for it from this table would
        leave two files disagreeing about the same sprite.
        """
        toml = settings_toml.load_path(path) if path else settings_toml.load(folder)
        if toml is None:
            self._dirty = False
            return None
        toml.problems = []
        for r in range(self.rowCount()):
            name = self.item(r, self.COL_NAME).text()
            if name not in toml.sprites:
                continue
            # The geometry the author typed, whether or not it adds up:
            # saving what is shown is the honest thing, and the row is
            # already red about it. The packer refuses on it either way, so
            # nothing worse gets built than would have been.
            record = toml.sprites[name]
            record['frames'] = self.cellWidget(r, self.COL_FRAMES).value()
            record['width'] = self.cellWidget(r, self.COL_CELL_W).value()
            record['height'] = self.cellWidget(r, self.COL_CELL_H).value()
            widget = self.cellWidget(r, self.COL_PLAYBACK)
            idx = widget.currentIndex()
            if idx >= len(self.PLAYBACK):
                continue                # the unknown value, left as it was
            record['flags'] = idx
        mine = {self.item(r, self.COL_NAME).text().lower()
                for r in range(self.rowCount())}
        toml.excluded = {k: v for k, v in toml.excluded.items()
                         if k not in mine}
        for r in range(self.rowCount()):
            box = self.cellWidget(r, self.COL_INCLUDE).findChild(QCheckBox)
            if box is not None and not box.isChecked():
                toml.excluded[self.item(r, self.COL_NAME).text().lower()] = True
        written = (settings_toml.save_path(path, toml) if path
                   else settings_toml.save(folder, toml))
        self._dirty = False
        return written


class Window(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1060, 720)
        self._folder = None
        #: True while the Options menu is being ticked from a file, so the
        #: toggled signal does not write the value straight back.
        self._loading_options = False
        #: Which settings file the window is editing, where the
        #: folder holds several. None means its only one, or none.
        self._project_path = None
        self._out_dir = None
        self._job = None
        #: Questions settled before packing, by key -- see _offer_setup.
        self._answers = {}

        #: The projects opened before, for the Recents menu. Built before
        #: the menus, which read it as they are put together.
        self._recents = Recents()

        self._drop = DropZone(self.set_folder, self.take_archive_apart,
                              self.decode_pictures, self.open_project)
        self._pack = QPushButton('Pack to Level.dir')
        self._pack.setObjectName('primary')
        self._pack.setEnabled(False)
        self._pack.setMinimumHeight(40)
        self._pack.clicked.connect(self.start_pack)

        self._out_label = QLabel('No output folder chosen')
        self._out_label.setObjectName('muted')
        out_btn = QPushButton('Output...')
        out_btn.clicked.connect(self._choose_out)

        # Not shown. How many processes to pack with is a real setting, but it
        # is the tool's business rather than the author's: nobody drawing a
        # terrain has an opinion about it, and a control saying "auto" beside
        # the folder they just dropped only invites the question. Kept as a
        # value so the pack still has one, and so somewhere to put it can be
        # found later without unpicking anything.
        self._jobs = QSpinBox()
        self._jobs.setRange(0, 64)
        self._jobs.setValue(0)              # auto

        self._progress = QProgressBar()
        self._progress.setRange(0, 0)
        self._progress.setVisible(False)

        self._files = QTreeWidget()
        self._files.setHeaderLabels(['File', 'Size', 'Colours'])
        self._files.setRootIsDecorated(False)
        self._files.setAlternatingRowColors(True)
        self._files.setSortingEnabled(True)
        self._files.sortByColumn(0, Qt.AscendingOrder)
        head = self._files.header()
        head.setSectionResizeMode(0, QHeaderView.Stretch)
        head.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        head.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        for col in (1, 2):
            self._files.headerItem().setTextAlignment(col, Qt.AlignRight)
        #: Bumped on every load, so a colour count still running for the last
        #: folder knows to stop rather than write into the new one's rows.
        self._load_token = 0

        # No Save button on the tab. File > Save (Cmd+S) writes both tables,
        # and a button per tab suggested each was saved on its own -- which
        # was never true, and is less true now that one project holds them.
        self._objects = ObjectTable()
        obj_page = QWidget()
        obj_box = QVBoxLayout(obj_page)
        obj_box.setContentsMargins(0, 0, 0, 0)
        obj_box.addWidget(self._objects)

        self._palette = QLabel('Pack once with the palette sheet to see it.')
        self._palette.setAlignment(Qt.AlignCenter)
        self._palette.setObjectName('muted')

        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setFont(QFont('Menlo', 11))
        # self._log.setPlaceholderText(
        #     'What the packer says appears here.\n\n'
        #     'The same notes the command line prints: colours counted, art '
        #     'refitted, objects widened, anything the terrain guide would '
        #     'complain about.')

        self._changed = QTreeWidget()
        self._changed.setHeaderLabels(['What packing changed in your folder'])
        self._changed.setRootIsDecorated(False)

        self._sprites = SpriteTable()
        spr_page = QWidget()
        spr_box = QVBoxLayout(spr_page)
        spr_box.setContentsMargins(0, 0, 0, 0)
        spr_box.addWidget(self._sprites)
        self._spr_page = spr_page

        tabs = QTabWidget()
        tabs.addTab(self._files, 'Folder')
        self._obj_page = obj_page
        tabs.addTab(obj_page, 'Objects')
        tabs.addTab(spr_page, 'Sprites')
        tabs.addTab(self._palette, 'Palette')
        # The Changes tab is not shown. The widget is still built and still
        # filled after a pack -- everything that writes to it goes on working
        # -- it simply has no tab of its own. Qt gives indexOf a widget that
        # is in no tab bar as -1, and setTabText(-1, ...) does nothing, so
        # the line that renames it is harmless rather than needing a guard.
        # Put it back by uncommenting this.
        # tabs.addTab(self._changed, 'Changes')
        self._tabs = tabs

        left = QWidget()
        lbox = QVBoxLayout(left)
        lbox.setContentsMargins(16, 16, 8, 16)
        lbox.setSpacing(12)
        lbox.addWidget(self._drop)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(out_btn)
        lbox.addLayout(row)
        lbox.addWidget(self._out_label)
        lbox.addWidget(tabs, 1)
        lbox.addWidget(self._progress)
        lbox.addWidget(self._pack)

        right = QWidget()
        rbox = QVBoxLayout(right)
        rbox.setContentsMargins(8, 16, 16, 16)
        rbox.addWidget(QLabel('Log'))
        rbox.addWidget(self._log, 1)

        split = QSplitter()
        split.addWidget(left)
        split.addWidget(right)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        self.setCentralWidget(split)
        self.statusBar().showMessage('Drop a build folder to begin')
        self._build_menus()

    def _build_menus(self):
        """The menu bar. Qt moves this to the system bar on macOS.

        There was none at all before, which on macOS leaves the app showing
        only the items the system supplies and looks half-finished. Refresh
        is what it is for now; it is also the natural home for anything later
        that does not deserve a button of its own.
        """
        # File first, as everywhere else. Save writes the tables that have
        # unsaved edits; Save As names the project and owns the new name
        # afterwards, so a folder can hold two settings files over the same
        # art and this is how the second one comes to exist.
        filemenu = self.menuBar().addMenu('&File')

        # Open comes first: it is how a session starts when the terrain is
        # one already set up, which after the first sitting is most of them.
        open_act = QAction('&Open terrain project...', self)
        open_act.setShortcut(QKeySequence.Open)              # Cmd+O / Ctrl+O
        open_act.setStatusTip('Open a saved terrain project and the folder '
                              'it describes')
        open_act.setToolTip(open_act.statusTip())
        open_act.triggered.connect(lambda: self.open_project())
        filemenu.addAction(open_act)

        # Recents is a submenu rather than a run of items in File, so the
        # numbers that open them read as one list and File stays short.
        self._recent_menu = filemenu.addMenu('Open &Recent')
        self._rebuild_recents()
        filemenu.addSeparator()

        save = QAction('&Save', self)
        save.setShortcut(QKeySequence.Save)                  # Cmd+S / Ctrl+S
        save.setStatusTip('Write the object and sprite tables to the '
                          'settings file')
        save.setToolTip(save.statusTip())
        save.triggered.connect(self.save_project)
        filemenu.addAction(save)
        self._save_action = save

        save_as = QAction('Save &As...', self)
        save_as.setShortcut(QKeySequence('Ctrl+Shift+S'))    # Cmd+Shift+S
        save_as.setStatusTip('Write the settings to a file of your own '
                             'naming, and keep working in it')
        save_as.setToolTip(save_as.statusTip())
        save_as.triggered.connect(self.save_project_as)
        filemenu.addAction(save_as)
        self._save_as_action = save_as

        view = self.menuBar().addMenu('&View')
        refresh = QAction('&Refresh', self)
        # Both, because the two habits differ: F5 is what Windows and Linux
        # reach for, Ctrl+R what a Mac user does -- and Qt spells Ctrl as the
        # Command key there, so one string covers both platforms.
        # QKeySequence.Refresh is not enough on its own: it resolves to F5
        # everywhere, macOS included.
        refresh.setShortcuts([QKeySequence('F5'), QKeySequence('Ctrl+R')])
        refresh.setStatusTip('Read the folder again')
        refresh.triggered.connect(lambda: self.refresh(announce=True))
        view.addAction(refresh)
        self._refresh_action = refresh

        # Per-project, not per-machine: a terrain that has to be forced, or
        # whose art is refitted every pack, is that way wherever it is
        # opened, so these live in the folder's settings file beside
        # everything else about it. Disabled until a folder is open, because
        # without one there is nothing for them to belong to.
        options = self.menuBar().addMenu('&Options')
        self._option_actions = {}
        for key, label, tip in (
                ('recolour', 'Automatic re&colour on pack',
                 'Fit every picture to one palette when packing, without '
                 'asking each time'),
                ('compress_spr', 'Compress &sprites',
                 'Store sprites compressed, as the game ships them'),
                ('force', '&Force',
                 'Write the archive even when it would not load')):
            act = QAction(label, self)
            act.setCheckable(True)
            act.setStatusTip(tip)
            act.setToolTip(tip)
            act.setEnabled(False)
            act.toggled.connect(
                lambda on, k=key: self._set_option(k, on))
            options.addAction(act)
            self._option_actions[key] = act

    def _set_option(self, key, on):
        """Record one per-project choice, and write it where it belongs."""
        if self._loading_options or not self._folder:
            return
        toml = settings_toml.load(self._folder)
        if toml is None or toml.problems:
            # Nothing to write into, or a file we could not read. Saying so
            # is better than writing a fresh settings file over one that is
            # merely unreadable to us.
            self._say('err', f'  note: could not record {key}: '
                             f'{settings_toml.SETTINGS_TOML_NAME} '
                             f'{"is missing" if toml is None else "does not read"}')
            self._show_options(self._folder)
            return
        toml.problems = []
        toml.tool[key] = bool(on)
        settings_toml.save(self._folder, toml)
        self._say('out', f'{key} {"on" if on else "off"}')

    def _show_options(self, folder):
        """Tick the menu from the folder's settings file."""
        wanted = settings_toml.options_of(
            settings_toml.load(folder) if folder else None)
        # Set without recording: setChecked fires toggled, which would write
        # the value straight back and, on a folder with no settings file,
        # report an error for a change nobody made.
        self._loading_options = True
        try:
            for key, act in self._option_actions.items():
                act.setEnabled(bool(folder))
                act.setChecked(wanted[key])
        finally:
            self._loading_options = False

    # ------------------------------------------------------------ folder --

    def set_folder(self, folder, ask=True, project=None):
        """Take `folder` as the terrain to pack.

        `project` names which of the folder's settings files to open, where
        the caller already knows -- opening one by name, or from Recents.
        Without it the folder is asked about as before.

        `ask` says whether this call may open a dialog. It is a real property
        of the call rather than a hook for tests: everything here otherwise
        runs to completion, and a caller with no one at the keyboard -- the
        selftest, or anything driving the window -- would simply block forever
        on a modal box nobody can answer.
        """
        # Which project, where the folder holds more than one. Asked before
        # anything is read, since the answer decides what "this folder's
        # settings" means for the rest of the call.
        if project is not None:
            self._project_path = project
        else:
            self._project_path = self._choose_project(folder, ask)
            if self._project_path is False:
                return                  # the chooser was dismissed

        # Any folder may be a terrain now -- the name no longer decides. What
        # remains is the setup confirmation, shown for a folder that is not
        # set up yet, with its contents reported rather than a refusal. The
        # one safeguard: nothing at all to pack is a plain message, not a
        # dialog.
        if not st.folder_settled(folder):
            count, found = st.describe_terrain_folder(folder)
            if count == 0 and not found:
                msg = (f'{os.path.basename(folder.rstrip(os.sep))} has '
                       f'nothing to pack -- no objects and no terrain assets.')
                if ask:
                    QMessageBox.warning(self, 'Nothing to pack', msg)
                self._say('err', msg)
                return
            if ask:
                pretty = ', '.join(found) if found else 'none yet'
                box = QMessageBox(self)
                box.setIcon(QMessageBox.Question)
                box.setWindowTitle('spritetool')
                box.setText(f"Set up '{os.path.basename(folder.rstrip(os.sep))}' "
                            f"as a spritetool terrain?")
                box.setInformativeText(
                    f'{count} object(s) discovered; terrain assets found: '
                    f'{pretty}.\n\n'
                    f'This writes {settings_toml.SETTINGS_TOML_NAME} to the '
                    f'folder.')
                yes = box.addButton('Set it up', QMessageBox.YesRole)
                box.addButton('Leave it alone', QMessageBox.NoRole)
                box.setDefaultButton(yes)
                box.exec()
                if box.clickedButton() is not yes:
                    self._say('err', 'setting the folder up was declined')
                    return
                # What the project is called. Asked here because this is the
                # one moment the author is looking at a folder they have
                # just chosen and knows what they mean it to be; asking at
                # the first save would be asking about work already done.
                # Empty means the question was not answered, so nothing is
                # set up and no file is written -- an unnamed project would
                # have to be named something, and picking for them is how a
                # folder ends up with a file nobody meant to create.
                named = self._ask_project_name(folder)
                if not named:
                    self._say('err', 'setting the folder up was cancelled: '
                                     'no project name given')
                    return
                self._project_path = named
                # Written now, empty, rather than left for the first save.
                # Everything below settles into the folder's existing
                # settings file, and creating it under the chosen name is
                # what makes that file the chosen one -- otherwise the
                # migration writes the default name and the project the
                # author just named is never the one being edited.
                settings_toml.save_path(
                    named, settings_toml.TerrainSettings())
                # Setting the folder up is also when its SpriteEditor-era
                # files are read into the TOML -- and the point to offer
                # clearing them away, while the author is still looking at
                # the folder they just dropped. Doing it here rather than at
                # the first pack keeps the file browser honest from the
                # start; the settings are written before anything is asked,
                # so declining costs nothing either way.
                #
                # Before the marker, not after: a folder with any TOML at all
                # is one the migration treats as already converted, so
                # marking first would leave the legacy files unread.
                self._migrate_legacy(folder)
                # Deliberately NOT marked settled here. The defaults offer
                # below is part of the same sitting, and it is skipped for a
                # folder that is already settled -- so marking now would
                # answer a question that has not been asked and put the
                # shipped art out of reach of every new folder. setup_terrain
                # marks it once the offer has been made, and leaves it
                # unmarked when the offer is refused so it stands next time.

        # A different terrain than the one already open. What is on screen
        # belongs to the old one -- the log is its pack's notes, the output
        # box is where *it* was going -- so both are let go of rather than
        # carried across and quietly attributed to the new folder.
        switching = self._folder is not None and folder != self._folder
        if switching:
            self._log.clear()
            self._out_dir = None

        self._folder = folder
        self._answers = {}
        self._drop.show_folder(folder, self._project_name())
        self._pack.setEnabled(True)
        if self._out_dir is None:
            # Where it went last time, if the project remembers and the place
            # is still there. A remembered path that has since been deleted
            # is worse than no memory at all -- it points somewhere that will
            # fail at the end of a pack -- so it falls through to the guess.
            # The folder itself, not just its parent. Packing creates the
            # output folder, so a path that has never existed is fine -- but
            # one the project remembers and that has since been deleted is a
            # different thing: it names somewhere that was, and silently
            # recreating it is not what the author meant by opening this
            # terrain. Falling back to the guess beside the source says
            # where it is going instead.
            remembered = None
            saved = settings_toml.load(folder)
            if saved is not None:
                was = saved.tool.get('last_output')
                if isinstance(was, str) and was and os.path.isdir(was):
                    remembered = was
            if remembered:
                # remember=False on both branches below: neither is a choice
                # the author made. Writing a guess into the file would make
                # merely opening a folder a change to it.
                self._set_out(remembered, remember=False)
            else:
                # Beside the build folder, named after it. A guess, and shown,
                # so it is corrected before packing rather than discovered
                # after.
                parent = os.path.dirname(folder.rstrip(os.sep))
                name = os.path.basename(folder.rstrip(os.sep))
                if name.lower() == 'build':
                    name = os.path.basename(parent) or 'terrain'
                    parent = os.path.dirname(parent)
                self._set_out(os.path.join(parent, f'{name} packed'),
                              remember=False)
        self._load_folder(folder)
        # Remembered once the folder has actually loaded, so a project that
        # could not be opened does not climb to the top of the list.
        self._remember(self._project_path)
        self.statusBar().showMessage(folder)
        if ask:
            self._offer_setup(folder)

    def _ask_project_name(self, folder):
        """Ask what to call this project. The settings path, or None.

        Empty at first rather than filled in with a guess: a prefilled box
        is answered by pressing return, which is not the same as choosing,
        and the name is what the Recents menu will show for years.
        """
        while True:
            name, ok = QInputDialog.getText(
                self, 'Name this project',
                f'A name for this terrain project.\n\nIt is saved as this '
                f'name plus {settings_toml.SETTINGS_TOML_SUFFIX} in\n'
                f'{_elide(folder)}')
            if not ok:
                return None
            name = name.strip()
            if not name:
                return None
            # Only the parts a file name cannot hold. Anything else the
            # author typed is theirs to keep -- spaces and capitals included.
            cleaned = ''.join('-' if c in '/\\:' else c for c in name)
            path = os.path.join(
                folder, f'{cleaned}{settings_toml.SETTINGS_TOML_SUFFIX}')
            if not os.path.exists(path):
                return path
            if QMessageBox.question(
                    self, 'Already there',
                    f'{os.path.basename(path)} is already in this folder.\n\n'
                    f'Open that project instead?',
                    QMessageBox.Open | QMessageBox.Cancel,
                    QMessageBox.Open) == QMessageBox.Open:
                return path

    def open_project(self, path=None):
        """Open a settings file, and the terrain folder holding it.

        The project is the unit an author thinks in, so this is what Open
        and the Recents menu both do. The folder comes from the file's own
        place rather than being asked for separately.
        """
        if path is None:
            path, _ = QFileDialog.getOpenFileName(
                self, 'Open terrain project', self._folder or '',
                f'spritetool projects (*{settings_toml.SETTINGS_TOML_SUFFIX})')
            if not path:
                return
        path = os.path.abspath(path)
        if not os.path.isfile(path):
            # A remembered project whose file has since gone. Saying so and
            # dropping it is better than an error from deeper in: the author
            # asked for something that is not there any more.
            QMessageBox.warning(
                self, 'Not there any more',
                f'{os.path.basename(path)} is no longer in\n'
                f'{os.path.dirname(path)}.\n\nIt has been taken off the '
                f'recent list.')
            self._recents.forget(path)
            self._rebuild_recents()
            return
        if not self._let_go_of_current():
            return
        folder = os.path.dirname(path)
        # Set before the load, since it is what decides which of a folder's
        # settings files the tables are filled from.
        self._project_path = path
        self.set_folder(folder, ask=False, project=path)

    def _let_go_of_current(self):
        """Settle unsaved edits before something else is opened.

        Returns whether to go on. The same three answers as closing the
        window, and for the same reason: opening another project drops what
        is on screen, so it is the same loss and deserves the same question.
        """
        pending = self.unsaved() if self._folder else []
        if not pending:
            return True
        what = ' and '.join(pending)
        answer = QMessageBox.question(
            self, 'Unsaved settings',
            f'The {what} table has changes that are not saved yet.\n\n'
            f'Save them before opening another project?',
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            QMessageBox.Save)
        if answer == QMessageBox.Cancel:
            return False
        if answer == QMessageBox.Save:
            return self.save_project()
        return True

    def _project_name(self):
        """What this project is called, or None where it has no file yet."""
        if not self._project_path:
            return None
        name = os.path.basename(self._project_path)
        suffix = settings_toml.SETTINGS_TOML_SUFFIX
        if name.lower().endswith(suffix.lower()):
            name = name[:-len(suffix)]
        return name or None

    def _remember(self, path):
        """Note a project as opened, and put it at the top of Recents."""
        if not path:
            return
        self._recents.add(path)
        self._rebuild_recents()

    def _rebuild_recents(self):
        """Fill the Recents submenu from the store.

        Numbered from one, and the number is the shortcut: the first item is
        the project opened before this one, which is the one an author
        reaches for most.
        """
        menu = getattr(self, '_recent_menu', None)
        if menu is None:
            return
        menu.clear()
        # Every remembered project is listed, the one open included. Hiding
        # it made the list empty for anyone who had opened exactly one
        # project, which is everybody on their first day -- and after a quit
        # and a reopen the thing you most want to click is the terrain you
        # were just in. What the current project gets instead is no shortcut
        # number, so Cmd+1 still means "the one before this".
        here = os.path.abspath(self._project_path or '')
        paths = self._recents.paths()
        if not paths:
            empty = menu.addAction('Nothing opened yet')
            empty.setEnabled(False)
            return
        number = 0
        for path in paths[:RECENT_LIMIT]:
            current = os.path.abspath(path) == here
            name = os.path.basename(path)
            if name.lower().endswith(settings_toml.SETTINGS_TOML_SUFFIX.lower()):
                name = name[:-len(settings_toml.SETTINGS_TOML_SUFFIX)]
            shown = f'{name}  --  {_elide(os.path.dirname(path), 40)}'
            act = QAction(f'{shown}   (open)' if current else shown, self)
            if not current:
                number += 1
                if number <= 9:
                    act.setShortcut(QKeySequence(f'Ctrl+{number}'))
                elif number == RECENT_LIMIT:
                    act.setShortcut(QKeySequence('Ctrl+0'))
            act.setStatusTip(path)
            act.setToolTip(path)
            act.triggered.connect(lambda _=False, p=path: self.open_project(p))
            menu.addAction(act)
        menu.addSeparator()
        clear = QAction('Clear the list', self)
        clear.triggered.connect(self._clear_recents)
        menu.addAction(clear)

    def _clear_recents(self):
        for path in self._recents.paths():
            self._recents.forget(path)
        self._rebuild_recents()

    def _choose_project(self, folder, ask=True):
        """Which settings file to open. None for a folder with none.

        False means the author dismissed the chooser, which is different from
        having nothing to choose: one is "not this folder after all", the
        other is "a folder not set up yet".
        """
        found = settings_toml.candidates(folder)
        if len(found) <= 1:
            return found[0] if found else None
        if not ask:
            return found[0]
        names = [os.path.basename(p) for p in found]
        # Told apart by what is in them, not just by name: two projects over
        # the same art differ in their settings, and the object count is the
        # cheapest true thing to say about one.
        labels = []
        for path, name in zip(found, names):
            settings = settings_toml.load_path(path)
            n = len(settings.objects) if settings else 0
            labels.append(f'{name}  --  {n} object{"" if n == 1 else "s"}')
        picked, ok = QInputDialog.getItem(
            self, APP_NAME,
            f'{os.path.basename(folder.rstrip(os.sep))} holds '
            f'{len(found)} settings files.\n\nWhich one?',
            labels, 0, False)
        if not ok:
            return False
        return found[labels.index(picked)]

    def _warn_about_orphans(self, folder):
        """Say when settings describe art that is no longer here.

        Said once, on open, and not acted on: the entries stay until a save,
        so looking at a project does not edit it. The words avoid "orphan"
        and "stale" -- what the author needs to know is that some settings
        point at pictures that are gone.
        """
        project = settings_toml.Project.for_folder(folder, self._project_path)
        try:
            _names, objects, _notes = st.scan_terrain(folder)
            sprites = [r['name'] for r in st.sprite_records(folder)]
        except Exception:
            return
        said = project.find_orphans(objects, sprites)
        if not said:
            return
        shown = ', '.join(said[:4])
        more = f' and {len(said) - 4} more' if len(said) > 4 else ''
        self._say('err',
                  f'  note: {len(said)} setting(s) describe pictures that are '
                  f'no longer in this folder: {shown}{more}. They are left '
                  f'alone until you save, which drops them.')

    def _migrate_legacy(self, folder):
        """Fold the SpriteEditor-era settings into the TOML, then offer to
        clear them away.

        The same two steps the CLI takes on its first pack, done here so a
        folder is tidy from the moment it is set up rather than after its
        first build. Everything is read and written before anything is
        deleted, so a yes cannot lose settings and a no costs nothing.
        """
        try:
            _names, objects, _notes = st.scan_terrain(folder)
        except Exception as exc:
            self._say('err', f'  note: could not read {folder}: {exc}')
            return
        recent = ['']
        ask = self._dialog_asker({}, recent)
        had_toml = st.settings_toml.load(folder)
        try:
            # Converts and, having written the TOML, offers the deletion
            # itself. Only a folder that had no TOML takes that path.
            _settings, trouble = st._settle_object_settings(
                folder, objects, ask, had_toml)
        except Exception as exc:
            self._say('err', f'  note: settings did not migrate: {exc}')
            return
        if trouble:
            # Not fatal here: the pack says the same thing in its own words,
            # and a clash is the author's to resolve rather than the drop's.
            self._say('err', f'  note: {trouble}')
            return
        if had_toml is not None:
            # Already converted on an earlier run, so nothing above asked.
            # Files may still be sitting there from a `no`, so the offer is
            # made once more rather than never again.
            st.offer_to_clear_legacy(folder, ask, had_toml)
            gone = st.offer_to_clear_build_files(folder, ask)
        else:
            # The conversion above made the build-file offers itself, once
            # the TOML was written.
            gone = []
        if gone:
            self._say('out', f'  cleared {len(gone)} file(s) from the folder')

    def setup_needed(self, folder):
        """Required pieces this folder has not got, or [] if it is settled.

        Separate from the dialog so the decision can be looked at without a
        window in the way -- and so nothing has to open a modal box to find
        out whether one is warranted.
        """
        if st.folder_settled(folder):
            return []                 # settled on an earlier run
        if not st.default_sources(st.REQUIRED_ASSETS):
            return []                 # nothing to lend, so nothing to offer
        return [p for p in st.REQUIRED_ASSETS
                if (not st._has_icon(folder) if p == st.DEFAULT_ICON
                    else not self._holds(folder, p))]

    def _offer_setup(self, folder):
        """Ask about borrowing art now, not when Pack is pressed.

        The tool offers its own art for whatever a folder has not got, once,
        on the folder's first pack. On a command line that arrives as a run of
        questions and reads fine. Behind a button called "Pack to Level.dir"
        it does not: pressing it should pack, not start an interview about
        setting the folder up.

        So the same decision is taken here, at the point the folder arrives,
        and passed to the pack as a settled answer. `defaults.` is the group
        the individual questions live under -- answering it is exactly what
        --defaults does.
        """
        lacking = self.setup_needed(folder)
        if not lacking:
            # Nothing to offer -- the folder has every required piece, or
            # there are no presets to lend. Either way the questions are
            # over, so mark it now: setup_terrain below is what usually
            # does that, and it is not going to run.
            if not st.folder_settled(folder):
                st._mark_settled(folder, ())
            return

        pretty = ', '.join(sorted({p.split('.')[0] for p in lacking}))
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        # The question goes in setText, not the title: macOS does not show a
        # title on a message box at all, so a window titled with the question
        # and a body of detail reads there as detail with no question.
        box.setWindowTitle('spritetool')
        box.setText('Set this folder up as a terrain?')
        box.setInformativeText(
            f'It has no {pretty}.\n\n'
            f'A terrain needs them. spritetool ships plain ones it can copy '
            f'in for you to draw over, written into this folder. You are only '
            f'asked once.\n\n'
            f'Without them the folder cannot be packed until you supply your '
            f'own.')
        yes = box.addButton('Copy them in', QMessageBox.YesRole)
        box.addButton('Leave it alone', QMessageBox.NoRole)
        box.setDefaultButton(yes)
        box.exec()

        if box.clickedButton() is not yes:
            # Remembered so the pack does not ask again piece by piece. The
            # folder stays unmarked, so the offer stands next time it is
            # dropped -- nothing was set up, and saying so once is enough.
            self._answers['defaults.'] = False
            self._say('out', f'not borrowing defaults for: {pretty}')
            return

        # The required pieces are settled by the box above. The optional
        # layers are not: a backdrop and a foreground change what the terrain
        # looks like more than anything else lent here, so each is its own
        # question rather than something that arrives with the rest.
        answers = {f'defaults.{p.split(".")[0]}': True
                   for p in st.REQUIRED_ASSETS}
        self._run_setup(folder, answers)

    def _run_setup(self, folder, answers):
        """Copy the shipped art in now, asking about the optional pieces.

        In this process rather than a spawned one: it is a handful of file
        copies with no compression in it, and the questions belong to the
        window anyway.
        """
        with self._capturing() as recent:
            try:
                borrowed, refused = st.setup_terrain(
                    folder, self._dialog_asker(answers, recent))
            except Exception as exc:
                self._say('err', f'could not set the folder up: {exc}')
                return
        if refused:
            QMessageBox.warning(self, 'Not set up', refused)
            self._say('err', refused)
            return
        self._say('out', f'set up {os.path.basename(folder.rstrip(os.sep))}: '
                         f'{len(borrowed)} piece(s) copied in')
        self._load_folder(folder)
        self.statusBar().showMessage(
            f'{len(borrowed)} piece(s) copied in -- edit them, then pack')

    @contextlib.contextmanager
    def _capturing(self):
        """Send the tool's prints to the log, keeping the most recent line.

        setup_terrain describes each piece on stdout just before asking about
        it -- "No back2: an animated layer behind the map." -- which is the
        only place that sentence exists. Keeping the last line is how the
        dialog can show it rather than the bare question.
        """
        recent = ['']

        class _Sink:
            def __init__(self, say):
                self._say = say
                self._buf = ''

            def write(self, text):
                self._buf += text
                while '\n' in self._buf:
                    line, _, self._buf = self._buf.partition('\n')
                    if line.strip():
                        recent[0] = line.strip()
                        self._say(line)
                return len(text)

            def flush(self):
                if self._buf.strip():
                    recent[0] = self._buf.strip()
                    self._say(self._buf)
                self._buf = ''

        out = _Sink(lambda line: self._say('out', line))
        err = _Sink(lambda line: self._say('err', line))
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, err
        try:
            yield recent
        finally:
            out.flush()
            err.flush()
            sys.stdout, sys.stderr = old_out, old_err

    def _dialog_asker(self, answers, recent):
        """An Asker that settles what it can and puts the rest in a dialog."""
        def ask_one(question):
            given = st._settled(answers, question.key)
            if given is not None:
                return given
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Warning if question.destructive
                        else QMessageBox.Question)
            box.setWindowTitle('spritetool')
            box.setText(question.prompt)
            if recent[0] and recent[0] != question.prompt:
                box.setInformativeText(recent[0])
            if question.subjects:
                box.setDetailedText('\n'.join(question.subjects[:20]))
            # A destructive question says what it does on the button. "Yes"
            # beside a warning icon is the same click as "Yes" beside a
            # question mark, and one of them deletes the author's files.
            yes = box.addButton('Delete' if question.destructive else 'Yes',
                                QMessageBox.YesRole)
            no = box.addButton('Keep' if question.destructive else 'No',
                               QMessageBox.NoRole)
            box.setDefaultButton(yes if question.default else no)
            box.exec()
            return box.clickedButton() is yes
        return ask_one

    @staticmethod
    def _holds(folder, piece):
        """Whether the folder has a source for one entry, by any spelling.

        Through split_picture rather than by joining extensions here: it is
        what decides that text.png and text.img.bmp name the same entry, and
        a second opinion about that would eventually disagree with the packer
        about whether a folder is missing something.
        """
        want = piece.rsplit('.', 1)[0].lower()
        for f in os.listdir(folder):
            if f.lower() == piece.lower():
                return True                      # an already-built .img/.spr
            split = st.split_picture(f)
            if split and split[0].lower() == want:
                return True
        return False

    def _set_out(self, path, remember=True):
        # Absolute, because a project may be opened from anywhere and a
        # relative path would mean somewhere else the next time.
        path = os.path.abspath(path)
        self._out_dir = path
        self._out_label.setText(f'Output: {_elide(path, 64)}')
        self._out_label.setToolTip(path)
        if remember and self._folder:
            self._remember_output(path)

    def _remember_output(self, path):
        """Keep the output folder with the project, not with the machine."""
        saved = settings_toml.load(self._folder)
        if saved is None or saved.problems:
            return          # nothing to write into, or a file we cannot read
        if saved.tool.get('last_output') == path:
            return          # unchanged; do not rewrite the file to say so
        saved.problems = []
        saved.tool['last_output'] = path
        settings_toml.save(self._folder, saved)

    def _choose_out(self):
        folder = QFileDialog.getExistingDirectory(self, 'Where to write')
        if folder:
            self._set_out(folder)

    def _session_settings(self):
        """What the tables hold now, as a TerrainSettings.

        Built from the widgets rather than read back from disk, so it is the
        session's answer even where nothing has been saved. The file is for
        loading and saving; between those two moments the window is the
        source of truth, and a hand edit to the file mid-session is not
        something it tries to notice.
        """
        settled = settings_toml.load(self._folder) if self._folder else None
        settled = settled or settings_toml.TerrainSettings()
        settled.problems = []
        if self._objects.rowCount():
            settled.objects = {stem.lower(): list(values)
                               for stem, values in self._objects.values()}
        excluded = dict(settled.excluded)
        if self._objects.rowCount():
            mine = {self._objects.item(r, self._objects.COL_NAME).text().lower()
                    for r in range(self._objects.rowCount())}
            excluded = {k: v for k, v in excluded.items() if k not in mine}
            excluded.update(self._objects.excluded())
        for r in range(self._sprites.rowCount()):
            name = self._sprites.item(r, self._sprites.COL_NAME).text()
            box = self._sprites.cellWidget(
                r, self._sprites.COL_INCLUDE).findChild(QCheckBox)
            excluded.pop(name.lower(), None)
            if box is not None and not box.isChecked():
                excluded[name.lower()] = True
            if name in settled.sprites:
                # The geometry as typed, so a refresh does not put back what
                # the file still says.
                for key, col in (('frames', self._sprites.COL_FRAMES),
                                 ('width', self._sprites.COL_CELL_W),
                                 ('height', self._sprites.COL_CELL_H)):
                    spin = self._sprites.cellWidget(r, col)
                    if spin is not None:
                        settled.sprites[name][key] = spin.value()
            widget = self._sprites.cellWidget(r, self._sprites.COL_PLAYBACK)
            if name in settled.sprites and widget is not None:
                idx = widget.currentIndex()
                if idx < len(self._sprites.PLAYBACK):
                    settled.sprites[name]['flags'] = idx
        settled.excluded = excluded
        return settled

    def _load_folder(self, folder, reuse=False):
        self._files.clear()
        try:
            names = sorted(os.listdir(folder))
        except OSError as exc:
            self._say('err', str(exc))
            return
        self._load_token += 1
        token = self._load_token
        self._files.setSortingEnabled(False)     # fill first, then order
        rows = []
        for name in names:
            path = os.path.join(folder, name)
            if _hidden(name):
                continue
            if os.path.isdir(path):
                # A gfx0/gfx1 folder is packed -- Coral Reef ships 450 sprite
                # overrides in one -- and listing it file by file would bury
                # the terrain's own art under hundreds of rows nobody edits
                # here. So it gets a single row saying it is in, with what it
                # holds and what that weighs. Any other subfolder is not
                # packed and is left out, as it always was.
                if name.lower() not in st.SPRITE_SUBFOLDERS:
                    continue
                sprites = total = 0
                for sub in os.listdir(path):
                    subpath = os.path.join(path, sub)
                    if not os.path.isfile(subpath) or _hidden(sub):
                        continue
                    total += os.path.getsize(subpath)
                    if sub.lower().endswith('.spr'):
                        sprites += 1
                item = _Row([f'{name}/  ({sprites} sprite override'
                             f'{"" if sprites == 1 else "s"}, packed)',
                             _human(total), ''])
                item.setData(1, Qt.UserRole, total)
                item.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
                item.setToolTip(0, f'{path}\nPacked into the archive as '
                                   f'{name}\\<name>.spr entries.')
                self._files.addTopLevelItem(item)
                continue
            if not os.path.isfile(path):
                continue
            size = os.path.getsize(path)
            item = _Row([name, _human(size), ''])
            item.setData(1, Qt.UserRole, size)
            item.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
            item.setTextAlignment(2, Qt.AlignRight | Qt.AlignVCenter)
            self._files.addTopLevelItem(item)
            if name.lower().endswith(('.png', '.bmp')):
                rows.append((item, path))
        self._files.setSortingEnabled(True)

        # On a refresh the settings are the session's, not the file's: the
        # folder is re-read for its pictures, which is the point, but an edit
        # made in the window and not yet saved must survive it.
        keep = self._session_settings() if reuse else None
        count = self._objects.load(folder, keep)
        self._tabs.setTabText(self._tabs.indexOf(self._obj_page),
                              f'Objects ({count})' if count else 'Objects')
        for problem in self._objects.problems:
            self._say('err', f'  note: {settings_toml.SETTINGS_TOML_NAME}: '
                             f'{problem}')

        # The sprite records. A wrong one is not a crash -- the art comes out
        # sliced in the wrong places -- so the count carries how many did not
        # add up, and each is said once in the log where the packer's other
        # notes go.
        sprites = self._sprites.load(folder, keep)
        bad = len(self._sprites.problems)
        self._tabs.setTabText(
            self._tabs.indexOf(self._spr_page),
            f'Sprites ({sprites})' if not bad else f'Sprites ({bad} wrong)')
        for problem in self._sprites.problems:
            self._say('err', f'  note: {problem}')
        self._show_options(folder)
        self._sync_save_actions()
        self._warn_about_orphans(folder)
        self._show_palette(os.path.join(folder, st.PALETTE_NAME))
        self._count_colours(rows, token)

    def _count_colours(self, rows, token):
        """Fill the Colours column, letting the window keep up.

        Counting is quick for a folder of objects and not quick for one with
        parallax sheets in it -- a real terrain took nearly three seconds --
        and doing it before the list appears would freeze the drop. So the
        names and sizes go up first and the counts arrive after, a few files
        at a time.

        `token` is what stops a count begun for one folder writing into the
        next: dropping a second folder bumps it, and this notices and stops.
        """
        shared = set()
        for i, (item, path) in enumerate(rows):
            if token != self._load_token:
                return
            found = _picture_colours(path)
            if found is not None:
                item.setText(2, f'{len(found):,}')
                item.setData(2, Qt.UserRole, len(found))
                # Shown for every picture, totalled only for the ones the
                # budget is about.
                if _in_budget(path):
                    shared |= found
            if i % 8 == 7:
                QApplication.processEvents()

        # The folder's own total is not the sum of the column: the budget
        # counts a colour once however many pictures draw it. Worth showing,
        # since a column adding to well over 112 otherwise looks alarming.
        #
        # This counts the art as drawn, which is not the number the packed
        # archive ends up with -- each picture is reduced to a palette of its
        # own on the way in, so a folder of 4,000 colours can pack to a few
        # hundred. It is the one the author can do something about.
        if not rows:
            return
        over = (f' -- over the {st.MAX_SHARED_COLOURS}'
                if len(shared) > st.MAX_SHARED_COLOURS else '')
        self._tabs.setTabText(0, f'Folder ({len(shared):,} colours{over})')

    def _show_palette(self, path):
        if os.path.exists(path):
            pix = QPixmap(path)
            if not pix.isNull():
                self._palette.setPixmap(pix.scaled(
                    QSize(520, 520), Qt.KeepAspectRatio, Qt.SmoothTransformation))
                return
        self._palette.setText('No palette.png yet. Pack with the palette '
                              'sheet enabled to draw one.')

    # ------------------------------------------------------------- save --

    def unsaved(self):
        """Which tables differ from the settings file, by name."""
        return [name for name, table in (('object', self._objects),
                                         ('sprite', self._sprites))
                if table.dirty]

    def save_project(self):
        """Write whatever has unsaved edits. Returns whether it got them all."""
        if not self._folder:
            return True
        done = True
        if self._objects.dirty:
            done = self._save_objects() and done
        if self._sprites.dirty:
            done = self._save_sprites() and done
        self._sync_save_actions()
        return done

    def save_project_as(self):
        """Name the settings file, keep working in it.

        The way a second project over the same art comes to exist: the folder
        keeps whatever it had, and this becomes the file the window is
        editing.
        """
        if not self._folder:
            return
        suggested = os.path.join(
            self._folder,
            f'{os.path.basename(self._folder.rstrip(os.sep))}'
            f'{settings_toml.SETTINGS_TOML_SUFFIX}')
        path, _ = QFileDialog.getSaveFileName(
            self, 'Save the settings as', suggested,
            f'spritetool settings (*{settings_toml.SETTINGS_TOML_SUFFIX})')
        if not path:
            return
        if not path.lower().endswith(settings_toml.SETTINGS_TOML_SUFFIX):
            # The suffix is what makes the file ours, and what finds it
            # again; a name without it would simply not be seen next time.
            path += settings_toml.SETTINGS_TOML_SUFFIX
        project = settings_toml.Project.for_folder(self._folder)
        for stem, values in self._objects.values():
            project.objects[stem.lower()] = list(values)
        project.save(path)
        self._say('out', f'wrote {os.path.basename(path)}')
        self._load_folder(self._folder)

    def _sync_save_actions(self):
        """Save is only offered when there is something to write."""
        if hasattr(self, '_save_action'):
            self._save_action.setEnabled(bool(self._folder)
                                         and bool(self.unsaved()))
        if hasattr(self, '_save_as_action'):
            self._save_as_action.setEnabled(bool(self._folder))

    def closeEvent(self, event):
        """Do not lose unsaved edits to a window being closed.

        Cancel has to actually cancel -- ignoring the event -- or the dialog
        is a formality that closes anyway.
        """
        pending = self.unsaved() if self._folder else []
        if not pending:
            event.accept()
            return
        what = ' and '.join(pending)
        answer = QMessageBox.question(
            self, 'Unsaved settings',
            f'The {what} table has changes that are not in '
            f'{settings_toml.SETTINGS_TOML_NAME} yet.\n\nSave them before '
            f'closing?',
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            QMessageBox.Save)
        if answer == QMessageBox.Cancel:
            event.ignore()
            return
        if answer == QMessageBox.Save and not self.save_project():
            event.ignore()      # a save that could not finish is not a close
            return
        event.accept()

    def _save_sprites(self):
        """Write the playback choices back to the settings file."""
        if not self._folder:
            return False
        if self._sprites.problems:
            # A row that does not add up is showing a record the packer would
            # refuse. Saving from here would not fix it and might write a
            # playback value beside geometry nobody has corrected yet.
            QMessageBox.warning(
                self, 'Not saving',
                'Some sprite records do not match their sheets:\n\n'
                + '\n'.join(self._sprites.problems[:6])
                + '\n\nFix the frame count or the sheet first.')
            return False
        path = self._sprites.save(self._folder, self._project_path)
        if path is None:
            QMessageBox.information(
                self, APP_NAME,
                f'This folder keeps its sprite geometry in .spr.spd sidecars '
                f'rather than {settings_toml.SETTINGS_TOML_NAME}.\n\nPack '
                f'it once, or convert it when asked, and the records move '
                f'into the settings file where they can be edited here.')
            return False
        self._say('out', f'wrote {os.path.basename(path)}')
        self._load_folder(self._folder)
        return True

    def _save_objects(self):
        if not self._folder:
            return False
        if self._objects.problems:
            # The table could not read what is there, so it is showing
            # defaults. Writing those back would replace settings nobody
            # chose to change.
            QMessageBox.warning(
                self, 'Not saving',
                f'{settings_toml.SETTINGS_TOML_NAME} could not be read, so '
                f'the table is not '
                f'showing what is in it:\n\n'
                + '\n'.join(self._objects.problems[:4])
                + '\n\nFix the file first; saving now would overwrite it.')
            return False
        path = self._objects.save(self._folder, self._project_path)
        self._say('out', f'wrote {os.path.basename(path)}')
        self._load_folder(self._folder)
        return True

    # ----------------------------------------------------------- refresh --

    def refresh(self, announce=False):
        """Read the folder again, keeping where the author was looking.

        The one reload path: the menu item, the shortcut and coming back to
        the window all end up here, so there is a single thing to reason
        about rather than three that drift.

        `announce` is for the explicit triggers, which should say they did
        something even when nothing changed -- a command that appears to do
        nothing is one the author stops trusting. The focus trigger passes
        False, because a line in the log every time the window is clicked is
        noise.
        """
        if not self._folder:
            return
        if self._job is not None and self._job.running:
            # Packing writes into the folder it is reading -- the settings
            # file, borrowed art copied in, that art refitted in place -- so
            # a reload here would show a folder half-written and race the
            # work still doing it.
            if announce:
                self._say('err', 'not refreshing: a job is running')
            return
        # No question about unsaved edits any more: a refresh re-reads the
        # folder's pictures and keeps the session's settings, so there is
        # nothing to lose and nothing to ask. This fires whenever the window
        # is brought back to the front, and a dialog on every return -- for a
        # loss that no longer happens -- was the worst of both.

        # Where the author was, so a reload does not throw it away. The tab
        # matters most: refreshing while reading the Sprites tab should not
        # drop them back on Folder.
        tab = self._tabs.currentIndex()
        scroll = self._files.verticalScrollBar().value()

        was_dirty = self._objects.dirty or self._sprites.dirty
        self._load_folder(self._folder, reuse=True)
        # Rebuilding the rows clears each table's flag, but nothing was
        # written -- so the project is exactly as unsaved as it was.
        if was_dirty:
            self._objects._dirty = self._objects.rowCount() > 0
            self._sprites._dirty = self._sprites.rowCount() > 0
            self._sync_save_actions()

        self._tabs.setCurrentIndex(min(tab, self._tabs.count() - 1))
        self._files.verticalScrollBar().setValue(scroll)
        if announce:
            name = os.path.basename(self._folder.rstrip(os.sep))
            self._say('out', f'refreshed {name}')

    def _app_state_changed(self, state):
        """Refresh when the window is brought back to the front.

        Changing a file in the folder means being in another program to do
        it, so coming back here is the gesture that follows an edit. Reading
        the folder then costs a fraction of a second and means what is shown
        is what is on disk, without a watcher on hundreds of files.
        """
        if state == Qt.ApplicationActive:
            self.refresh()

    # -------------------------------------------------------------- pack --

    def start_pack(self):
        if not self._folder or (self._job and self._job.running):
            return
        # Nothing is asked and nothing is written. Packing builds the
        # terrain as it is on screen: the settings go to the packer directly,
        # the same way the tables are the source of truth everywhere else in
        # the window. Saving stays a thing the author does when they mean to.

        self._log.clear()
        self._changed.clear()
        self._progress.setVisible(True)
        self._pack.setEnabled(False)
        self.statusBar().showMessage('Packing...')

        options = {'write_palette': True, 'jobs': self._jobs.value(),
                   'settings': self._session_settings()}
        self._job = PackJob(self)
        self._job.line.connect(self._say)
        self._job.question.connect(self._ask)
        self._job.done.connect(self._packed)
        self._job.failed.connect(self._refused)
        self._job.crashed.connect(self._crashed)
        self._job.cancelled.connect(
            lambda: self.statusBar().showMessage('Cancelled'))
        self._job.finished.connect(self._settle)
        self._job.start(self._folder, self._out_dir, options, self._answers)

    def decode_pictures(self, paths, ask=True):
        """Decode loose .img/.spr pictures to BMP, several at a time.

        One question -- where they go -- rather than one per file: the answer
        is the same for all of them, and a handful of pictures dropped
        together is one gesture. Decoding is quick enough to do here rather
        than in a child; the archive path is in one because a Water.dir with
        its GIFs takes twenty seconds, and a handful of pictures does not.
        """
        if not paths:
            return
        if self._job is not None and self._job.running:
            QMessageBox.information(
                self, APP_NAME, 'Something is already running. Let it finish '
                'first.')
            return
        if not ask:
            return

        first = os.path.dirname(os.path.abspath(paths[0]))
        many = len(paths) > 1
        out_dir = QFileDialog.getExistingDirectory(
            self,
            f'Where should {"they" if many else os.path.basename(paths[0])} '
            f'go?' if many else
            f'Where should {os.path.basename(paths[0])} go?',
            first)
        if not out_dir:
            return

        self._log.clear()
        wrote, failed = 0, []
        for path in paths:
            try:
                n, note = st.decode_picture_file(path, out_dir)
            except Exception as exc:            # never on one bad file
                n, note = 0, f'{os.path.basename(path)}: {exc}'
            if n:
                wrote += n
                self._say('out', f'Decoded {note}')
            else:
                failed.append(note)
                self._say('err', f'  {note}')
        self._say('out', f'\nWrote {wrote} picture'
                         f'{"" if wrote == 1 else "s"} to {out_dir}')
        if failed and not wrote:
            QMessageBox.warning(
                self, APP_NAME,
                'Nothing could be decoded:\n\n' + '\n'.join(failed[:6]))
        self.statusBar().showMessage(
            f'Decoded {wrote} of {len(paths)} to {out_dir}')

    def take_archive_apart(self, archive, ask=True):
        """A dropped .dir: where to put it, how to open it, then do it.

        Three questions in the order the answers are needed, so nothing is
        asked that a later answer could make pointless: where it goes, then
        whether to extract or decompress, then -- only for a decompress --
        whether to write GIFs, which is the one that costs twenty seconds on
        a shipped Water.dir.

        Dismissing any of them stops the whole thing. Nothing is written
        until the last is answered.
        """
        if self._job is not None and self._job.running:
            QMessageBox.information(
                self, APP_NAME, 'Something is already running. Let it finish '
                'first.')
            return
        if not ask:
            return

        name = os.path.basename(archive)
        # Beside the archive, named after it -- the same guess the output box
        # makes for a pack, and shown in the chooser so it can be corrected.
        suggested = os.path.join(os.path.dirname(os.path.abspath(archive)),
                                 f'{os.path.splitext(name)[0]} unpacked')
        out_dir = QFileDialog.getExistingDirectory(
            self, f'Where should {name} go?', os.path.dirname(suggested))
        if not out_dir:
            return

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle(APP_NAME)
        box.setText(f'How should {name} be opened?')
        box.setInformativeText(
            'Extract writes the files exactly as the archive stores them.\n\n'
            'Decompress writes those files and decodes every picture to a '
            'BMP with a .spd beside each sprite.')
        decompress = box.addButton('Decompress', QMessageBox.AcceptRole)
        extract = box.addButton('Extract', QMessageBox.AcceptRole)
        box.addButton('Cancel', QMessageBox.RejectRole)
        box.setDefaultButton(decompress)
        box.exec()
        clicked = box.clickedButton()
        if clicked is decompress:
            mode = 'decompress'
        elif clicked is extract:
            mode = 'extract'
        else:
            return

        want_gif = False
        if mode == 'decompress':
            gbox = QMessageBox(self)
            gbox.setIcon(QMessageBox.Question)
            gbox.setWindowTitle(APP_NAME)
            gbox.setText('Write an animated GIF for each sprite?')
            # gbox.setInformativeText(
            #     'A preview of every animation, beside the decoded art. It is '
            #     'the slow part -- a shipped Water.dir takes about twenty '
            #     'seconds -- and nothing needs them to pack again.')
            no = gbox.addButton('No', QMessageBox.NoRole)
            yes = gbox.addButton('Write GIFs', QMessageBox.YesRole)
            gbox.addButton('Cancel', QMessageBox.RejectRole)
            gbox.setDefaultButton(no)
            gbox.exec()
            if gbox.clickedButton() is yes:
                want_gif = True
            elif gbox.clickedButton() is not no:
                return

        self._log.clear()
        self._changed.clear()
        self._progress.setVisible(True)
        self._pack.setEnabled(False)
        self.statusBar().showMessage(f'{mode.capitalize()}ing {name}...')

        self._job = PackJob(self)
        self._job.line.connect(self._say)
        self._job.unpacked.connect(self._unpacked)
        self._job.failed.connect(self._refused)
        self._job.crashed.connect(self._crashed)
        self._job.finished.connect(self._settle)
        self._job.start_unpack(archive, out_dir, mode, want_gif)

    def _unpacked(self, r):
        """An extract or decompress finished."""
        where = r['out_dir']
        gifs = ', with GIFs' if r.get('gifs') else ''
        self._say('out', f"\n{r['mode'].capitalize()}ed "
                         f"{os.path.basename(r['archive'])} into {where}{gifs}")
        self.statusBar().showMessage(f"Done: {where}")

    def _say(self, stream, text):
        if not text.strip():
            return
        self._log.appendPlainText(text)

    def _ask(self, q):
        """A Question, as a dialog. The child is blocked until this answers."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning if q['destructive']
                    else QMessageBox.Question)
        box.setWindowTitle('spritetool')
        box.setText(q['prompt'])
        # What the tool printed on the way to asking. For the palette question
        # that is the colour count, how far over the budget it is, and what
        # leaving it costs -- none of which is in the prompt itself.
        if q.get('context'):
            box.setInformativeText('\n'.join(q['context']))
        if q['subjects']:
            shown = '\n'.join(q['subjects'][:12])
            if len(q['subjects']) > 12:
                shown += f"\n... and {len(q['subjects']) - 12} more"
            box.setDetailedText(shown)
        if q['destructive']:
            box.setInformativeText('This deletes files in your folder.')
        yes = box.addButton('Yes', QMessageBox.YesRole)
        no = box.addButton('No', QMessageBox.NoRole)
        box.addButton('Cancel pack', QMessageBox.RejectRole)
        box.setDefaultButton(yes if q['default'] else no)
        box.exec()

        clicked = box.clickedButton()
        if clicked is yes:
            reply = ANSWER_YES
        elif clicked is no:
            reply = ANSWER_NO
        else:
            reply = ANSWER_CANCEL
        self._log.appendPlainText(f'  [{q["key"]}] {q["prompt"]} -> {reply}')
        self._job.answer(reply)

    def _packed(self, r):
        self.statusBar().showMessage(
            f'Packed {len(r["entries"])} entries, {_human(r["archive_bytes"])}')
        self._log.appendPlainText(
            f'\nPacked {len(r["entries"])} entries into {r["out_path"]}\n'
            f'  built {r["built"]}, copied {r["reused"]}, '
            f'{_human(r["archive_bytes"])}')
        if r['too_big_to_send']:
            self._log.appendPlainText(
                '  note: past what wkTerrainSync will send to another player')

        for label, items in (('added', r['added']), ('changed', r['changed']),
                             ('removed', r['removed'])):
            for name in items:
                self._changed.addTopLevelItem(
                    QTreeWidgetItem([f'{label}: {name}']))
        if self._changed.topLevelItemCount():
            # Found by widget, not by number: a tab added anywhere to the left
            # silently renames a different one otherwise.
            self._tabs.setTabText(
                self._tabs.indexOf(self._changed),
                f'Changes ({self._changed.topLevelItemCount()})')
        if self._folder:
            # reuse=True, because packing wrote no settings. The folder is
            # re-read for what the pack put in it -- a palette sheet, art
            # borrowed on a first run -- but the tables keep what they hold.
            # Without this, switching a sprite off and pressing Pack showed
            # it switched back on: the file still said include, having never
            # been asked to say otherwise.
            was_dirty = self._objects.dirty or self._sprites.dirty
            self._load_folder(self._folder, reuse=True)
            if was_dirty:
                self._objects._dirty = self._objects.rowCount() > 0
                self._sprites._dirty = self._sprites.rowCount() > 0
                self._sync_save_actions()

    def _refused(self, lines):
        """A pack that could not finish, with what stopped it.

        The whole complaint in the dialog, not just its first line: the
        headline says how many entries failed and the lines under it say
        which and why, and a box showing only the headline is a box saying
        nothing actionable.
        """
        self._log.appendPlainText('\n' + '\n'.join(lines))
        self.statusBar().showMessage(lines[0] if lines else 'Refused')
        head = lines[0] if lines else 'Refused'
        rest = [ln.strip() for ln in lines[1:] if ln.strip()]
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle('Not packed')
        box.setText(head)
        if rest:
            box.setInformativeText('\n'.join(rest[:8]))
            if len(rest) > 8:
                box.setDetailedText('\n'.join(rest))
        box.exec()

    def _crashed(self, tb):
        self._log.appendPlainText('\n' + tb)
        self.statusBar().showMessage('The packer crashed')

    def _settle(self):
        self._progress.setVisible(False)
        # Only if there is something to pack. An archive job leaves no folder
        # loaded, and a Pack button that lights up with nothing behind it
        # invites a press that can only report the obvious.
        self._pack.setEnabled(self._folder is not None)


STYLE = """
#dropzone {
    border: 2px dashed palette(mid);
    border-radius: 10px;
    background: palette(alternate-base);
}
#dropzone[hot="true"] { border-color: palette(highlight); }
#droplabel { font-size: 15px; }
#muted { color: palette(placeholder-text); }
QPushButton#primary { font-weight: 600; }
"""



def _check_drag(table):
    """Drag down a column of tick boxes, with the mouse Qt itself delivers.

    Hand-built QMouseEvents sent with sendEvent are a restatement of whatever
    the author expected the event stream to be, so they agree with the code by
    construction: the first version of this drag passed such a test and did
    nothing at all in the window. QTest goes through Qt's real dispatch, which
    is where the Enter/Leave traffic and the implicit grab come from -- and a
    stray Leave cancelling the drag was the actual bug.
    """
    from PySide6.QtTest import QTest

    col = table.COL_INCLUDE
    rows = min(table.rowCount(), 4)
    if rows < 3:
        print(f'drag: only {rows} row(s), not checked')
        return True

    def box(row):
        holder = table.cellWidget(row, col)
        return holder.findChild(QCheckBox) if holder is not None else None

    boxes = [box(r) for r in range(rows)]
    if any(b is None for b in boxes):
        print('gui selftest FAILED: no tick box to drag')
        return False
    for b in boxes:
        b.setChecked(True)

    # Where each row sits in the viewport, so the drag is aimed the way a
    # pointer is: at the table, not at a widget it happens to contain.
    def centre(row):
        rect = table.visualRect(table.model().index(row, col))
        return rect.center()

    # Rows 0..last-1 are dragged over; the last row is left alone, so the
    # drag is shown to stop where the pointer stopped rather than running on.
    last = rows - 1
    QTest.mousePress(boxes[0], Qt.LeftButton)
    for row in range(1, last):
        QTest.mouseMove(table.viewport(), centre(row))
        QApplication.processEvents()
    QTest.mouseRelease(table.viewport(), Qt.LeftButton, pos=centre(last - 1))
    QApplication.processEvents()

    # Rows 0..last-1 were dragged over and take the drag's value; the rest
    # were never touched and keep theirs.
    # Read the cells again rather than the handles taken earlier: a box that
    # changed may have been rebuilt, and a stale handle then reports the value
    # it held before the drag.
    got = [box(r).isChecked() for r in range(rows)]
    want = [False] * last + [True] * (rows - last)
    print(f'drag: {got} (dragged over rows 0..{last - 1} of {rows})')
    if got != want:
        print(f'gui selftest FAILED: drag gave {got}, wanted {want}')
        return False
    return True


def _check_recents():
    """Recents remembers, in order, without duplicates, and survives a run.

    Kept in a temporary store rather than the real one: a test that writes
    to the author's own recent list would put its fixtures in their File
    menu.
    """
    import tempfile
    from PySide6.QtCore import QSettings

    with tempfile.TemporaryDirectory() as tmp:
        ini = os.path.join(tmp, 'recent.ini')
        store = QSettings(ini, QSettings.IniFormat)
        recent = Recents(store)
        for name in ('one', 'two', 'three'):
            recent.add(f'/t/{name}{settings_toml.SETTINGS_TOML_SUFFIX}')
        newest = recent.paths()
        # Newest first, and re-adding moves rather than duplicates.
        recent.add(f'/t/one{settings_toml.SETTINGS_TOML_SUFFIX}')
        moved = recent.paths()
        over = RECENT_LIMIT + 4
        for i in range(over):
            recent.add(f'/t/p{i}{settings_toml.SETTINGS_TOML_SUFFIX}')
        capped = len(recent.paths())
        want = recent.paths()
        store.sync()
        # A fresh reader over the same file is what a later run of the
        # program sees.
        kept = Recents(QSettings(ini, QSettings.IniFormat)).paths()

    ordered = newest[0].endswith(f'three{settings_toml.SETTINGS_TOML_SUFFIX}')
    once = len(moved) == 3 and moved[0].endswith(
        f'one{settings_toml.SETTINGS_TOML_SUFFIX}')
    print(f'recents: newest first {ordered}, no duplicates {once}, '
          f'capped at {capped}, survives a restart {kept == want}')
    if not (ordered and once and capped == RECENT_LIMIT and kept == want):
        print('gui selftest FAILED: recent projects are not kept properly')
        return False
    return True


def _check_recent_menu(window):
    """The one remembered project is listed, open or not.

    The bug this stands against: the menu hid whichever project was open,
    so an author who had opened exactly one -- everybody, on their first
    day -- quit, reopened, and found an empty list. What the open project
    loses is its shortcut number, not its place in the list, so Cmd+1 still
    means the project before this one.
    """
    import tempfile
    from PySide6.QtCore import QSettings

    def listed():
        return [a.text() for a in window._recent_menu.actions() if a.text()]

    def numbered():
        return [a.text().split('  --')[0].strip()
                for a in window._recent_menu.actions()
                if a.text() and a.shortcut().toString()]

    was_store, was_project = window._recents, window._project_path
    with tempfile.TemporaryDirectory() as tmp:
        store = QSettings(os.path.join(tmp, 'm.ini'), QSettings.IniFormat)
        window._recents = Recents(store)
        here = f'/t/here{settings_toml.SETTINGS_TOML_SUFFIX}'
        before = f'/t/before{settings_toml.SETTINGS_TOML_SUFFIX}'
        window._recents.add(before)
        window._recents.add(here)

        # Open: listed, marked, and not holding a number.
        window._project_path = here
        window._rebuild_recents()
        open_listed = any('here' in t for t in listed())
        open_keys = numbered()

        # Nothing open, as after a restart: still listed, and numbered.
        window._project_path = None
        window._rebuild_recents()
        shut_listed = any('here' in t for t in listed())
        shut_keys = numbered()

    window._recents, window._project_path = was_store, was_project
    window._rebuild_recents()

    good = (open_listed and shut_listed
            and open_keys == ['before']         # Cmd+1 skips the open one
            and shut_keys == ['here', 'before'])
    print(f'recent menu: open project listed {open_listed}, listed after a '
          f'restart {shut_listed}, Cmd+1 goes to {open_keys or [None]}')
    if not good:
        print('gui selftest FAILED: the recent menu hides projects it should '
              'list')
        return False
    return True


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    selftest = '--selftest' in argv
    if selftest:
        argv.remove('--selftest')
        # Offscreen so CI can run this without a display server.
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

    app = QApplication(argv)
    app.setApplicationName(APP_NAME)
    app.setStyleSheet(STYLE)
    window = Window()
    # Coming back to the window re-reads the folder. Changing a file in it
    # means being in another program to do it, so a return here is the
    # gesture that follows an edit -- and this catches it without a watcher
    # on hundreds of files. applicationStateChanged rather than a focus
    # event on the widget: it fires when the app is brought forward, which is
    # the thing being noticed, and not when focus moves between two of its
    # own widgets.
    app.applicationStateChanged.connect(window._app_state_changed)
    window.show()

    if selftest:
        # Build every widget, touch the folder-loading path, and leave. Enough
        # to catch an import that is not bundled or a signal wired to nothing.
        #
        # ask=False because there is nobody to answer: set_folder would offer
        # to set the fixture up and block on a modal box until it was killed.
        # The decision behind that dialog is still checked, just without one.
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        fixture = os.path.join(here, 'test', 'pack', 'flat', 'build')
        if os.path.isdir(fixture):
            window.set_folder(fixture, ask=False)
            print(f'setup needed: {len(window.setup_needed(fixture))} piece(s)')

            # Refresh has three triggers and one path; a folder that changes
            # behind the window must be picked up by it, and the tab the
            # author was on must survive. Checked here because it needs a
            # built window, which is what this already has.
            import shutil as _sh
            spare = os.path.join(fixture, 'obj-selftest-refresh.png')
            source = next((os.path.join(fixture, f)
                           for f in sorted(os.listdir(fixture))
                           if f.lower().endswith('.png')), None)
            if source and not os.path.exists(spare):
                was = window._files.topLevelItemCount()
                window._tabs.setCurrentIndex(window._tabs.count() - 1)
                tab = window._tabs.currentIndex()
                _sh.copyfile(source, spare)
                try:
                    window.refresh()
                    grew = window._files.topLevelItemCount() == was + 1
                    kept = window._tabs.currentIndex() == tab
                finally:
                    os.remove(spare)
                    window.refresh()
                back = window._files.topLevelItemCount() == was
                print(f'refresh: sees a new file {grew}, keeps the tab '
                      f'{kept}, sees it go {back}')
                if not (grew and kept and back):
                    print('gui selftest FAILED: refresh did not track the '
                          'folder')
                    return 1

            if not _check_drag(window._objects):
                return 1
            if not _check_recents():
                return 1
            if not _check_recent_menu(window):
                return 1
        app.processEvents()
        print('gui selftest ok')
        return 0
    return app.exec()
