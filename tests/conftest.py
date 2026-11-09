"""Ghi lại mọi request mà từng test đã gửi, để run.py đưa vào bảng kết quả (cách gọi + tham số)."""
import json

import pytest
import requests

_calls = []
_orig = requests.sessions.Session.request


def _mask(headers):
    out = {}
    for k, v in (headers or {}).items():
        v = str(v)
        # token thật rất dài: che đi, token giả ngắn thì giữ nguyên để tester thấy mình gửi gì
        out[k] = "<token>" if k.lower() == "authorization" and len(v) > 40 else v
    return out


def _recording_request(self, method, url, **kw):
    resp = _orig(self, method, url, **kw)
    req = resp.request
    body = req.body
    if isinstance(body, bytes):
        body = body.decode("utf-8", "replace")
    _calls.append({"method": req.method, "url": req.url,
                   "headers": _mask({k: v for k, v in req.headers.items()
                                     if k.lower() in ("authorization", "content-type")}),
                   "body": (body or "")[:500]})
    return resp


requests.sessions.Session.request = _recording_request


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    _calls.clear()  # chỉ tính request trong thân test, không tính bước đăng nhập ở fixture
    yield
    if _calls:
        item.user_properties.append(("request", json.dumps(_calls, ensure_ascii=False)))
