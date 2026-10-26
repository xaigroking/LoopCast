"""One directory lock shared by the service and offline maintenance commands."""
import fcntl
import os
from pathlib import Path
import stat


def directory(path, create=False):
    path = Path(path).expanduser()
    if '..' in path.parts:
        raise ValueError('UNSAFE_PATH')
    path = path.absolute()
    if path == Path('/'):
        raise ValueError('UNSAFE_PATH')
    handle = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=handle)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=handle)
            os.close(handle)
            handle = child
        return path, handle
    except BaseException:
        os.close(handle)
        raise


class DirectoryLock:
    def __init__(self, path, create=False):
        self.path, self.directory_fd = directory(path, create)
        self.fd = None
        try:
            self.fd = os.open('service.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=self.directory_fd)
            info = os.fstat(self.fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('UNSAFE_LOCK')
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self.directory_fd is not None:
            os.close(self.directory_fd)
            self.directory_fd = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
