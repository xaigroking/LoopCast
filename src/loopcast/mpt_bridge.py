"""Standalone pinned-generator launcher with an explicit Edge TTS proxy port."""
import inspect
import os


def configure_proxy():
    proxy = os.environ.get('LOOPCAST_EDGE_PROXY')
    if not proxy: return
    import edge_tts
    original = edge_tts.Communicate
    signature = inspect.signature(original)
    if 'proxy' not in signature.parameters: raise RuntimeError('Unsupported TTS proxy interface')
    class ControlledCommunicate(original):
        __signature__ = signature
        def __init__(self, *args, **kwargs):
            kwargs['proxy'] = proxy
            super().__init__(*args, **kwargs)
    edge_tts.Communicate = ControlledCommunicate


def main():
    configure_proxy()
    from app.launcher import main as upstream
    return upstream()


if __name__ == '__main__':
    raise SystemExit(main())
