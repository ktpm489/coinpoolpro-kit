"""
Test BẢO MẬT bổ sung (ngoài test_generic.py và test_admin_access.py).

Chạy: python run.py test -k security

Nhóm test:
  1. JWT giả mạo: alg=none, ký bằng secret yếu, sửa payload/chữ ký của token thật, scheme lạ, token rất dài
  2. Token đặt sai chỗ (query string) không được coi là đã đăng nhập
  3. Injection (SQL, NoSQL, XSS, path traversal, command) vào tham số GET: không 5xx, không lộ lỗi DB/stack trace
  4. Prototype pollution + trùng tham số (HTTP parameter pollution) không gây 5xx
  5. Đăng nhập (/auth/sign, /auth/admin/login): payload tấn công, chữ ký sai, body quá lớn, sai Content-Type
  6. Rate limit đăng nhập (chỉ chạy khi RUN_RATE_LIMIT=1)
  7. Header bảo mật, cookie, CORS, HTTP -> HTTPS
  8. Phương thức HTTP không khai báo (PUT/PATCH/DELETE/TRACE) không được chạy khi chưa đăng nhập
  9. File nhạy cảm/trang debug không được công khai
 10. Response không lộ stack trace, đường dẫn máy chủ, tên trường bí mật

An toàn: chỉ gửi payload tấn công vào GET và 2 endpoint đăng nhập; các request khác không có token
hoặc dùng body hỏng, nên dù API có lỗ hổng thì hành động ghi dữ liệu cũng không chạy.

Tinh chỉnh trong config.json, khoá "tests":
  "jwt_probe":    ["GET /auth/me", ...]   endpoint dùng để thử token giả (mặc định tự chọn tối đa 3 GET cần token)
  "security_skip": ["header Strict-Transport-Security", ...]   bỏ qua test theo id
"""
import base64
import hashlib
import hmac
import json
import os
import re
import time
from urllib.parse import quote, urlencode

import pytest
import requests

import test_generic as g

BASE = g.BASE
AUTH_HEADER = g.AUTH_HEADER
AUTH_STATUSES = g.AUTH_STATUSES
TIMEOUT = g.TIMEOUT
SECURITY_SKIP = set(g.T.get("security_skip", []))
EVIL_ORIGIN = "https://evil.example"

# Tín hiệu lộ thông tin nội bộ trong response
LEAK_RE = re.compile(
    r"(Traceback \(most recent call last\)|\bat\s+[\w.<>$]+\s+\([^)\n]*:\d+:\d+\)|node_modules[/\\]"
    r"|SequelizeDatabaseError|QueryFailedError|PrismaClientKnownRequestError|MongoServerError|MongooseError"
    r"|syntax error at or near|You have an error in your SQL syntax|unterminated quoted string"
    r"|ORA-\d{5}|SQLSTATE\[|SQLITE_ERROR|pg_catalog|ECONNREFUSED|ENOTFOUND|\bTypeError:|\bReferenceError:"
    r"|\"stack\"\s*:|(?:/home|/usr|/var|/app|/srv|/opt)/[\w./-]+\.(?:js|ts|py)\b|[A-Z]:\\\\[\w\\.-]+)",
    re.I)
SECRET_KEY_RE = re.compile(
    r"\"(password|passwd|pwd|private_?key|secret(?:_?key)?|mnemonic|seed(?:_?phrase)?|api_?key|"
    r"client_?secret|access_?key)\"\s*:\s*\"[^\"]+\"", re.I)

PAYLOADS = {
    "sqli-quote": "' OR '1'='1' -- ",
    "sqli-union": "1 UNION SELECT NULL,NULL,version()--",
    "sqli-stacked": "1; DROP TABLE users;--",
    "nosql": '{"$ne": null}',
    "xss": "<script>alert(1)</script>",
    "path-traversal": "../../../../etc/passwd",
    "path-traversal-enc": "..%2f..%2f..%2fetc%2fpasswd",
    "cmd-injection": "; cat /etc/passwd #",
    "template-injection": "{{7*7}}${7*7}<%= 7*7 %>",
    "null-byte": "abc%00def",
    "long-string": "A" * 5000,
    "unicode": "‮﻿\U0001f4a5" * 20,
}


def _skip_if_configured(test_id):
    if test_id in SECURITY_SKIP:
        pytest.skip(f"Bỏ qua theo tests.security_skip: {test_id}")


def _get(path, headers=None, **kw):
    kw.setdefault("allow_redirects", False)
    return requests.get(BASE + path, headers=headers or {}, timeout=TIMEOUT, **kw)


