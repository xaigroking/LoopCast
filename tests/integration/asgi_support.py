"""In-process ASGI host for lifecycle and live-stream resource assertions."""
import threading
import time

import uvicorn

from loopcast.api import create_app


class ASGIThread:
    def __init__(self, directory):
        self.app = create_app(directory)
        self.server = uvicorn.Server(uvicorn.Config(
            self.app,
            host="127.0.0.1",
            port=0,
            workers=1,
            reload=False,
            access_log=False,
            log_level="critical",
            lifespan="on",
            proxy_headers=False,
        ))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self):
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started and self.thread.is_alive() and time.monotonic() < deadline:
            time.sleep(.01)
        if not self.server.started:
            raise AssertionError("ASGI service did not complete startup")
        port = self.server.servers[0].sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        self.runtime = self.app.state.runtime
        return self

    def stop(self):
        if self.thread.is_alive():
            self.server.should_exit = True
            self.thread.join(timeout=15)
        if self.thread.is_alive():
            raise AssertionError("ASGI service did not stop cleanly")
