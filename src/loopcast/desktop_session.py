"""Opt-in desktop capability and scoped health proofs; no service-mode changes."""
import hashlib
import hmac
import json
import re

HEX = re.compile(r'[a-f0-9]{64}')

def proof(token, direction, nonce, api_origin, public_origin, revision):
    payload = json.dumps(['loopcast.desktop-health.v1', direction, nonce, api_origin, public_origin, revision], ensure_ascii=False, separators=(',', ':'))
    return hmac.new(token.encode('utf-8'), payload.encode('utf-8'), hashlib.sha256).hexdigest()

class DesktopSession:
    def __init__(self, token, api_origin, public_origin, revision):
        if not isinstance(token, str) or not HEX.fullmatch(token): raise ValueError('INVALID_SESSION')
        self.token, self.api_origin, self.public_origin, self.revision = token, api_origin, public_origin, revision

    def authorize(self, path, method, headers):
        values = headers.getlist('x-loopcast-desktop-session')
        if len(values) == 1 and hmac.compare_digest(values[0].encode(), self.token.encode()): return True, None
        nonce = headers.get('x-loopcast-desktop-probe', '')
        signature = headers.get('x-loopcast-desktop-proof', '')
        if path == '/healthz' and method == 'GET' and len(headers.getlist('x-loopcast-desktop-probe')) == 1 and len(headers.getlist('x-loopcast-desktop-proof')) == 1 and HEX.fullmatch(nonce) and HEX.fullmatch(signature):
            expected = proof(self.token, 'request', nonce, self.api_origin, self.public_origin, self.revision)
            if hmac.compare_digest(signature, expected):
                return True, proof(self.token, 'response', nonce, self.api_origin, self.public_origin, self.revision)
        return False, None