def _b64(data):
    if isinstance(data, (dict, list)):
        data = json.dumps(data, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(part):
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _split_auth(value):
    """'Bearer a.b.c' -> ('Bearer ', 'a.b.c')."""
    if " " in value:
        scheme, token = value.split(" ", 1)
        return scheme + " ", token.strip()
    return "", value.strip()


def _real_jwt():
    scheme, token = _split_auth(AUTH_HEADER) if AUTH_HEADER else ("", "")
    parts = token.split(".")
    if len(parts) == 3:
        try:
            json.loads(_unb64(parts[0]))
            json.loads(_unb64(parts[1]))
            return scheme, parts
        except Exception:  # noqa: BLE001
            return None
    return None


def _protected_probes():
    """Tối đa 3 GET thật sự cần đăng nhập để thử token giả.

    Chọn bằng cách gọi thử: chỉ lấy endpoint trả 401/403 khi không có token. Endpoint spec khai báo cần
    token nhưng thực tế công khai (200) hoặc kiểm tra tham số trước (400) sẽ làm test sai nghĩa nên bị loại.
    Ưu tiên /auth/me, /admin, /keyring. Muốn tự chọn: tests.jwt_probe = ["GET /auth/me", ...] trong config.json.
    """
    cfg = g.T.get("jwt_probe")
    cands = []
    for m, path, op, params in g.operations():
        if m != "get" or not g.is_protected(m, path, op):
            continue
        key = f"GET {path}"
        if cfg and key not in cfg:
            continue
        url, query = g.build(path, params)
        rank = 0 if path == "/auth/me" else 1 if path.startswith(("/admin", "/keyring")) else 2
        cands.append((rank, key, url, query))
    cands.sort(key=lambda c: c[0])
    out = []
    for _, key, url, query in cands:
        try:
            r = requests.get(BASE + url, params=query, timeout=TIMEOUT, allow_redirects=False)
        except requests.exceptions.RequestException:
            continue
        if r.status_code in AUTH_STATUSES:
            out.append((key, url, query))
        if len(out) >= (len(cfg) if cfg else 3):
            break
    return out


REJECT = AUTH_STATUSES | {400, 413, 431}
PROBES = _protected_probes()


def _real_token_works(url, query):
    """Token thật trong AUTH_HEADER có đang được chấp nhận không (hết hạn thì các test sửa token vô nghĩa)."""
    if not AUTH_HEADER:
        return False
    try:
        r = requests.get(BASE + url, params=query, headers={"Authorization": AUTH_HEADER}, timeout=TIMEOUT)
    except requests.exceptions.RequestException:
        return False
    return r.status_code not in REJECT


def _probe_params():
    return [pytest.param(url, query, id=key) for key, url, query in PROBES]


# ------------------------------------------------------------------ 1. JWT giả mạo
def _forged_tokens():
    now = int(time.time())
    claims = {"sub": "0x0000000000000000000000000000000000000001", "address": "0x0000000000000000000000000000000000000001",
              "role": "admin", "isAdmin": True, "iat": now, "exp": now + 3600}
    out = {}
    for alg in ("none", "None", "NONE", "nOnE"):
        out[f"alg={alg}, không chữ ký"] = f"{_b64({'alg': alg, 'typ': 'JWT'})}.{_b64(claims)}."
    out["alg=none, chữ ký rác"] = f"{_b64({'alg': 'none', 'typ': 'JWT'})}.{_b64(claims)}.AAAA"
    for secret in ("secret", "changeme", "jwt", "password", "your-256-bit-secret", ""):
        head, body = _b64({"alg": "HS256", "typ": "JWT"}), _b64(claims)
        sig = hmac.new(secret.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest()
        out[f"HS256 ký bằng secret yếu '{secret}'"] = f"{head}.{body}.{_b64(sig)}"
    out["đã hết hạn (exp=2001)"] = (f"{_b64({'alg': 'HS256', 'typ': 'JWT'})}."
                                    f"{_b64({**claims, 'exp': 1000000000})}.{_b64(b'x' * 32)}")
    out["thiếu phần chữ ký"] = f"{_b64({'alg': 'HS256', 'typ': 'JWT'})}.{_b64(claims)}"
    out["chuỗi rỗng"] = ""
    out["token = null"] = "null"
    out["token = undefined"] = "undefined"
    out["4 phần"] = "a.b.c.d"
    out["token 10KB"] = "A" * 10_000
    return out


FORGED = _forged_tokens()
# Mã chấp nhận khi từ chối: 401/403, hoặc 400/413/431 khi token sai định dạng hoặc quá lớn


@pytest.mark.parametrize("name", list(FORGED))
@pytest.mark.parametrize("url,query", _probe_params())
def test_forged_jwt_rejected(url, query, name):
    r = _get(url, {"Authorization": "Bearer " + FORGED[name]}, params=query)
    assert r.status_code in REJECT, f"token giả ({name}) nhận {r.status_code} (mong {sorted(REJECT)}): {r.text[:200]}"


@pytest.mark.parametrize("value", ["Bearer", "Bearer ", "Basic dXNlcjpwYXNz", "Token abc.def.ghi", "Bearer Bearer abc"])
@pytest.mark.parametrize("url,query", _probe_params())
def test_odd_auth_scheme_rejected(url, query, value):
    r = _get(url, {"Authorization": value}, params=query)
    assert r.status_code in REJECT, f"Authorization '{value}' nhận {r.status_code}: {r.text[:200]}"


def _tamper_cases():
    jwt = _real_jwt()
    if not jwt:
        return []
    return ["đổi payload, giữ chữ ký cũ", "đảo ký tự cuối chữ ký", "bỏ chữ ký", "đổi alg thành none", "đổi alg thành HS256 và ký lại bằng 'secret'"]


def _tampered(case):
    scheme, (h, p, s) = _real_jwt()
    claims = json.loads(_unb64(p))
    if case == "đổi payload, giữ chữ ký cũ":
        claims.update({"role": "admin", "isAdmin": True, "admin": True})
        return scheme + f"{h}.{_b64(claims)}.{s}"
    if case == "đảo ký tự cuối chữ ký":
        return scheme + f"{h}.{p}.{s[:-1]}{'A' if s[-1] != 'A' else 'B'}"
    if case == "bỏ chữ ký":
        return scheme + f"{h}.{p}."
    if case == "đổi alg thành none":
        return scheme + f"{_b64({'alg': 'none', 'typ': 'JWT'})}.{p}."
    head = _b64({"alg": "HS256", "typ": "JWT"})
    sig = hmac.new(b"secret", f"{head}.{p}".encode(), hashlib.sha256).digest()
    return scheme + f"{head}.{p}.{_b64(sig)}"


@pytest.mark.parametrize("case", _tamper_cases() or [pytest.param("-", marks=pytest.mark.skip(reason="AUTH_HEADER không phải JWT (3 phần) nên không thử sửa token thật"))])
@pytest.mark.parametrize("url,query", _probe_params())
def test_tampered_real_jwt_rejected(url, query, case):
    if not _real_token_works(url, query):
        pytest.skip("AUTH_HEADER hết hạn hoặc không dùng được ở endpoint này: sửa token cũng bị từ chối nên test vô nghĩa")
    r = _get(url, {"Authorization": _tampered(case)}, params=query)
    assert r.status_code in REJECT, f"token thật bị sửa ({case}) vẫn nhận {r.status_code}: {r.text[:200]}"


# ------------------------------------------------------------------ 2. token đặt sai chỗ
@pytest.mark.skipif(not AUTH_HEADER, reason="Cần AUTH_HEADER (token thật) để thử")
@pytest.mark.parametrize("where", ["token", "access_token", "jwt", "authorization"])
@pytest.mark.parametrize("url,query", _probe_params())
def test_token_in_query_not_accepted(url, query, where):
    if not _real_token_works(url, query):
        pytest.skip("AUTH_HEADER hết hạn hoặc không dùng được ở endpoint này: test sẽ luôn đạt một cách vô nghĩa")
    _, token = _split_auth(AUTH_HEADER)
    r = _get(url, params={**query, where: token})
    assert r.status_code in AUTH_STATUSES, (
        f"token thật đặt ở ?{where}= được chấp nhận ({r.status_code}): token lọt vào log/Referer, chỉ nên nhận qua header")


# ------------------------------------------------------------------ 3. injection vào GET
def _get_injectable():
    out = []
    for m, path, op, params in g.operations():
        key = f"GET {path}"
        if m != "get" or key in g.SKIP:
            continue
        names = [p["name"] for p in params
                 if p.get("in") in ("query", "path") and "enum" not in g._schema(p)
                 and g._schema(p).get("type", "string") == "string"]
        if names:
            out.append(pytest.param(path, params, names, id=key))
    return out


def _leak_check(r, label):
    m = LEAK_RE.search(r.text or "")
    assert not m, f"{label}: response lộ thông tin nội bộ ('{m.group(0)[:80]}'): {r.text[:200]}"


@pytest.mark.parametrize("path,params,names", _get_injectable())
def test_get_injection_safe(path, params, names):
    hdr = {"Authorization": AUTH_HEADER} if AUTH_HEADER else {}
    problems = []
    for label, payload in PAYLOADS.items():
        url, query = path, {}
        for p in params:
            injected = p["name"] in names
            if p.get("in") == "path":
                url = url.replace("{%s}" % p["name"], quote(payload, safe="") if injected else str(g.sample(p)))
            elif p.get("in") == "query" and (injected or p.get("required")):
                query[p["name"]] = payload if injected else g.sample(p)
        try:
            r = _get(url, hdr, params=query)
        except requests.exceptions.RequestException as e:
            problems.append(f"{label}: request lỗi {type(e).__name__}")
            continue
        text = r.text or ""
        if r.status_code >= 500:
            problems.append(f"{label}: {r.status_code}")
        elif LEAK_RE.search(text):
            problems.append(f"{label}: lộ lỗi nội bộ '{LEAK_RE.search(text).group(0)[:50]}'")
        elif label == "xss" and "html" in r.headers.get("content-type", "").lower() and payload in text:
            problems.append(f"{label}: payload XSS phản chiếu nguyên văn trong HTML")
        elif "root:x:0:0" in text:
            problems.append(f"{label}: đọc được /etc/passwd")
        elif label == "template-injection" and "7*7" not in text and re.search(r"(?<![\d{])49(?![\d}])", text):
            problems.append(f"{label}: có dấu hiệu template bị thực thi (7*7=49)")
    assert not problems, "; ".join(problems)


# ------------------------------------------------------------------ 4. prototype pollution, trùng tham số
def _get_with_query():
    return [pytest.param(path, params, id=f"GET {path}")
            for m, path, op, params in g.operations()
            if m == "get" and f"GET {path}" not in g.SKIP]


@pytest.mark.parametrize("path,params", _get_with_query())
def test_prototype_pollution_and_hpp_not_5xx(path, params):
    hdr = {"Authorization": AUTH_HEADER} if AUTH_HEADER else {}
    url, query = g.build(path, params)
    extra = "&".join(["__proto__[isAdmin]=true", "constructor[prototype][isAdmin]=true",
                      "__proto__.polluted=1", "limit[]=1&limit[]=2", "page=1&page=2&page=abc"])
    # nối thẳng vào query string để giữ nguyên ngoặc vuông và tham số trùng
    qs = "&".join(x for x in (urlencode(query, doseq=True), extra) if x)
    dup = ""
    if query:
        k, v = next(iter(query.items()))
        dup = f"&{quote(k)}={quote(str(v))}&{quote(k)}={quote(str(v))}2"
    r = requests.get(f"{BASE}{url}?{qs}{dup}", headers=hdr, timeout=TIMEOUT, allow_redirects=False)
    assert r.status_code < 500, f"pollution/HPP -> {r.status_code}: {r.text[:200]}"
    _leak_check(r, "pollution/HPP")


# ------------------------------------------------------------------ 5. endpoint đăng nhập
TOKEN_KEYS = ["token", "accessToken", "access_token", "jwt"]


def _has_token(r):
    try:
        return bool(_find(r.json(), TOKEN_KEYS))
    except Exception:  # noqa: BLE001
        return False


def _find(data, names):
    if isinstance(data, dict):
        for n in names:
            if data.get(n):
                return data[n]
        data = list(data.values())
    if isinstance(data, list):
        for v in data:
            f = _find(v, names)
            if f:
                return f
    return None


LOGIN_BODIES = {
    "SQLi trong username": {"username": "admin' OR '1'='1' -- ", "password": "x"},
    "SQLi trong password": {"username": "admin", "password": "' OR '1'='1"},
    "NoSQL operator $ne": {"username": {"$ne": None}, "password": {"$ne": None}},
    "NoSQL operator $gt": {"username": {"$gt": ""}, "password": {"$gt": ""}},
    "username là mảng": {"username": ["admin"], "password": ["x"]},
    "kiểu số/bool": {"username": 0, "password": True},
    "chuỗi rỗng": {"username": "", "password": ""},
    "null": {"username": None, "password": None},
    "username/password quá dài": {"username": "a" * 100_000, "password": "b" * 100_000},
    "trường thừa (mass assignment)": {"username": "qa-nonexistent", "password": "x", "role": "admin", "isAdmin": True},
    "__proto__": json.loads('{"username":"qa-nonexistent","password":"x","__proto__":{"isAdmin":true}}'),
}


@pytest.mark.parametrize("name", list(LOGIN_BODIES))
def test_admin_login_attack_payloads(name):
    r = requests.post(BASE + "/auth/admin/login", json=LOGIN_BODIES[name], timeout=TIMEOUT)
    assert r.status_code < 500, f"{name} -> {r.status_code}: {r.text[:200]}"
    assert not r.ok, f"{name} đăng nhập thành công ({r.status_code}): {r.text[:200]}"
    assert not _has_token(r), f"{name}: response chứa token dù đăng nhập sai"
    _leak_check(r, name)


def test_admin_login_wrong_password_same_message_as_unknown_user():
    """Không để lộ tài khoản có tồn tại hay không (user enumeration) qua thông báo/mã trả về."""
    user = os.environ.get("ADMIN_USERNAME")
    if not user:
        pytest.skip("Cần ADMIN_USERNAME trong .env để so sánh tài khoản có thật với tài khoản không tồn tại")
    a = requests.post(BASE + "/auth/admin/login", json={"username": user, "password": "sai-mat-khau-qa-123"}, timeout=TIMEOUT)
    b = requests.post(BASE + "/auth/admin/login", json={"username": "qa-khong-ton-tai-9z", "password": "sai-mat-khau-qa-123"}, timeout=TIMEOUT)
    assert a.status_code == b.status_code, f"user thật -> {a.status_code}, user không có -> {b.status_code}: lộ việc tài khoản tồn tại"
    assert a.text == b.text, f"thông báo khác nhau giữa user thật và user không tồn tại: {a.text[:100]!r} vs {b.text[:100]!r}"


SIGN_BODIES = {
    "chữ ký toàn số 0": {"message": "qa", "signature": "0x" + "00" * 65},
    "chữ ký rỗng": {"message": "qa", "signature": "0x"},
    "chữ ký không phải hex": {"message": "qa", "signature": "0xZZZZ"},
    "message tự chế, chữ ký rác": {"message": "I am admin", "signature": "0x" + "ab" * 65},
    "message rỗng": {"message": "", "signature": "0x" + "11" * 65},
    "SQLi trong message": {"message": "' OR 1=1 --", "signature": "0x" + "11" * 65},
    "kiểu mảng": {"message": ["a"], "signature": ["0x11"]},
    "NoSQL operator": {"message": {"$ne": None}, "signature": {"$ne": None}},
    "message 1MB": {"message": "A" * 1_000_000, "signature": "0x" + "11" * 65},
}


@pytest.mark.parametrize("name", list(SIGN_BODIES))
def test_sign_in_invalid_signature_rejected(name):
    r = requests.post(BASE + "/auth/sign", json=SIGN_BODIES[name], timeout=TIMEOUT)
    assert r.status_code < 500, f"{name} -> {r.status_code}: {r.text[:200]}"
    assert not r.ok, f"{name} được chấp nhận ({r.status_code}): {r.text[:200]}"
    assert not _has_token(r), f"{name}: response chứa token dù chữ ký sai"
    _leak_check(r, name)


@pytest.mark.parametrize("address", ["0x123", "'; DROP TABLE users;--", "0x" + "g" * 40, "0x" + "0" * 41, "../../etc/passwd", "A" * 5000])
def test_sign_challenge_invalid_address_rejected(address):
    r = _get("/auth/sign", params={"address": address})
    assert 400 <= r.status_code < 500, f"address '{address[:30]}' nhận {r.status_code} (mong 4xx): {r.text[:200]}"
    _leak_check(r, "address không hợp lệ")


def test_sign_challenge_unique_per_address():
    a = _get("/auth/sign", params={"address": "0x" + "0" * 39 + "1"})
    b = _get("/auth/sign", params={"address": "0x" + "0" * 39 + "2"})
    if not (a.ok and b.ok):
        pytest.skip(f"GET /auth/sign không trả 2xx ({a.status_code}/{b.status_code})")
    assert a.text != b.text, "challenge giống nhau cho hai địa chỉ khác nhau"


BODY_LIMITS = {
    "body 5MB": lambda: ("application/json", json.dumps({"username": "a", "password": "b" * 5_000_000})),
    "JSON lồng 5000 tầng": lambda: ("application/json", "[" * 5000 + "]" * 5000),
    "số cực lớn": lambda: ("application/json", '{"username":1' + "0" * 10_000 + ',"password":"x"}'),
}


@pytest.mark.parametrize("name", list(BODY_LIMITS))
def test_login_oversized_body_rejected(name):
    ctype, data = BODY_LIMITS[name]()
    r = requests.post(BASE + "/auth/admin/login", data=data, headers={"Content-Type": ctype}, timeout=TIMEOUT)
    assert 400 <= r.status_code < 500, f"{name} nhận {r.status_code} (mong 4xx, vd 400/413): {r.text[:200]}"


@pytest.mark.parametrize("ctype,data", [
    ("text/plain", '{"username":"a","password":"b"}'),
    ("application/xml", "<a><username>a</username></a>"),
    ("application/x-www-form-urlencoded", "username=a&password=b"),
    ("multipart/form-data; boundary=x", "--x\r\nContent-Disposition: form-data; name=\"username\"\r\n\r\na\r\n--x--"),
    ("application/json; charset=utf-7", '{"username":"+ADw-script+AD4-","password":"b"}'),
], ids=["text/plain", "xml", "urlencoded", "multipart", "json-utf7"])
def test_login_wrong_content_type_not_5xx(ctype, data):
    r = requests.post(BASE + "/auth/admin/login", data=data, headers={"Content-Type": ctype}, timeout=TIMEOUT)
    assert r.status_code < 500, f"Content-Type {ctype} -> {r.status_code}: {r.text[:200]}"
    assert not _has_token(r), "đăng nhập bằng body không chuẩn lại ra token"


# ------------------------------------------------------------------ 6. rate limit (opt-in)
@pytest.mark.skipif(os.environ.get("RUN_RATE_LIMIT") != "1",
                    reason="Bật bằng RUN_RATE_LIMIT=1 (gửi 40 lần đăng nhập sai liên tiếp bằng tài khoản không tồn tại)")
def test_admin_login_rate_limited():
    codes = []
    for i in range(40):
        r = requests.post(BASE + "/auth/admin/login", json={"username": f"qa-bruteforce-{i}", "password": "sai"}, timeout=TIMEOUT)
        codes.append(r.status_code)
        if r.status_code in (429, 423):
            return
    assert False, f"40 lần đăng nhập sai liên tiếp không bị giới hạn (mã nhận: {sorted(set(codes))}): thiếu rate limit chống brute-force"


# ------------------------------------------------------------------ 7. header, cookie, CORS, HTTPS
def _probe_responses():
    """Vài response tiêu biểu: route 404, một GET thường, một lỗi 4xx từ endpoint đăng nhập."""
    out = {"404": _get("/__khong_ton_tai__/abc")}
    for m, path, op, params in g.operations():
        if m == "get" and not g.is_protected(m, path, op):
            url, query = g.build(path, params)
            out["GET công khai " + path] = _get(url, params=query)
            break
    out["POST /auth/admin/login sai"] = requests.post(BASE + "/auth/admin/login", json={"username": "qa", "password": "qa"}, timeout=TIMEOUT)
    return out


@pytest.fixture(scope="module")
def probe_responses():
    return _probe_responses()


@pytest.mark.parametrize("header,check", [
    ("X-Content-Type-Options", lambda v: v.lower() == "nosniff"),
    ("Strict-Transport-Security", lambda v: re.search(r"max-age=(\d+)", v) and int(re.search(r"max-age=(\d+)", v).group(1)) >= 15552000),
    ("X-Frame-Options", lambda v: v.upper() in ("DENY", "SAMEORIGIN")),
], ids=lambda x: x if isinstance(x, str) else "")
def test_security_header_present(header, check, probe_responses):
    _skip_if_configured(f"header {header}")
    if header == "Strict-Transport-Security" and not BASE.startswith("https://"):
        pytest.skip("BASE_URL không dùng https")
    bad = []
    for name, r in probe_responses.items():
        v = r.headers.get(header)
        if not v or not check(v):
            bad.append(f"{name}: {v!r}")
    assert not bad, f"thiếu hoặc yếu header {header}: " + "; ".join(bad)


def test_no_version_disclosure_headers(probe_responses):
    bad = []
    for name, r in probe_responses.items():
        if r.headers.get("X-Powered-By"):
            bad.append(f"{name}: X-Powered-By={r.headers['X-Powered-By']}")
        srv = r.headers.get("Server", "")
        if re.search(r"/\d", srv):
            bad.append(f"{name}: Server={srv}")
        for h in ("X-AspNet-Version", "X-Runtime", "X-Version"):
            if r.headers.get(h):
                bad.append(f"{name}: {h}={r.headers[h]}")
    assert not bad, "lộ công nghệ/phiên bản: " + "; ".join(bad)


def test_cookies_have_secure_flags(probe_responses):
    bad = []
    for name, r in probe_responses.items():
        for c in r.raw.headers.getlist("Set-Cookie"):
            low = c.lower()
            missing = [f for f in ("httponly", "secure", "samesite") if f not in low]
            if missing:
                bad.append(f"{name}: {c.split('=')[0]} thiếu {', '.join(missing)}")
    assert not bad, "; ".join(bad)


def test_error_responses_do_not_leak(probe_responses):
    for name, r in probe_responses.items():
        _leak_check(r, name)


def test_responses_are_not_cached_if_private():
    if not AUTH_HEADER:
        pytest.skip("Cần AUTH_HEADER")
    probes = [p for p in PROBES if "/me" in p[1]] or PROBES[:1]
    if not probes:
        pytest.skip("Không có endpoint cần token")
    bad = []
    for key, url, query in probes:
        r = _get(url, {"Authorization": AUTH_HEADER}, params=query)
        cc = r.headers.get("Cache-Control", "").lower()
        if r.ok and not ("no-store" in cc or "private" in cc or "no-cache" in cc):
            bad.append(f"{key}: Cache-Control={cc!r}")
    assert not bad, "response riêng tư của user không có no-store/private: " + "; ".join(bad)


@pytest.mark.parametrize("origin", [EVIL_ORIGIN, "null", "https://" + (BASE.split("//")[-1]) + ".evil.example"],
                         ids=["evil", "null", "suffix-bypass"])
@pytest.mark.parametrize("url,query", _probe_params() or [pytest.param("/__khong_ton_tai__", {}, id="route")])
def test_cors_does_not_trust_arbitrary_origin(url, query, origin):
    r = _get(url, {"Origin": origin}, params=query)
    pre = requests.options(BASE + url, params=query, timeout=TIMEOUT, allow_redirects=False,
                           headers={"Origin": origin, "Access-Control-Request-Method": "GET",
                                    "Access-Control-Request-Headers": "authorization"})
    for label, resp in (("response", r), ("preflight", pre)):
        acao = resp.headers.get("Access-Control-Allow-Origin")
        acac = resp.headers.get("Access-Control-Allow-Credentials", "").lower() == "true"
        assert acao != origin, f"{label}: server phản chiếu Origin tuỳ ý '{origin}' (ACAO={acao}, credentials={acac})"
        assert not (acao == "*" and acac), f"{label}: ACAO=* kèm Allow-Credentials=true"
        assert acao != "null", f"{label}: server cho phép Origin 'null'"


@pytest.mark.skipif(not BASE.startswith("https://"), reason="BASE_URL không dùng https")
def test_http_redirects_to_https():
    http_url = "http://" + BASE[len("https://"):] + "/auth/me"
    try:
        r = requests.get(http_url, timeout=TIMEOUT, allow_redirects=False)
    except requests.exceptions.RequestException:
        return  # cổng 80 đóng: chấp nhận được
    ok = r.status_code in (301, 302, 307, 308) and r.headers.get("Location", "").startswith("https://")
    assert ok or r.status_code in (400, 403, 404, 421, 426), (
        f"HTTP thường trả {r.status_code} thay vì chuyển sang HTTPS: token có thể bị gửi dạng rõ")


# ------------------------------------------------------------------ 8. phương thức HTTP không khai báo
def _undeclared_method_cases():
    out = []
    for path, item in g.SPEC.get("paths", {}).items():
        item = g._resolve(item)
        declared = {m for m in g.METHODS if m in item}
        missing = [m for m in ("put", "patch", "delete") if m not in declared]
        if missing:
            params = [g._resolve(p) for p in item.get("parameters", [])]
            for m in declared:
                params += [g._resolve(p) for p in item[m].get("parameters", [])]
            out.append(pytest.param(path, params, missing, id=f"{'/'.join(m.upper() for m in missing)} {path}"))
    return out


@pytest.mark.parametrize("path,params,methods", _undeclared_method_cases())
def test_undeclared_method_not_executed_without_token(path, params, methods):
    """Không token + phương thức spec không khai báo: chỉ được 401/403/404/405 (DELETE giả không được xoá được gì)."""
    url, query = g.build(path, params)
    bad = []
    for m in methods:
        r = requests.request(m.upper(), BASE + url, params=query, timeout=TIMEOUT, allow_redirects=False)
        if r.status_code not in AUTH_STATUSES | {404, 405, 400}:
            bad.append(f"{m.upper()} -> {r.status_code}")
    assert not bad, "phương thức không khai báo được xử lý khi chưa đăng nhập: " + ", ".join(bad)


@pytest.mark.parametrize("method", ["TRACE", "TRACK", "CONNECT", "PROPFIND", "FOO"])
def test_dangerous_http_methods_disabled(method):
    try:
        r = requests.request(method, BASE + "/auth/me", timeout=TIMEOUT, allow_redirects=False,
                             headers={"X-Probe": "qa-trace-marker"})
    except requests.exceptions.RequestException:
        return  # kết nối bị từ chối: tốt
    assert r.status_code not in range(200, 300), f"{method} được chấp nhận ({r.status_code})"
    assert r.status_code < 500, f"{method} -> {r.status_code}"
    assert "qa-trace-marker" not in r.text, f"{method} phản chiếu lại header của request (XST)"


@pytest.mark.parametrize("override", ["X-HTTP-Method-Override", "X-HTTP-Method", "X-Method-Override"])
@pytest.mark.parametrize("path,params", [(p, pr) for m, p, o, pr in g.operations()
                                           if m == "get" and g.is_protected(m, p, o)][:3])
def test_method_override_header_ignored_without_token(path, params, override):
    url, query = g.build(path, params)
    r = requests.post(BASE + url, params=query, headers={override: "DELETE"}, timeout=TIMEOUT, allow_redirects=False)
    assert r.status_code in AUTH_STATUSES | {400, 404, 405}, f"POST + {override}: DELETE -> {r.status_code}"


# ------------------------------------------------------------------ 9. file nhạy cảm / trang debug
SENSITIVE_PATHS = [
    "/.env", "/.env.local", "/.env.production", "/.git/config", "/.git/HEAD", "/.svn/entries", "/.DS_Store",
    "/config.json", "/package.json", "/package-lock.json", "/docker-compose.yml", "/Dockerfile", "/wrangler.toml",
    "/backup.sql", "/dump.sql", "/db.sqlite", "/server-status", "/phpinfo.php", "/info.php",
    "/actuator", "/actuator/env", "/actuator/heapdump", "/metrics", "/debug", "/console", "/admin/console",
    "/.well-known/security.txt.bak", "/id_rsa", "/.ssh/id_rsa", "/.aws/credentials", "/WEB-INF/web.xml",
]


@pytest.mark.parametrize("path", SENSITIVE_PATHS)
def test_sensitive_path_not_exposed(path):
    r = _get(path)
    ctype = r.headers.get("content-type", "").lower()
    # 200 dạng HTML thường là trang SPA/catch-all; 200 dạng khác (json/text/octet) mới là rò file
    exposed = r.status_code == 200 and "text/html" not in ctype and len(r.content) > 0
    assert not exposed, f"{path} trả 200 ({ctype}, {len(r.content)} byte): {r.text[:100]!r}"
    if r.status_code == 200:
        assert not re.search(r"(DB_PASSWORD|PRIVATE_KEY|SECRET|\[core\]|ref: refs/heads)", r.text), f"{path} chứa nội dung nhạy cảm"


# ------------------------------------------------------------------ 10. dữ liệu nhạy cảm trong response
@pytest.mark.parametrize("path,params", [pytest.param(p, pr, id=f"GET {p}") for m, p, o, pr in g.operations()
                                           if m == "get" and f"GET {p}" not in g.SKIP])
def test_get_response_has_no_secret_fields(path, params):
    hdr = {"Authorization": AUTH_HEADER} if AUTH_HEADER else {}
    url, query = g.build(path, params)
    r = _get(url, hdr, params=query)
    m = SECRET_KEY_RE.search(r.text or "")
    assert not m, f"response chứa trường bí mật: {m.group(0)[:60]}"
    _leak_check(r, f"GET {path}")


def test_malformed_json_error_does_not_leak():
    """Body sai kiểu ở endpoint công khai: thông báo lỗi chỉ nên mô tả trường sai, không kèm stack trace."""
    r = requests.post(BASE + "/auth/sign", data="{bad json", headers={"Content-Type": "application/json"}, timeout=TIMEOUT)
    assert r.status_code < 500, f"JSON hỏng -> {r.status_code}: {r.text[:200]}"
    _leak_check(r, "JSON hỏng ở /auth/sign")
