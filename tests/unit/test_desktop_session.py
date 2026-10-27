import unittest
from starlette.datastructures import Headers
from loopcast.desktop_session import DesktopSession, proof

class DesktopSessionTests(unittest.TestCase):
    def test_scoped_proof_binds_every_field_and_separates_directions(self):
        token, nonce = '1'*64, '2'*64
        api, origin, revision = 'http://127.0.0.1:1234', 'http://127.0.0.1:5678', '3'*40
        session = DesktopSession(token, api, origin, revision)
        signature = proof(token, 'request', nonce, api, origin, revision)
        headers = Headers({'x-loopcast-desktop-probe': nonce, 'x-loopcast-desktop-proof': signature})
        allowed, response = session.authorize('/healthz', 'GET', headers)
        self.assertTrue(allowed); self.assertEqual(response, proof(token, 'response', nonce, api, origin, revision))
        self.assertNotEqual(signature, response)
        for path, method in [('/v1/runs', 'GET'), ('/openapi.json', 'GET'), ('/healthz', 'POST')]:
            self.assertFalse(session.authorize(path, method, headers)[0])
        for changed in [(api+'0', origin, revision), (api, origin+'0', revision), (api, origin, '4'*40)]:
            other = DesktopSession(token, *changed)
            self.assertFalse(other.authorize('/healthz', 'GET', headers)[0])
        reflected = Headers({'x-loopcast-desktop-probe': nonce, 'x-loopcast-desktop-proof': response})
        self.assertFalse(session.authorize('/healthz', 'GET', reflected)[0])

    def test_bearer_is_constant_shape_and_duplicate_headers_do_not_authorize(self):
        token = 'a'*64
        session = DesktopSession(token, 'http://127.0.0.1:1', 'http://127.0.0.1:2', 'b'*40)
        self.assertTrue(session.authorize('/v1/runs', 'GET', Headers({'x-loopcast-desktop-session': token}))[0])
        for headers in [Headers(), Headers({'x-loopcast-desktop-session': 'wrong'}), Headers(raw=[(b'x-loopcast-desktop-session', token.encode()), (b'x-loopcast-desktop-session', token.encode())])]:
            self.assertFalse(session.authorize('/v1/runs', 'GET', headers)[0])
