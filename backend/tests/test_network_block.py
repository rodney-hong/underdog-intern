"""
The test-wide network block in conftest.py, run on small inner test sessions (in a subprocess,
under the real conftest): a caught network call still fails its test; loopback is allowed; a
background thread that calls out after its test has ended fails the session; live_network is
allowed out only with RUN_LIVE_EXTRACTOR=1. Nothing leaves the machine: every inner call is
blocked before DNS (RUN_LIVE_EXTRACTOR is removed for the inner sessions).
"""
import os

import pytest

pytest_plugins = ["pytester"]

CONFTEST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "conftest.py")

INNER_CONFTEST = f"""
import importlib.util
spec = importlib.util.spec_from_file_location("sportsbettr_conftest", {CONFTEST!r})
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
globals().update({{k: v for k, v in vars(mod).items() if not k.startswith("__")}})
"""


@pytest.fixture
def inner(pytester, monkeypatch):
    monkeypatch.delenv("RUN_LIVE_EXTRACTOR", raising=False)
    pytester.makeconftest(INNER_CONFTEST)
    return pytester


def test_a_caught_call_still_fails_its_test(inner):
    inner.makepyfile("""
        import socket
        def test_swallows_the_error():
            try:
                socket.getaddrinfo("example.invalid", 443)
            except OSError:
                pass
        def test_loopback_is_fine():
            socket.getaddrinfo("localhost", 80)
    """)
    r = inner.runpytest_subprocess("-p", "no:cacheprovider")
    r.assert_outcomes(passed=2, errors=1)          # the first test passes its call, then errors at teardown
    r.stdout.fnmatch_lines(["*ERROR at teardown of test_swallows_the_error*",
                            "*made 1 blocked network call(s)*example.invalid*"])


def test_a_thread_calling_out_after_its_test_fails_the_session(inner):
    inner.makepyfile("""
        import socket, threading, time
        def _late():
            time.sleep(0.5)
            try:
                socket.getaddrinfo("example.invalid", 443)
            except OSError:
                pass
        def test_leaks_a_thread():
            threading.Thread(target=_late).start()
        def test_after():
            time.sleep(1.0)
    """)
    r = inner.runpytest_subprocess("-p", "no:cacheprovider")
    assert r.ret != 0
    r.stdout.fnmatch_lines(["*ERROR at teardown of test_after*",       # blamed on the test running then
                            "*made 1 blocked network call*"])


def test_a_thread_calling_out_after_the_last_test_fails_the_session(inner):
    inner.makepyfile("""
        import socket, threading, time
        def _late():
            time.sleep(0.3)
            try:
                socket.getaddrinfo("example.invalid", 443)
            except OSError:
                pass
        def test_leaks_a_thread():
            threading.Thread(target=_late).start()
    """)
    # the last test ends before the thread calls out; hold the session open past it
    inner.makeconftest(INNER_CONFTEST + """
_orig_finish = globals()["pytest_sessionfinish"]
def pytest_sessionfinish(session, exitstatus):
    import time
    time.sleep(0.8)
    _orig_finish(session, exitstatus)
""")
    r = inner.runpytest_subprocess("-p", "no:cacheprovider")
    r.assert_outcomes(passed=1)
    assert r.ret != 0
    r.stdout.fnmatch_lines(["*network calls outside any test*example.invalid*"])


def test_live_network_needs_the_env_switch(inner):
    inner.makepyfile("""
        import socket, pytest
        @pytest.mark.live_network
        def test_live():
            try:
                socket.getaddrinfo("example.invalid", 443)
            except OSError:
                pass
    """)
    r = inner.runpytest_subprocess("-p", "no:cacheprovider")
    r.stdout.fnmatch_lines(["*made 1 blocked network call*"])         # the marker alone does not lift it
