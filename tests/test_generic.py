"""
Bộ test CHUNG: tự sinh từ swagger.json của dự án đang chọn, không cần sửa cho từng dự án.

Chạy: python run.py test

Các nhóm test:
  1. GET nào cũng không được trả 5xx (tham số lấy mẫu từ spec)
  2. Route không tồn tại không được trả 5xx
  3. Endpoint yêu cầu đăng nhập: không gửi token  -> phải bị từ chối (401/403)
  4. Endpoint yêu cầu đăng nhập: gửi token sai    -> phải bị từ chối (401/403)
  5. Endpoint ghi dữ liệu có nhận body: gửi JSON hỏng (có token) -> không được 5xx
  6. GET có tham số enum: gửi giá trị ngoài danh sách -> phải bị từ chối bằng 4xx (không 2xx, không 5xx)

An toàn: các test 3-5 gửi body hỏng nên dù API có lỗ hổng thì hành động cũng không chạy.
Endpoint nguy hiểm không có body (vd POST /clear-cache) bị bỏ qua trừ khi ALLOW_DESTRUCTIVE=1.

Tinh chỉnh trong config.json, khoá "tests":
  "skip":          ["GET /health", ...]          bỏ qua endpoint ("METHOD /path")
  "auth_statuses": [401, 403]                    mã được coi là "từ chối do thiếu quyền"
  "assume_protected": true                       coi MỌI endpoint là cần đăng nhập (khi spec không khai báo security)
  "public":        ["POST /auth/login", ...]     endpoint công khai dù spec khai báo security
  "samples":       {"orderId": "123"}            giá trị mẫu theo TÊN tham số (path/query)
  "max_get_ms":    3000                          GET chậm hơn mức này thì fail (bỏ trống = không kiểm tra)
"""
import json
import os
import re
from pathlib import Path

import pytest
import requests

BASE = os.environ.get("BASE_URL", "").rstrip("/")
SPEC_FILE = os.environ.get("SPEC_FILE", "")
AUTH_HEADER = os.environ.get("AUTH_HEADER") or ""
ALLOW_DESTRUCTIVE = os.environ.get("ALLOW_DESTRUCTIVE") == "1"
TIMEOUT = 20
METHODS = ("get", "post", "put", "patch", "delete")
MUTATING = ("post", "put", "patch", "delete")
RISKY_WORDS = ["delete", "remove", "destroy", "purge", "drop", "clear", "reset", "truncate", "wipe", "flush",
               "send", "broadcast", "publish", "transfer", "withdraw", "pay", "refund", "charge", "cancel",
               "revoke", "disable", "ban"]

if not BASE or not SPEC_FILE:
    pytest.exit("Thiếu BASE_URL / SPEC_FILE. Chạy bằng: python run.py test", returncode=2)
if not Path(SPEC_FILE).exists():
    pytest.exit("Chưa có swagger.json. Chạy trước: python run.py spec", returncode=2)

SPEC = json.loads(Path(SPEC_FILE).read_text(encoding="utf-8"))


def _load_config():
    f = os.environ.get("PROJECT_CONFIG")
    return json.loads(Path(f).read_text(encoding="utf-8")) if f and Path(f).exists() else {}


CONFIG = _load_config()
T = CONFIG.get("tests", {})
SKIP = set(T.get("skip", []))
PUBLIC = set(T.get("public", []))
AUTH_STATUSES = set(T.get("auth_statuses", [401, 403]))
SAMPLES = T.get("samples", {})
FIELD_SAMPLES = CONFIG.get("field_samples", {})
# "risky": false trong config = loại trừ khỏi danh sách nguy hiểm
RISKY_CFG = {(r["method"].upper(), r["path"]) for r in CONFIG.get("risky", []) if r.get("risky", True) is not False}
NOT_RISKY_CFG = {(r["method"].upper(), r["path"]) for r in CONFIG.get("risky", []) if r.get("risky", True) is False}


