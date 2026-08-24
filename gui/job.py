"""The packing job, run in a process of its own. No Qt in this file.

Packing does not belong in the window's process. It takes tens of seconds, it
starts process pools of its own, and `Team17Compressor.compress` is a tight
Python loop with nothing in it that would notice a request to stop -- so a
thread could not be cancelled, only waited for. A separate process can be
terminated outright, and a crash in Pillow kills the job rather than the app.

Everything here has to survive being pickled and re-imported under `spawn`,
which is why the job is a module-level function taking plain queues, and why
what goes over them are dicts rather than the tool's own dataclasses.
"""

import os
import sys
import traceback

# The window sends one of these back for a question.
ANSWER_YES = 'yes'
ANSWER_NO = 'no'
ANSWER_CANCEL = 'cancel'


class Cancelled(Exception):
    """The window closed a question rather than answering it."""


class _Tee:
    """A file-like object that turns writes into events.

    The tool reports by printing: notes to stderr, findings to stdout. Rather
    than change that for the GUI's benefit, the child's streams are pointed
    here and the lines arrive as events like everything else.

    The most recent few lines are kept as well. A question's prompt is short
    on purpose -- "Repalette it now?" -- and what it is really asking is in
    the sentences printed just before it, which is the only place the colour
    count and what declining costs are ever said. A dialog that showed the
    prompt alone would be asking the author to decide on nothing.
    """

    #: Three, because that is the longest run the tool prints before a
    #: question -- the palette one, which says the count, what leaving it
    #: costs, and what fitting it costs. More reaches back into whatever was
    #: being reported before and reads as part of the question.
    KEEP = 3

    def __init__(self, events, kind, recent):
        self._events = events
        self._kind = kind
        self._recent = recent
        self._buf = ''

    def _line(self, text):
        self._events.put((self._kind, text))
        if text.strip():
            self._recent.append(text.strip())
            del self._recent[:-self.KEEP]

    def write(self, text):
        self._buf += text
        while '\n' in self._buf:
            line, _, self._buf = self._buf.partition('\n')
            self._line(line)
        return len(text)

    def flush(self):
        if self._buf:
            self._line(self._buf)
            self._buf = ''

    def isatty(self):
        # False, and it matters: cli_asker reads a terminal when there is one.
        # There is not one here, and the window answers instead.
        return False


def _snapshot(folder):
    """Every file under `folder`, with its size and mtime.

    Packing writes into the folder it is given -- borrowed art, settings, a
    consolidated object_settings.txt, art refitted in place. Comparing two of
    these is how the window can say what it changed, which the CLI got for
    free by printing as it went.

    Dotfiles are left out. The Finder rewrites .DS_Store whenever a folder is
    looked at, so including them would report a change the pack did not make.
    """
    out = {}
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for f in files:
            if f.startswith('.'):
                continue
            p = os.path.join(root, f)
            try:
                st = os.stat(p)
            except OSError:
                continue
            out[os.path.relpath(p, folder)] = (st.st_size, st.st_mtime_ns)
    return out


def _changes(before, after):
    """(added, changed, removed), each a sorted list of relative paths."""
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(k for k in set(before) & set(after)
                     if before[k] != after[k])
    return added, changed, removed


def run(folder, out_dir, option_fields, answers, events, replies):
    """Pack `folder`, reporting to `events` and asking through `replies`.

    Runs in a spawned child. Never raises: everything it has to say leaves
    through the queue, because an exception here would die somewhere nobody
    is looking.
    """
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import spritetool as st

    recent = []
    sys.stdout = _Tee(events, 'out', recent)
    sys.stderr = _Tee(events, 'err', recent)

    def ask(question):
        # Anything already settled is not asked again. The window decides some
        # of these when the folder is dropped rather than when Pack is pressed,
        # and _settled is what makes 'defaults.' stand for every question
        # beneath it -- the same rule --defaults uses, borrowed rather than
        # written out a second time here.
        given = st._settled(answers, question.key)
        if given is not None:
            events.put(('out', f'{question.prompt} '
                               f'[{"y" if given else "n"}, settled already]'))
            return given
        sys.stdout.flush()
        events.put(('question', {
            'key': question.key,
            'prompt': question.prompt,
            'default': question.default,
            'destructive': question.destructive,
            'subjects': list(question.subjects),
            'context': [ln for ln in recent if ln != question.prompt],
        }))
        # Cleared once it has been used, so the next question is described by
        # what was printed for it rather than by what was left over from this
        # one. A question with nothing printed before it shows nothing.
        recent.clear()
        reply = replies.get()
        if reply == ANSWER_CANCEL:
            raise Cancelled()
        return reply == ANSWER_YES

    before = _snapshot(folder)
    try:
        options = st.Options(**option_fields)
        options.answers.update(answers)
        result = st.pack_terrain(folder, out_dir, options, ask)
    except Cancelled:
        events.put(('cancelled', None))
    except st.SpritetoolError as exc:
        events.put(('failed', exc.lines()))
    except Exception:
        # Anything unexpected reaches the window as a report rather than a
        # silent death. The traceback is the only useful thing left to say.
        events.put(('crashed', traceback.format_exc()))
    else:
        added, changed, removed = _changes(before, _snapshot(folder))
        events.put(('done', {
            'out_path': result.out_path,
            'entries': list(result.entries),
            'built': result.built,
            'reused': result.reused,
            'archive_bytes': result.archive_bytes,
            'largest': list(result.largest) if result.largest else None,
            'too_big_to_send': result.too_big_to_send,
            'added': added,
            'changed': changed,
            'removed': removed,
        }))
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        events.put(('finished', None))


def unpack(archive, out_dir, mode, want_gif, events, replies):
    """Take `archive` apart into `out_dir`, reporting to `events`.

    `mode` is 'extract' (the archive's files as they are stored) or
    'decompress' (the same files with every picture decoded to BMP, and
    optionally a GIF per sprite).

    In a spawned child for the same reason packing is: decoding a shipped
    Water.dir to GIFs takes twenty seconds, and a window that stops answering
    for twenty seconds looks broken. It asks nothing, so `replies` is unused
    -- it is taken anyway so the bridge can start either job the same way.

    The work is done by calling the tool's own command line rather than the
    functions behind it, because there are no functions behind it: extract
    and decompress live inside main()'s argument dispatch. Reaching them
    through argv keeps one implementation instead of a second copy here that
    would drift.
    """
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import spritetool as st

    recent = []
    sys.stdout = _Tee(events, 'out', recent)
    sys.stderr = _Tee(events, 'err', recent)

    argv = [sys.argv[0] if sys.argv else 'spritetool.py', mode,
            archive, out_dir]
    if want_gif and mode == 'decompress':
        argv.append('--gif')

    saved = sys.argv
    try:
        sys.argv = argv
        code = st.main()
    except st.SpritetoolError as exc:
        events.put(('failed', exc.lines()))
    except Exception:
        events.put(('crashed', traceback.format_exc()))
    else:
        if code:
            # main() reports its own reason on the way out, so the lines are
            # already in the log; this is only what stops the window calling
            # it a success.
            events.put(('failed', [f'{mode} did not finish (exit {code})']))
        else:
            events.put(('unpacked', {
                'mode': mode,
                'out_dir': out_dir,
                'archive': archive,
                'gifs': bool(want_gif and mode == 'decompress'),
            }))
    finally:
        sys.argv = saved
        sys.stdout.flush()
        sys.stderr.flush()
        events.put(('finished', None))
