# LoopCast

LoopCast currently provides an installable Python command-line scaffold.
Video generation and publishing are not implemented yet.

Requires Python 3.12 or newer.

## Build and run

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install --require-hashes -r requirements-ci.lock
python -m build --no-isolation
python -m venv /tmp/loopcast-install
/tmp/loopcast-install/bin/python -m pip install --no-deps dist/*.whl
/tmp/loopcast-install/bin/python -I -m loopcast --help
/tmp/loopcast-install/bin/python -I -m loopcast --version
```
