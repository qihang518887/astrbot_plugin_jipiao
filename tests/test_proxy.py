"""Verify environment proxy handling against a local HTTP server/proxy."""

import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


@pytest.fixture
def local_server():
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(("GET", self.path))
            body = b"proxy" if self.path.startswith("http://") else b"direct"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_CONNECT(self):
            received.append(("CONNECT", self.path))
            # No TLS or external network is needed to prove proxy selection.
            self.send_response(502)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def native_request(url, proxy_env):
    # A fresh process models real service startup and avoids Windows C-runtime
    # environment caching when Python changes os.environ inside a running test.
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.lower().endswith("_proxy") and key != "REQUEST_METHOD"
    }
    env.update(proxy_env)
    code = """
import sys
import httpx
from astrbot_plugin_jipiao.proxy import EnvironmentProxyTransport
client = httpx.Client(transport=EnvironmentProxyTransport(), trust_env=False)
try:
    print(client.get(sys.argv[1], timeout=2).text)
except Exception as exc:
    print(type(exc).__name__)
finally:
    client.close()
"""
    result = subprocess.run(
        [sys.executable, "-c", code, url], env=env, capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.mark.parametrize("name", ["HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"])
def test_native_https_environment_proxy(local_server, name):
    endpoint, received = local_server
    native_request("https://jipiao-proxy-test.invalid/check", {name: endpoint})
    assert received and all(
        item == ("CONNECT", "jipiao-proxy-test.invalid:443") for item in received
    )


def test_native_https_proxy_precedes_all_proxy(local_server):
    endpoint, received = local_server
    native_request(
        "https://jipiao-proxy-test.invalid/check",
        {
            "HTTPS_PROXY": endpoint,
            "ALL_PROXY": "http://127.0.0.1:1",
        },
    )
    assert received and received[0][0] == "CONNECT"


def test_native_http_proxy(local_server):
    endpoint, received = local_server
    result = native_request("http://jipiao-proxy-test.invalid/check", {"http_proxy": endpoint})
    assert result == "proxy"
    assert received == [("GET", "http://jipiao-proxy-test.invalid/check")]


@pytest.mark.parametrize(
    "name,value",
    [
        ("NO_PROXY", "localhost,127.0.0.1"),
        ("no_proxy", "127.0.0.1"),
        ("NO_PROXY", "*"),
    ],
)
def test_native_no_proxy_bypass(local_server, name, value):
    endpoint, received = local_server
    assert native_request(endpoint + "/check", {"http_proxy": endpoint, name: value}) == "direct"
    assert received == [("GET", "/check")]


def test_native_without_proxy_is_direct(local_server):
    endpoint, received = local_server
    assert native_request(endpoint + "/check", {}) == "direct"
    assert received == [("GET", "/check")]
