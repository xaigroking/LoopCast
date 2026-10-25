"""Own the single-worker application resources for one data directory."""
import fcntl
from pathlib import Path
import threading

from .storage import Store
from .worker import Worker


class ApplicationRuntime:
    """One process-local owner for storage, recovery, worker and publisher."""

    def __init__(self, directory: str | Path, generator_settings=None):
        from .generation import Generator

        self.generator = Generator(generator_settings) if generator_settings is not None else None
        self.directory = Path(directory).expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock_file = (self.directory / "service.lock").open("a")
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._lock_file.close()
            raise RuntimeError("The data directory is already in use") from None

        self.stopping = threading.Event()
        self.stream_slots = threading.BoundedSemaphore(16)
        self.stream_lock = threading.Lock()
        self.stream_count = 0
        self.worker = None
        self.publisher = None
        try:
            self.store = Store(self.directory, self.generator.manifest if self.generator else None)
            self.worker = Worker(self.store, generator=self.generator)
            self.worker.start()
            from .publisher import Publisher
            self.publisher = Publisher(self.store)
        except BaseException:
            if self.worker is not None:
                self.worker.close()
            self._release_lock()
            raise

    def ready(self) -> bool:
        return not self.stopping.is_set() and self.worker is not None and self.worker.thread.is_alive()

    def close(self):
        self.stopping.set()
        try:
            if self.worker is not None:
                self.worker.close()
            if self.publisher is not None:
                self.publisher.close()
        finally:
            self._release_lock()

    def _release_lock(self):
        if not self._lock_file.closed:
            self._lock_file.close()