# ------------------------------------------------------------------ đọc spec
def _resolve(obj):
    """Giải $ref nội bộ (#/...) ở mức một tầng."""
    if isinstance(obj, dict) and "$ref" in obj and obj["$ref"].startswith("#/"):
        node = SPEC
        for part in obj["$ref"][2:].split("/"):
            node = node.get(part.replace("~1", "/").replace("~0", "~"), {})
        return node
    return obj


def _schema(p):
    return _resolve(p.get("schema") or p)


def sample(p):
    """Giá trị hợp lệ cho một tham số, theo thứ tự: cấu hình > enum/default > pattern > kiểu."""
    name = p.get("name", "")
    if name in SAMPLES:
        return SAMPLES[name]
    if name in FIELD_SAMPLES:
        return FIELD_SAMPLES[name]
    s = _schema(p)
    if "enum" in s:
        return s["enum"][0]
    if s.get("type") == "array":
        return sample({"schema": _resolve(s.get("items", {}))})
    pat = s.get("pattern", "")
    if "0x" in pat:
        return "0x" + "0" * 39 + "1"
    if "{24}" in pat:
        return "0" * 24
    if s.get("format") == "uuid":
        return "00000000-0000-0000-0000-000000000000"
    if s.get("type") in ("integer", "number"):
        return s.get("minimum", 1)
    if s.get("type") == "boolean":
        return "true"
    if name in ("id", "_id") or name.endswith("Id") or name.endswith("_id"):
        return "0" * 24     # id hợp lệ về định dạng nhưng không tồn tại; sai thì khai báo trong tests.samples
    return s.get("default", "test")


def operations():
    """Sinh (method, path, op, params). params = tham số cấp path + cấp operation."""
    for path, item in SPEC.get("paths", {}).items():
        item = _resolve(item)
        shared = [_resolve(p) for p in item.get("parameters", [])]
        for m in METHODS:
            if m in item:
                op = item[m]
                params = shared + [_resolve(p) for p in op.get("parameters", [])]
                yield m, path, op, params


def is_protected(method, path, op):
    key = f"{method.upper()} {path}"
    if key in PUBLIC:
        return False
    if "security" in op:
        return bool(op["security"])
    if SPEC.get("security"):
        return True
    return bool(T.get("assume_protected"))


def has_body(op, params):
    return "requestBody" in op or any(p.get("in") == "body" for p in params)


def is_risky(method, path):
    if (method.upper(), path) in NOT_RISKY_CFG:
        return False
    if (method.upper(), path) in RISKY_CFG or method == "delete":
        return True
    words = set(re.findall(r"[a-z]+", re.sub(r"([a-z])([A-Z])", r"\1 \2", path).lower()))
    return bool(words & set(RISKY_WORDS))


def build(path, params, only_required_query=True):
    url, query = path, {}
    for p in params:
        if p.get("in") == "path":
            url = url.replace("{%s}" % p["name"], str(sample(p)))
        elif p.get("in") == "query" and (p.get("required") or not only_required_query):
            query[p["name"]] = sample(p)
    return url, query


def call(method, url, headers=None, **kw):
    return requests.request(method.upper(), BASE + url, headers=headers or {}, timeout=TIMEOUT, **kw)


def _cases(pred):
    out = []
    for m, path, op, params in operations():
        key = f"{m.upper()} {path}"
        if key in SKIP or not pred(m, path, op, params):
            continue
        out.append(pytest.param(m, path, op, params, id=key))
    return out


ARGS = "method,path,op,params"


# ------------------------------------------------------------------ 0. spec
def test_spec_has_paths():
    assert SPEC.get("paths"), "swagger.json không có paths"


# ------------------------------------------------------------------ 1. GET không 5xx
@pytest.mark.parametrize(ARGS, _cases(lambda m, p, o, pr: m == "get"))
def test_get_never_5xx(method, path, op, params):
    url, query = build(path, params)
    hdr = {"Authorization": AUTH_HEADER} if AUTH_HEADER else {}
    r = call("GET", url, hdr, params=query)
    assert r.status_code < 500, f"{url} -> {r.status_code}: {r.text[:200]}"
    limit = T.get("max_get_ms")
    if limit:
        assert r.elapsed.total_seconds() * 1000 <= limit, f"{url} chậm: {r.elapsed.total_seconds():.2f}s"


