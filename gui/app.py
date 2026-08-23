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

from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QFont, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFrame, QHBoxLayout,
    QHeaderView, QLabel, QMainWindow, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QSpinBox, QSplitter, QTabWidget, QTableWidget,
    QTableWidgetItem, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from .bridge import PackJob
from .job import ANSWER_CANCEL, ANSWER_NO, ANSWER_YES

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import spritetool as st                                        # noqa: E402


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


class DropZone(QFrame):
    """Where a folder lands. Also a button, for people who do not drag."""

    def __init__(self, on_folder):
        super().__init__()
        self._on_folder = on_folder
        self.setAcceptDrops(True)
        self.setObjectName('dropzone')
        self.setMinimumHeight(96)

        self._label = QLabel('Drop a terrain build folder here')
        self._label.setAlignment(Qt.AlignCenter)
        self._label.setObjectName('droplabel')
        self._label.setTextFormat(Qt.RichText)

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

    def show_folder(self, folder):
        if not folder:
            self._label.setText('Drop a terrain build folder here')
            self._label.setToolTip('')
            return
        # The name, big, and the path beneath it small. A build folder is
        # usually several levels down and the full path crowds out the one
        # part that identifies it.
        parent, name = os.path.split(folder.rstrip(os.sep))
        self._label.setText(
            f'<div style="font-size:16px;font-weight:600">{name}</div>'
            f'<div style="font-size:11px;color:gray">{_elide(parent)}</div>')
        self._label.setToolTip(folder)

    def _browse(self):
        folder = QFileDialog.getExistingDirectory(self, 'Choose a build folder')
        if folder:
            self._on_folder(folder)

    # Only ever one folder, and only a folder: a dropped file is a mistake
    # worth refusing at the door rather than reporting later.
    def dragEnterEvent(self, event):
        urls = event.mimeData().urls()
        if len(urls) == 1 and os.path.isdir(urls[0].toLocalFile()):
            event.acceptProposedAction()
            self.setProperty('hot', True)
            self._restyle()

    def dragLeaveEvent(self, event):
        self.setProperty('hot', False)
        self._restyle()

    def dropEvent(self, event):
        self.setProperty('hot', False)
        self._restyle()
        self._on_folder(event.mimeData().urls()[0].toLocalFile())

    def _restyle(self):
        self.style().unpolish(self)
        self.style().polish(self)


class ObjectTable(QTableWidget):
    """The six settings the guide gives every object, one row each.

    The lowest-risk useful thing a window can do that a text editor cannot:
    the values are small integers with meanings, so they get spin boxes and
    named choices instead of a column of digits.
    """

    COLUMNS = ('Object', 'Weight', 'In front', 'Soil', 'Collision',
               'No stacking', 'Location')
    WHERE = ('side (left)', 'side (right)', 'ceiling', 'floor')

    def __init__(self):
        super().__init__(0, len(self.COLUMNS))
        self.setHorizontalHeaderLabels(self.COLUMNS)
        self.verticalHeader().setVisible(False)
        self.setAlternatingRowColors(True)
        self.setSelectionBehavior(QTableWidget.SelectRows)
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

    def load(self, folder):
        """Read the folder's objects and whatever settings they have."""
        self.setRowCount(0)
        self._dirty = False
        self.problems = []
        try:
            _names, objects, _notes = st.scan_terrain(folder)
        except Exception as exc:
            self.problems = [str(exc)]
            return 0

        # object_settings.txt is keyed by the picture's filename and
        # scan_terrain hands back bare stems, so the two have to be brought
        # together the same way the packer does it -- through split_picture,
        # which knows the .img/.spr infix SpriteEditor used. Matching on the
        # raw name instead silently misses every object, and the table would
        # show defaults it would then save over the author's real settings.
        settings = {}
        combined = os.path.join(folder, st.SETTINGS_NAME)
        if os.path.exists(combined):
            with open(combined, encoding='latin-1') as fh:
                by_picture, problems = st.parse_settings(fh.read())
            if problems:
                self.problems = problems
                return 0
            for picture, values in by_picture.items():
                split = st.split_picture(picture)
                settings[(split[0] if split else picture).lower()] = values
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
            self.setItem(row, 0, name)

            weight = QSpinBox()
            weight.setRange(1, 10)
            weight.setValue(values[0])
            weight.valueChanged.connect(self._touch)
            self.setCellWidget(row, 1, weight)

            for col, idx in ((2, 1), (3, 2), (4, 3), (5, 4)):
                box = QCheckBox()
                box.setChecked(bool(values[idx]))
                box.stateChanged.connect(self._touch)
                holder = QWidget()
                lay = QHBoxLayout(holder)
                lay.setContentsMargins(0, 0, 0, 0)
                lay.setAlignment(Qt.AlignCenter)
                lay.addWidget(box)
                self.setCellWidget(row, col, holder)

            where = QComboBox()
            where.addItems(self.WHERE)
            where.setCurrentIndex(min(values[5], 3))
            where.currentIndexChanged.connect(self._touch)
            self.setCellWidget(row, 6, where)

        self._dirty = False
        return len(objects)

    def _touch(self, *_):
        self._dirty = True

    def values(self):
        """[(stem, six values)], in the table's order."""
        out = []
        for row in range(self.rowCount()):
            stem = self.item(row, 0).text()
            vals = [self.cellWidget(row, 1).value()]
            for col in (2, 3, 4, 5):
                box = self.cellWidget(row, col).findChild(QCheckBox)
                vals.append(1 if box.isChecked() else 0)
            vals.append(self.cellWidget(row, 6).currentIndex())
            out.append((stem, vals))
        return out

    def save(self, folder):
        """Write object_settings.txt. Returns what it wrote to.

        Keyed by the picture's filename and written with '\\n' newlines, both
        matching what the packer writes -- otherwise every save would show up
        as a change to the whole file, and a folder would never look settled.
        """
        picture_of = {}
        for f in os.listdir(folder):
            split = st.split_picture(f)
            if split:
                picture_of.setdefault(split[0].lower(), f)
        rows = [(picture_of.get(stem.lower(), stem), values)
                for stem, values in sorted(self.values(),
                                           key=lambda kv: kv[0].lower())]
        path = os.path.join(folder, st.SETTINGS_NAME)
        with open(path, 'w', encoding='latin-1', newline='\n') as fh:
            fh.write(st.format_settings(rows))
        self._dirty = False
        return path


