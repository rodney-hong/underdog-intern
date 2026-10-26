"""
Hosted screenshot reading (migration plan v2, step 0.10), every HTTP call mocked:
  - Ollama Cloud (gemma4:31b, Bearer OLLAMA_API_KEY) reads the cards, one call at a time;
  - a usage-limit / quota error switches to the Gemini API (gemma-4-31b-it, x-goog-api-key,
    image inline), logs the switch, and keeps using Gemini for the cooldown;
  - Ollama Cloud down or without a key also falls back; both failing gives one clear
    message, which /screenshot/extract returns as the 503 detail;
  - images and crops are only ever encoded to memory.
"""
import base64
import io
import os
import sys
import threading
import time

import httpx
import pytest
from PIL import Image

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import screenshot_import as si          # noqa: E402
import synthetic_screenshot as ss       # noqa: E402

CARD = {"player_name": "Jalen Williams", "stat_label": "Rebounds", "line": 5.5, "buttons": ["Higher", "Lower"]}


class Fake:
    """httpx.post stand-in: per-service behaviour, call log, peak concurrency per service."""
    def __init__(self):
        self.calls, self.mode = [], {"ollama": "ok", "gemini": "ok"}
        self.active, self.peak = {"ollama": 0, "gemini": 0}, {"ollama": 0, "gemini": 0}
        self.lock = threading.Lock()

    def __call__(self, url, json=None, timeout=None, headers=None):
        svc = "gemini" if url.startswith(si.GEMINI_URL) else "ollama"
        with self.lock:
            self.calls.append((svc, url, headers or {}, json))
            self.active[svc] += 1
            self.peak[svc] = max(self.peak[svc], self.active[svc])
        try:
            time.sleep(0.01)
            req = httpx.Request("POST", url)
            mode = self.mode[svc]
            if mode == "quota":
                text = ('{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED"}}' if svc == "gemini"
                        else '{"error": "you have reached your weekly usage limit"}')
                return httpx.Response(429 if svc == "gemini" else 403, request=req, text=text)
            if mode == "down":
                raise httpx.ConnectError("connection refused", request=req)
            if svc == "ollama":
                return httpx.Response(200, request=req, json={"message": {"content": __import__("json").dumps(CARD)}})
            parts = [{"text": "thinking about it", "thought": True},
                     {"text": "Here you go:\n```json\n" + __import__("json").dumps(CARD) + "\n```"}]
            return httpx.Response(200, request=req, json={"candidates": [{"content": {"parts": parts}}]})
        finally:
            with self.lock:
                self.active[svc] -= 1


@pytest.fixture
def fake(monkeypatch):
    f = Fake()
    monkeypatch.setattr(si.httpx, "post", f)
    monkeypatch.setattr(si.time, "sleep", lambda s: None)          # the 5xx retry's pause
    monkeypatch.setattr(si, "_quota_until", {})
    monkeypatch.setattr(si, "detect_cards", lambda img: [(0, 0, 60, 40), (60, 0, 120, 40), (0, 40, 60, 80)])
    monkeypatch.setenv("SCREENSHOT_EXTRACTOR", "ollama_cloud")
    monkeypatch.setenv("OLLAMA_API_KEY", "ollama-test-key")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-test-key")
    real_save = Image.Image.save

    def memory_only(self, fp, *a, **k):
        assert isinstance(fp, io.BytesIO), f"image written to {fp!r}"
        return real_save(self, fp, *a, **k)

    monkeypatch.setattr(Image.Image, "save", memory_only)
    return f


def _png():
    return ss.render()


def test_ollama_cloud_reads_the_cards_one_at_a_time(fake):
    name, rows = si.extract_rows(_png())
    assert name == "ollama_cloud" and len(rows) == 3
    assert all(r["player_name"] == "Jalen Williams" and r["stat"] == "Rebounds" and r["line"] == 5.5 for r in rows)
    svc, url, headers, body = fake.calls[0]
    assert url == "https://ollama.com/api/chat" and headers == {"Authorization": "Bearer ollama-test-key"}
    assert body["model"] == "gemma4:31b" and body["format"] == "json" and body["think"] is False
    assert len(fake.calls) == 3 * len(si.READ_SCALES) and {c[0] for c in fake.calls} == {"ollama"}
    assert fake.peak["ollama"] == 1                                 # SCREENSHOT_OLLAMA_CLOUD_PARALLEL=1