# ------------------------------------------------------------------ 2. route lạ
@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
def test_unknown_route_not_5xx(method):
    r = call(method, "/__khong_ton_tai__/abc")
    assert r.status_code < 500, f"{method} route lạ -> {r.status_code}: {r.text[:200]}"


# ------------------------------------------------------------------ 3-4. xác thực
def _safe_without_token(m, path, op, params):
    """Chỉ chạy khi nếu API quên kiểm tra token thì hành động vẫn không thực thi được."""
    if not is_protected(m, path, op):
        return False
    if m == "get" or not is_risky(m, path):
        return True
    return ALLOW_DESTRUCTIVE or has_body(op, params) or any(p.get("in") == "path" for p in params)


def _unauth_request(m, path, op, params, headers):
    url, query = build(path, params)
    kw = {"params": query}
    if m in MUTATING:
        if has_body(op, params):
            # JSON hỏng: tới được handler mới biết, nên thiếu kiểm tra token cũng không chạy được hành động
            kw["data"] = "{khong phai json"
            headers = {**headers, "Content-Type": "application/json"}
    return call(m, url, headers, **kw)


@pytest.mark.parametrize(ARGS, _cases(_safe_without_token))
def test_protected_rejects_missing_token(method, path, op, params):
    r = _unauth_request(method, path, op, params, {})
    assert r.status_code in AUTH_STATUSES, (
        f"không gửi token nhưng nhận {r.status_code} (mong {sorted(AUTH_STATUSES)}): {r.text[:200]}. "
        f"Nếu API kiểm tra dữ liệu trước token thì thêm giá trị đúng vào tests.samples; "
        f"nếu endpoint thực ra công khai thì thêm vào tests.public.")


@pytest.mark.parametrize(ARGS, _cases(_safe_without_token))
def test_protected_rejects_bad_token(method, path, op, params):
    r = _unauth_request(method, path, op, params, {"Authorization": "Bearer token.sai.hoan.toan"})
    assert r.status_code in AUTH_STATUSES, f"token sai nhưng nhận {r.status_code}: {r.text[:200]}"


# ------------------------------------------------------------------ 5. body hỏng khi đã đăng nhập
def _bad_body_ok(m, path, op, params):
    if m not in MUTATING or not has_body(op, params) or not AUTH_HEADER:
        return False
    return ALLOW_DESTRUCTIVE or not is_risky(m, path)


@pytest.mark.parametrize(ARGS, _cases(_bad_body_ok))
def test_malformed_json_not_5xx(method, path, op, params):
    url, query = build(path, params)
    r = call(method, url, {"Authorization": AUTH_HEADER, "Content-Type": "application/json"},
             params=query, data="{khong phai json")
    assert r.status_code < 500, f"JSON hỏng -> {r.status_code}: {r.text[:200]}"


# ------------------------------------------------------------------ 6. giá trị ngoài enum
def _enum_cases():
    out = []
    for m, path, op, params in operations():
        key = f"GET {path}"
        if m != "get" or key in SKIP:
            continue
        for p in params:
            if p.get("in") == "query" and "enum" in _schema(p) and _schema(p).get("type") != "array":
                out.append(pytest.param(path, params, p["name"], id=f"{key} ?{p['name']}"))
    return out


@pytest.mark.parametrize("path,params,name", _enum_cases())
def test_invalid_enum_rejected(path, params, name):
    url, query = build(path, params)
    query[name] = "__gia_tri_khong_hop_le__"
    hdr = {"Authorization": AUTH_HEADER} if AUTH_HEADER else {}
    r = call("GET", url, hdr, params=query)
    assert 400 <= r.status_code < 500, f"{name} ngoài enum nhưng nhận {r.status_code}: {r.text[:200]}"
