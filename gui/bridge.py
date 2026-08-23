"""Between the packing process and the window.

The child talks in queue messages; Qt wants signals delivered on the GUI
thread. This is the one place that knows both. A QThread does nothing but
block on the queue and re-emit -- it never packs, so it never blocks the
window, and there is nothing in it worth cancelling.
"""

import multiprocessing

from PySide6.QtCore import QObject, QThread, Signal

from . import job


class PackJob(QObject):
    """One run of the packer, as signals.

    `question` is the awkward one: the child is blocked waiting for an answer,
    so whoever handles it must eventually call `answer`. Nothing here enforces
    that -- if the window forgets, the pack simply waits forever -- so the
    window closes the loop by treating a dismissed dialog as a cancel.
    """

    line = Signal(str, str)          # stream ('out'/'err'), text
    question = Signal(dict)
    finished = Signal()
    done = Signal(dict)
    failed = Signal(list)
    crashed = Signal(str)
    cancelled = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._proc = None
        self._replies = None
        self._pump = None

    @property
    def running(self):
        return self._proc is not None and self._proc.is_alive()

    def start(self, folder, out_dir, options, answers):
        if self.running:
            raise RuntimeError('a pack is already running')
        # spawn explicitly: the tool asks for it everywhere else, and a forked
        # child of a Qt process inherits a copy of the event loop's state,
        # which is its own kind of trouble.
        ctx = multiprocessing.get_context('spawn')
        events = ctx.Queue()
        self._replies = ctx.Queue()
        self._proc = ctx.Process(
            target=job.run,
            args=(folder, out_dir, options, answers, events, self._replies),
            daemon=True)
        self._proc.start()

        self._pump = _Pump(events)
        self._pump.event.connect(self._dispatch)
        self._pump.start()

    def answer(self, reply):
        """Unblock the child, which is sitting in replies.get()."""
        if self._replies is not None:
            self._replies.put(reply)

    def cancel(self):
        """Stop the pack. The child may be deep in a compression loop.

        Terminating leaves the output half-written, which is why the window
        only offers this before anything is packed or as a last resort -- the
        archive is written in one go at the end, so a cancelled run mostly
        just loses its work rather than leaving a broken Level.dir.
        """
        if self._proc is not None and self._proc.is_alive():
            self._proc.terminate()

    def _dispatch(self, kind, payload):
        if kind in ('out', 'err'):
            self.line.emit(kind, payload)
        elif kind == 'question':
            self.question.emit(payload)
        elif kind == 'done':
            self.done.emit(payload)
        elif kind == 'failed':
            self.failed.emit(payload)
        elif kind == 'crashed':
            self.crashed.emit(payload)
        elif kind == 'cancelled':
            self.cancelled.emit()
        elif kind == 'finished':
            if self._pump is not None:
                self._pump.stop()
            self.finished.emit()


class _Pump(QThread):
    """Blocks on the child's queue and re-emits on the GUI thread."""

    event = Signal(str, object)

    def __init__(self, events):
        super().__init__()
        self._events = events
        self._stop = False

    def run(self):
        import queue as _queue
        while not self._stop:
            try:
                kind, payload = self._events.get(timeout=0.2)
            except _queue.Empty:
                continue
            except (OSError, EOFError):
                break
            self.event.emit(kind, payload)
            if kind == 'finished':
                break

    def stop(self):
        self._stop = True