def test_usage_limit_switches_to_gemini_and_logs_it(fake, capsys):
    fake.mode["ollama"] = "quota"
    name, rows = si.extract_rows(_png())
    assert name == "gemini" and len(rows) == 3 and rows[0]["line"] == 5.5
    assert "Ollama Cloud failed (Ollama Cloud usage limit reached (HTTP 403)); switching to Gemini" in capsys.readouterr().out
    g = [c for c in fake.calls if c[0] == "gemini"]
    svc, url, headers, body = g[0]
    assert url == "https://generativelanguage.googleapis.com/v1beta/models/gemma-4-31b-it:generateContent"
    assert headers == {"x-goog-api-key": "gemini-test-key"}
    inline = body["contents"][0]["parts"][0]["inline_data"]
    assert inline["mime_type"] == "image/png" and Image.open(io.BytesIO(base64.b64decode(inline["data"]))).size
    assert fake.peak["gemini"] <= si.GEMINI_PARALLEL
    # within the cooldown Ollama Cloud is not asked again
    n_ollama = sum(c[0] == "ollama" for c in fake.calls)
    fake.mode["ollama"] = "ok"
    assert si.extract_rows(_png())[0] == "gemini"
    assert sum(c[0] == "ollama" for c in fake.calls) == n_ollama
    assert "usage limit reached recently: reading with Gemini" in capsys.readouterr().out


def test_after_the_cooldown_ollama_cloud_is_tried_again(fake, monkeypatch):
    fake.mode["ollama"] = "quota"
    si.extract_rows(_png())
    monkeypatch.setattr(si, "_quota_until", {"ollama_cloud": time.monotonic() - 1})
    fake.mode["ollama"] = "ok"
    assert si.extract_rows(_png())[0] == "ollama_cloud"


@pytest.mark.parametrize("trouble", ["down", "no key"])
def test_ollama_cloud_down_or_unset_also_falls_back(fake, monkeypatch, trouble):
    if trouble == "down":
        fake.mode["ollama"] = "down"
    else:
        monkeypatch.delenv("OLLAMA_API_KEY")
    assert si.extract_rows(_png())[0] == "gemini"
    assert si._quota_until == {}                                    # not a quota error: no cooldown


@pytest.mark.parametrize("ollama, gemini", [("quota", "quota"), ("down", "down"), ("quota", "down")])
def test_both_failing_gives_one_clear_message(fake, ollama, gemini):
    fake.mode.update(ollama=ollama, gemini=gemini)
    with pytest.raises(si.ExtractorUnavailable) as e:
        si.extract_rows(_png())
    msg = str(e.value)
    assert msg.startswith("Screenshot reading is unavailable right now: Ollama Cloud (")
    assert "and Gemini (" in msg and "both failed. Enter the lines by hand" in msg


def test_both_failing_reaches_the_app_as_a_503(fake):
    from fastapi.testclient import TestClient
    import main
    fake.mode.update(ollama="quota", gemini="quota")
    r = TestClient(main.app).post("/screenshot/extract", files={"file": ("s.png", _png(), "image/png")})
    assert r.status_code == 503 and "both failed" in r.json()["detail"]


def test_gemini_selected_directly_and_local_ollama_still_selectable(fake, monkeypatch):
    monkeypatch.setenv("SCREENSHOT_EXTRACTOR", "gemini")
    assert si.extract_rows(_png())[0] == "gemini" and {c[0] for c in fake.calls} == {"gemini"}
    fake.calls.clear()
    monkeypatch.setenv("SCREENSHOT_EXTRACTOR", "ollama")
    assert si.extract_rows(_png())[0] == "ollama"
    svc, url, headers, body = fake.calls[0]
    assert url == f"{si.OLLAMA_URL}/api/chat" and headers == {} and body["model"] == si.OLLAMA_MODEL


def test_a_quota_error_on_one_card_rereads_the_whole_image(fake, monkeypatch):
    """A limit hit midway would leave cards unread: the whole image goes to the fallback."""
    calls = {"n": 0}
    real = fake.__call__

    def flaky(url, json=None, timeout=None, headers=None):
        if not url.startswith(si.GEMINI_URL):
            calls["n"] += 1
            if calls["n"] == 3:
                req = httpx.Request("POST", url)
                return httpx.Response(429, request=req, text="rate limit")
        return real(url, json=json, timeout=timeout, headers=headers)

    monkeypatch.setattr(si.httpx, "post", flaky)
    name, rows = si.extract_rows(_png())
    assert name == "gemini" and len(rows) == 3 and not any(r.get("unreadable") for r in rows)


def test_quota_detection():
    req = httpx.Request("POST", "https://x")
    assert si._is_quota(httpx.Response(429, request=req))
    assert si._is_quota(httpx.Response(402, request=req))
    assert si._is_quota(httpx.Response(403, request=req, text="This model requires a subscription"))
    assert not si._is_quota(httpx.Response(401, request=req, text="unauthorized"))
    assert not si._is_quota(httpx.Response(500, request=req, text="quota service crashed"))
    assert si._json_text('Sure! {"a": 1} hope that helps') == {"a": 1}
    assert si._json_text('```json\n{"a": 2}\n```') == {"a": 2}
