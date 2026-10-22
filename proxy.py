"""Apply process proxy variables to this plugin's HTTP requests."""

from urllib.parse import urlsplit
from urllib.request import getproxies_environment, proxy_bypass_environment

import httpx


def proxy_for_url(url: str) -> str:
    """Resolve scheme-specific proxy, ALL_PROXY fallback, and NO_PROXY bypass.

    Args:
        url: URL of the outgoing flight-data request.

    Returns:
        Proxy URL, or an empty string to explicitly request a direct connection.
        Variables are read for each request; lowercase names take precedence
        where the operating system distinguishes case. No system proxy registry
        is read and no proxy credentials are logged.
    """
    proxies = getproxies_environment()
    parts = urlsplit(url)
    if proxy_bypass_environment(parts.netloc, proxies):
        return ""
    return proxies.get(parts.scheme.lower()) or proxies.get("all") or ""


class EnvironmentProxyTransport(httpx.BaseTransport):
    """Resolve each destination independently, reusing connections per proxy."""

    def __init__(self):
        self.transports = {}

    def handle_request(self, request):
        proxy = proxy_for_url(str(request.url))
        if proxy not in self.transports:
            self.transports[proxy] = httpx.HTTPTransport(proxy=proxy or None, trust_env=False)
        return self.transports[proxy].handle_request(request)

    def close(self):
        for transport in self.transports.values():
            transport.close()