class Window(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1060, 720)
        self._folder = None
        self._out_dir = None
        self._job = None
        #: Questions settled before packing, by key -- see _offer_setup.
        self._answers = {}

        self._drop = DropZone(self.set_folder)
        self._pack = QPushButton('Pack to Level.dir')
        self._pack.setObjectName('primary')
        self._pack.setEnabled(False)
        self._pack.setMinimumHeight(40)
        self._pack.clicked.connect(self.start_pack)

        self._out_label = QLabel('No output folder chosen')
        self._out_label.setObjectName('muted')
        out_btn = QPushButton('Output...')
        out_btn.clicked.connect(self._choose_out)

        self._jobs = QSpinBox()
        self._jobs.setRange(0, 64)
        self._jobs.setSpecialValueText('auto')
        self._jobs.setToolTip('Processes to pack with. auto uses every core; '
                              '1 packs in a single process.')

        self._progress = QProgressBar()
        self._progress.setRange(0, 0)
        self._progress.setVisible(False)

        self._files = QTreeWidget()
        self._files.setHeaderLabels(['File', 'Size'])
        self._files.setRootIsDecorated(False)
        self._files.setAlternatingRowColors(True)
        self._files.header().setSectionResizeMode(0, QHeaderView.Stretch)

        self._objects = ObjectTable()
        save_objects = QPushButton(f'Save {st.SETTINGS_NAME}')
        save_objects.clicked.connect(self._save_objects)
        obj_page = QWidget()
        obj_box = QVBoxLayout(obj_page)
        obj_box.setContentsMargins(0, 0, 0, 0)
        obj_box.addWidget(self._objects)
        obj_row = QHBoxLayout()
        obj_row.addStretch(1)
        obj_row.addWidget(save_objects)
        obj_box.addLayout(obj_row)

        self._palette = QLabel('Pack once with the palette sheet to see it.')
        self._palette.setAlignment(Qt.AlignCenter)
        self._palette.setObjectName('muted')

        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setFont(QFont('Menlo', 11))
        self._log.setPlaceholderText(
            'What the packer says appears here.\n\n'
            'The same notes the command line prints: colours counted, art '
            'refitted, objects widened, anything the terrain guide would '
            'complain about.')

        self._changed = QTreeWidget()
        self._changed.setHeaderLabels(['What packing changed in your folder'])
        self._changed.setRootIsDecorated(False)

        tabs = QTabWidget()
        tabs.addTab(self._files, 'Folder')
        tabs.addTab(obj_page, 'Objects')
        tabs.addTab(self._palette, 'Palette')
        tabs.addTab(self._changed, 'Changes')
        self._tabs = tabs

        left = QWidget()
        lbox = QVBoxLayout(left)
        lbox.setContentsMargins(16, 16, 8, 16)
        lbox.setSpacing(12)
        lbox.addWidget(self._drop)
        row = QHBoxLayout()
        row.addWidget(QLabel('Processes'))
        row.addWidget(self._jobs)
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

    # ------------------------------------------------------------ folder --

    def set_folder(self, folder, ask=True):
        """Take `folder` as the terrain to pack.

        `ask` says whether this call may open a dialog. It is a real property
        of the call rather than a hook for tests: everything here otherwise
        runs to completion, and a caller with no one at the keyboard -- the
        selftest, or anything driving the window -- would simply block forever
        on a modal box nobody can answer.
        """
        # The name is the only thing that says a folder was meant to be a
        # terrain: everything a terrain needs can be stood in for, and a
        # folder holding one picture is a legitimate starting point, so there
        # is nothing in the contents to test. Refused here rather than after
        # the packing button, where it would read as the pack having failed.
        refuse = st._terrain_needs(folder)
        if refuse:
            if ask:
                QMessageBox.warning(self, 'Not a build folder', refuse)
            self._say('err', refuse)
            return

        self._folder = folder
        self._answers = {}
        self._drop.show_folder(folder)
        self._pack.setEnabled(True)
        if self._out_dir is None:
            # Beside the build folder, named after it. A guess, and shown, so
            # it is corrected before packing rather than discovered after.
            parent = os.path.dirname(folder.rstrip(os.sep))
            name = os.path.basename(folder.rstrip(os.sep))
            if name.lower() == 'build':
                name = os.path.basename(parent) or 'terrain'
                parent = os.path.dirname(parent)
            self._set_out(os.path.join(parent, f'{name} packed'))
        self._load_folder(folder)
        self.statusBar().showMessage(folder)
        if ask:
            self._offer_setup(folder)

    def setup_needed(self, folder):
        """Required pieces this folder has not got, or [] if it is settled.

        Separate from the dialog so the decision can be looked at without a
        window in the way -- and so nothing has to open a modal box to find
        out whether one is warranted.
        """
        if st.read_settings(folder) is not None:
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
            yes = box.addButton('Yes', QMessageBox.YesRole)
            no = box.addButton('No', QMessageBox.NoRole)
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

    def _set_out(self, path):
        self._out_dir = path
        self._out_label.setText(f'Output: {_elide(path, 64)}')
        self._out_label.setToolTip(path)

    def _choose_out(self):
        folder = QFileDialog.getExistingDirectory(self, 'Where to write')
        if folder:
            self._set_out(folder)

    def _load_folder(self, folder):
        self._files.clear()
        try:
            names = sorted(os.listdir(folder))
        except OSError as exc:
            self._say('err', str(exc))
            return
        for name in names:
            path = os.path.join(folder, name)
            if not os.path.isfile(path) or _hidden(name):
                continue
            item = QTreeWidgetItem([name, _human(os.path.getsize(path))])
            self._files.addTopLevelItem(item)

        count = self._objects.load(folder)
        self._tabs.setTabText(1, f'Objects ({count})' if count else 'Objects')
        for problem in self._objects.problems:
            self._say('err', f'  note: {st.SETTINGS_NAME}: {problem}')
        self._show_palette(os.path.join(folder, st.PALETTE_NAME))

    def _show_palette(self, path):
        if os.path.exists(path):
            pix = QPixmap(path)
            if not pix.isNull():
                self._palette.setPixmap(pix.scaled(
                    QSize(520, 520), Qt.KeepAspectRatio, Qt.SmoothTransformation))
                return
        self._palette.setText('No palette.png yet. Pack with the palette '
                              'sheet enabled to draw one.')

    def _save_objects(self):
        if not self._folder:
            return
        if self._objects.problems:
            # The table could not read what is there, so it is showing
            # defaults. Writing those back would replace settings nobody
            # chose to change.
            QMessageBox.warning(
                self, 'Not saving',
                f'{st.SETTINGS_NAME} could not be read, so the table is not '
                f'showing what is in it:\n\n'
                + '\n'.join(self._objects.problems[:4])
                + '\n\nFix the file first; saving now would overwrite it.')
            return
        path = self._objects.save(self._folder)
        self._say('out', f'wrote {os.path.basename(path)}')
        self._load_folder(self._folder)

    # -------------------------------------------------------------- pack --

    def start_pack(self):
        if not self._folder or (self._job and self._job.running):
            return
        if self._objects.dirty:
            answer = QMessageBox.question(
                self, 'Unsaved object settings',
                f'The object table has changes that are not in '
                f'{st.SETTINGS_NAME} yet.\n\nSave them before packing?',
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
            if answer == QMessageBox.Cancel:
                return
            if answer == QMessageBox.Save:
                self._objects.save(self._folder)

        self._log.clear()
        self._changed.clear()
        self._progress.setVisible(True)
        self._pack.setEnabled(False)
        self.statusBar().showMessage('Packing...')

        options = {'write_palette': True, 'jobs': self._jobs.value()}
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

    def _say(self, stream, text):
        if not text.strip():
            return
        self._log.appendPlainText(text)

    def _ask(self, q):
        """A Question, as a dialog. The child is blocked until this answers."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning if q['destructive']
                    else QMessageBox.Question)
        box.setWindowTitle('spritetool asks')
        box.setText(q['prompt'])
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
            self._tabs.setTabText(3, f'Changes ({self._changed.topLevelItemCount()})')
        if self._folder:
            self._load_folder(self._folder)

    def _refused(self, lines):
        self._log.appendPlainText('\n' + '\n'.join(lines))
        self.statusBar().showMessage(lines[0] if lines else 'Refused')
        QMessageBox.warning(self, 'Not packed',
                            '\n'.join(lines[:1]) or 'Refused',
                            QMessageBox.Ok)

    def _crashed(self, tb):
        self._log.appendPlainText('\n' + tb)
        self.statusBar().showMessage('The packer crashed')

    def _settle(self):
        self._progress.setVisible(False)
        self._pack.setEnabled(True)


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
        app.processEvents()
        print('gui selftest ok')
        return 0
    return app.exec()
