"""
Phân quyền Coinpool V2: token của USER THƯỜNG không được vào endpoint admin.

GET gọi bình thường. POST/PUT/PATCH gửi body JSON hỏng (nếu thiếu kiểm tra quyền thì hành động vẫn không chạy được).
DELETE không body chỉ chạy khi ALLOW_DESTRUCTIVE=1.
Endpoint admin = đường dẫn bắt đầu bằng /admin hoặc /keyring (đổi bằng tests.admin_prefixes trong config.json).
Cần một token user thường, lấy theo thứ tự:
  1. USER_AUTH_HEADER trong .env (token dán sẵn, gồm cả "Bearer ")
  2. TEST_PRIVATE_KEY: ví TEST đăng nhập qua /auth/sign (giả định giống Keyring: trả JSON có token)
Không có cả hai thì các test bị bỏ qua.

Lưu ý: nếu ví TEST của bạn cũng là tài khoản admin thì test sẽ fail; dùng ví khác.
"""
import json
import os
from pathlib import Path

import pytest
import requests

BASE = os.environ["BASE_URL"].rstrip("/")
SCHEME = os.environ.get("AUTH_SCHEME", "Bearer ")
ADDR = "0x0000000000000000000000000000000000000001"
SPEC = json.loads(Path(os.environ["SPEC_FILE"]).read_text(encoding="utf-8"))
_cfg_file = os.environ.get("PROJECT_CONFIG")
_cfg = json.loads(Path(_cfg_file).read_text(encoding="utf-8")) if _cfg_file and Path(_cfg_file).exists() else {}
ADMIN_PREFIXES = tuple(_cfg.get("tests", {}).get("admin_prefixes", ["/admin", "/keyring"]))
ALLOW_DESTRUCTIVE = os.environ.get("ALLOW_DESTRUCTIVE") == "1"


def _find(data, names):
    if isinstance(data, dict):
        for n in names:
            if data.get(n):
                return data[n]
        for v in data.values():
            f = _find(v, names)
            if f:
                return f
    return None


def _user_token():
    if os.environ.get("USER_AUTH_HEADER"):
        return os.environ["USER_AUTH_HEADER"]
    key = os.environ.get("TEST_PRIVATE_KEY")
    if not key:
        return None
    from eth_account import Account
    from eth_account.messages import encode_defunct
    acct = Account.from_key(key)
    r = requests.get(f"{BASE}/auth/sign", params={"address": acct.address}, timeout=20)
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text
    message = body if isinstance(body, str) else _find(body, ["message"])
    sig = Account.sign_message(encode_defunct(text=message), key).signature.hex()
    r = requests.post(f"{BASE}/auth/sign", json={"message": message, "signature": sig if sig.startswith("0x") else "0x" + sig}, timeout=20)
    token = _find(r.json(), ["token", "accessToken", "access_token", "jwt"])
    return SCHEME + token if token else None


def _admin_gets():
    out = []
    for path, item in SPEC["paths"].items():
        if "get" not in item or not (path.startswith(ADMIN_PREFIXES)):
            continue
        url, query = path, {}
        for p in item["get"].get("parameters", []):
            if p["in"] == "path":
                url = url.replace("{%s}" % p["name"], ADDR)
            elif p["in"] == "query" and p.get("required"):
                query[p["name"]] = (p.get("enum") or [ADDR if "address" in p["name"].lower() else "test"])[0]
        out.append(pytest.param(url, query, id=f"GET {path}"))
    return out


@pytest.fixture(scope="module")
def user_hdr():
    t = _user_token()
    if not t:
        pytest.skip("Cần USER_AUTH_HEADER hoặc TEST_PRIVATE_KEY (token user thường)")
    return {"Authorization": t}


@pytest.mark.parametrize("url,query", _admin_gets())
def test_user_token_forbidden_on_admin(url, query, user_hdr):
    r = requests.get(BASE + url, headers=user_hdr, params=query, timeout=20)
    assert r.status_code in (401, 403), f"token user thường vào được endpoint admin: {r.status_code} {r.text[:200]}"


def _admin_writes():
    out = []
    for path, item in SPEC["paths"].items():
        if not path.startswith(ADMIN_PREFIXES):
            continue
        for m in ("post", "put", "patch", "delete"):
            op = item.get(m)
            if not op:
                continue
            params = item.get("parameters", []) + op.get("parameters", [])
            has_body = "requestBody" in op or any(p.get("in") == "body" for p in params)
            if not has_body and not ALLOW_DESTRUCTIVE:
                continue
            url = path
            for p in params:
                if p.get("in") == "path":
                    url = url.replace("{%s}" % p["name"], ADDR)
            out.append(pytest.param(m, url, has_body, id=f"{m.upper()} {path}"))
    return out


@pytest.mark.parametrize("method,url,has_body", _admin_writes())
def test_user_token_forbidden_on_admin_write(method, url, has_body, user_hdr):
    kw = {"data": "{khong phai json", "headers": {**user_hdr, "Content-Type": "application/json"}} if has_body else {"headers": user_hdr}
    r = requests.request(method.upper(), BASE + url, timeout=20, **kw)
    assert r.status_code in (401, 403), f"token user thường được phép {method.upper()} endpoint admin: {r.status_code} {r.text[:200]}"
