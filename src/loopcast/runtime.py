"""Own the single-worker application resources for one data directory."""
from pathlib import Path
import threading

from .storage import Store
from .worker import Worker
from .ownership import DirectoryLock


class ApplicationRuntime:
    """One process-local owner for storage, recovery, worker and publisher."""

    def __init__(self, directory: str | Path, generator_settings=None, media_settings=None, social_settings=None, *, schedule_settings=None, inspection_settings=None, clock=None, desktop_identity=None):
        from .generation import Generator

        self.generator_settings, self.media_settings = generator_settings, media_settings
        try: self.generator = Generator(generator_settings) if generator_settings is not None else None
        except (OSError, ValueError, RuntimeError): self.generator = None
        try:
            self._directory_lock = DirectoryLock(directory, create=True, desktop_identity=desktop_identity)
        except (OSError, ValueError):
            raise RuntimeError("The data directory is already in use") from None
        self.directory = self._directory_lock.path

        self.stopping = threading.Event()
        self.stream_slots = threading.BoundedSemaphore(16)
        self.stream_lock = threading.Lock()
        self.stream_count = 0
        self.worker = None
        self.publisher = None
        try:
            self.store = Store(self.directory, self.generator.manifest if self.generator else None, schedule_settings=schedule_settings, inspection_settings=inspection_settings, clock=clock)
            from .media import MediaLibrary
            from .creation import CreationExecutor
            from .social import SocialAdapter
            from .studio_service import StudioControl
            try: self.media = MediaLibrary(self.store, media_settings)
            except (OSError, ValueError, RuntimeError): self.media = MediaLibrary(self.store, None)
            self.social = SocialAdapter(self.directory, social_settings)
            self.studio = StudioControl(self.store, self.media, self.social, self.generator)
            from .inspection_service import InspectionService
            self.inspections=InspectionService(self)
            self.studio.inspections=self.inspections
            creation = CreationExecutor(self.generator, self.media) if self.generator else None
            self.worker = Worker(self.store, generator=self.generator, creation=creation)
            self.worker.start()
            from .publisher import Publisher
            self.publisher = Publisher(self.store, studio=self.studio, inspections=self.inspections)
        except BaseException:
            if self.worker is not None:
                self.worker.close()
            self._release_lock()
            raise

    def ready(self) -> bool:
        return not self.stopping.is_set() and self.worker is not None and self.worker.thread.is_alive() and self.publisher is not None and self.publisher.thread.is_alive()

    def close(self):
        self.stopping.set()
        errors = []
        resources = [resource for resource in (self.worker, self.publisher) if resource is not None]
        for resource in resources:
            try:resource.close()
            except BaseException as error:errors.append(error)
        stopped = all(resource.thread is None or not resource.thread.is_alive() for resource in resources)
        if stopped:
            self._release_lock()
        if errors and not isinstance(errors[0],Exception):raise errors[0]
        if errors or not stopped:
            raise RuntimeError('Background shutdown did not complete; the directory remains owned while work is active') from None

    def _release_lock(self):
        self._directory_lock.close()
