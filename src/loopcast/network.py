"""Explicit proxy configuration for isolated components, never inherited ambient env."""
from urllib.parse import urlsplit


def proxy_environment(value=None):
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) != {'proxy_url', 'allowed_proxy_hosts'}:
        raise ValueError('Invalid component network settings')
    parsed = urlsplit(value['proxy_url'])
    hosts = value['allowed_proxy_hosts']
    if not isinstance(hosts, list) or not hosts or not all(isinstance(h, str) and h for h in hosts):
        raise ValueError('Proxy hosts must be explicitly allowed')
    if parsed.scheme not in {'http', 'https'} or parsed.hostname not in hosts or parsed.path not in {'', '/'} or parsed.query or parsed.fragment:
        raise ValueError('Proxy is outside the configured allowlist')
    return {'HTTP_PROXY': value['proxy_url'], 'HTTPS_PROXY': value['proxy_url'], 'NO_PROXY': '127.0.0.1,localhost'}
