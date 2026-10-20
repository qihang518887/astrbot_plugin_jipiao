"""Apply process proxy variables explicitly without changing fli's shared client."""

from urllib.parse import urlsplit
from urllib.request import getproxies_environment, proxy_bypass_environment


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


class EnvironmentProxyClient:
    """Forward requests to fli, preserving its retries, timeout and rate limiter."""

    def __init__(self, client):
        self.client = client

    def get(self, url, **kwargs):
        if "proxy" not in kwargs and "proxies" not in kwargs:
            kwargs["proxy"] = proxy_for_url(url)
        return self.client.get(url, **kwargs)

    def post(self, url, **kwargs):
        if "proxy" not in kwargs and "proxies" not in kwargs:
            kwargs["proxy"] = proxy_for_url(url)
        return self.client.post(url, **kwargs)
