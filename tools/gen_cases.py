#!/usr/bin/env python3
"""
gen_cases.py - Sinh test case (CSV) từ swagger.json / openapi.json cho BẤT KỲ dự án nào.

Hỗ trợ Swagger 2.0 và OpenAPI 3.0 / 3.1 (file JSON, hoặc YAML nếu đã cài pyyaml).
Chỉ dùng thư viện chuẩn của Python. Có thể cài thêm `pip install exrex` để sinh giá trị
hợp lệ cho regex phức tạp (không cài thì vẫn chạy, dùng danh sách giá trị thử).

CÁCH DÙNG
  python gen_cases.py SPEC --list                             liệt kê endpoint và phân loại nguy hiểm
  python gen_cases.py SPEC -o test_cases.csv                  sinh case cho mọi endpoint ghi (POST/PUT/PATCH/DELETE)
  python gen_cases.py SPEC -o out.csv --config project.json   dùng cấu hình riêng của dự án
  python gen_cases.py SPEC -o out.csv --risky-only            chỉ các endpoint nguy hiểm
  python gen_cases.py SPEC -o out.csv --methods get,post,put,patch,delete
SPEC là đường dẫn file hoặc URL.

FILE CẤU HÌNH (JSON, mọi khoá đều không bắt buộc)
  {
    "risky": [                         endpoint nguy hiểm (path dùng * làm ký tự đại diện, vd /pool/*)
      {"method": "POST", "path": "/firebase/sendAll", "note": "Gửi thông báo thật..."},
      {"method": "POST", "path": "/auth/login", "risky": false}     <- khai báo "không nguy hiểm"
    ],
    "extra": [                         case nghiệp vụ viết tay
      {"method": "POST", "path": "/x", "category": "edge", "description": "...", "path_params": {},
       "query": {}, "body": {...}, "expected": "400", "risk": "LOW", "note": "", "auth": "token hợp lệ"}
    ],
    "field_samples": {"chainId": 1, "email": "qa@example.com"},     giá trị hợp lệ theo tên field/param
    "id_params": ["id", "uuid", "orderId"],                         param là ID tài nguyên
    "auto_risky": true,                                             tự phát hiện endpoint nguy hiểm theo từ khoá
    "risky_keywords": ["delete", "send", "..."],
    "admin_keywords": ["admin"]
  }
  Trong "body" của extra có thể dùng {"$repeat": [giá_trị, số_lần]} để tạo mảng lặp.

CỘT risk TRONG CSV
  SAFE         không thể đổi dữ liệu (ID không tồn tại, hoặc bị chặn trước khi xử lý)
  LOW          kỳ vọng bị từ chối (400/401/403); chỉ đổi dữ liệu nếu server thiếu validate / phân quyền
  DESTRUCTIVE  nếu chạy đúng sẽ đổi dữ liệu thật: chỉ chạy trên môi trường test, dữ liệu test
  NORMAL       endpoint không nằm trong danh sách nguy hiểm
Giá trị {{REAL_ID}} / {{OTHER_USER_ID}} trong cột path_params là chỗ tester điền dữ liệu test.
"""
import argparse
import copy
import csv
import fnmatch
import json
import os
import re
import sys
import urllib.request
from collections import Counter

try:
    import exrex  # tuỳ chọn
except ImportError:
    exrex = None

ADDR = "0x0000000000000000000000000000000000000001"
ZERO_OID = "0" * 24
ZERO_UUID = "00000000-0000-0000-0000-000000000000"
P_ADDR, P_NUM, P_OID = r"^0x[a-fA-F0-9]{40}$", r"^[0-9]+$", r"^[0-9a-fA-F]{24}$"
MUTATING = ("post", "put", "patch", "delete")
ALL_METHODS = ("get", "post", "put", "patch", "delete")

FORMAT_SAMPLES = {
    "email": "qa.test@example.com", "uuid": "123e4567-e89b-12d3-a456-426614174000",
    "uri": "https://example.com", "url": "https://example.com", "date": "2026-01-01",
    "date-time": "2026-01-01T00:00:00Z", "ipv4": "127.0.0.1", "hostname": "example.com",
}
FORMAT_INVALID = {
    "email": "not-an-email", "uuid": "not-a-uuid", "uri": "not a uri", "url": "not a url",
    "date": "not-a-date", "date-time": "not-a-date", "ipv4": "999.1.1.1",
}
CANDIDATES = ["test", "1", "a", "A", "abc123", "Test1234", "0", "00000", "test@example.com",
              "https://example.com", "2026-01-01", "+84901234567", "ABC-1234", ADDR]

DEFAULT_CONFIG = {
    "risky": [], "extra": [], "field_samples": {}, "id_params": ["id", "uuid", "_id"],
    "auto_risky": True, "admin_keywords": ["admin"],
    "risky_keywords": ["delete", "remove", "destroy", "purge", "drop", "clear", "reset", "truncate", "wipe",
                       "flush", "send", "broadcast", "publish", "transfer", "withdraw", "pay", "payment",
                       "refund", "charge", "cancel", "revoke", "disable", "ban"],
}

WARNINGS = []


def warn(msg):
    if msg not in WARNINGS:
        WARNINGS.append(msg)


# ------------------------------------------------------------------ đọc spec / config
_NUM_KEYS = ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
             "minLength", "maxLength", "minItems", "maxItems")


def _drop_non_numeric_bounds(node):
    """Joi/hapi có thể xuất giới hạn là tham chiếu (vd maximum: {"ref": {...}}) thay vì số: bỏ qua các giá trị đó."""
    if isinstance(node, dict):
        for k in _NUM_KEYS:
            if k in node and (isinstance(node[k], bool) or not isinstance(node[k], (int, float))):
                del node[k]
        for v in node.values():
            _drop_non_numeric_bounds(v)
    elif isinstance(node, list):
        for v in node:
            _drop_non_numeric_bounds(v)
    return node


def load(src):
    if re.match(r"https?://", src):
        with urllib.request.urlopen(src, timeout=20) as r:
            text = r.read().decode("utf-8")
    else:
        with open(src, encoding="utf-8") as f:
            text = f.read()
    try:
        return _drop_non_numeric_bounds(json.loads(text))
    except json.JSONDecodeError:
        try:
            import yaml
        except ImportError:
            sys.exit("Spec không phải JSON hợp lệ. Nếu là YAML, cài: pip install pyyaml")
        return _drop_non_numeric_bounds(yaml.safe_load(text))


def load_config(path):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if path:
        with open(path, encoding="utf-8") as f:
            user = json.load(f)
        for k in user:
            if k not in DEFAULT_CONFIG and k != "tests":   # "tests": cấu hình cho tests/test_generic.py
                warn(f"Khoá không hợp lệ trong config: '{k}' (bỏ qua). Khoá hợp lệ: {', '.join(DEFAULT_CONFIG)}")
        cfg.update({k: v for k, v in user.items() if k in DEFAULT_CONFIG})
    cfg["id_params"] = set(cfg["id_params"])
    return cfg


def expand(v):
    if isinstance(v, dict):
        if set(v) == {"$repeat"}:
            val, n = v["$repeat"]
            return [expand(val) for _ in range(n)]
        return {k: expand(x) for k, x in v.items()}
    if isinstance(v, list):
        return [expand(x) for x in v]
    return v


# ------------------------------------------------------------------ schema helpers
def resolve(spec, s):
    for _ in range(30):
        if isinstance(s, dict) and isinstance(s.get("$ref"), str):
            ref = s["$ref"]
            if not ref.startswith("#/"):
                warn(f"$ref ngoài file không được hỗ trợ: {ref}")
                return {}
            node = spec
            try:
                for part in ref[2:].split("/"):
                    node = node[part.replace("~1", "/").replace("~0", "~")]
            except (KeyError, TypeError):
                warn(f"Không tìm thấy $ref: {ref}")
                return {}
            s = node
        else:
            break
    return normalize(spec, s) if isinstance(s, dict) else {}


def normalize(spec, s):
    """Gộp allOf, chọn nhánh đầu của oneOf/anyOf, chuẩn hoá type dạng danh sách (OpenAPI 3.1)."""
    if "allOf" in s:
        merged = {k: v for k, v in s.items() if k != "allOf"}
        props, req = dict(merged.get("properties", {})), list(required_of(merged))
        for part in s["allOf"]:
            ps = resolve(spec, part)
            for k, v in ps.items():
                if k == "properties":
                    props.update(v)
                elif k == "required":
                    req += [r for r in required_of(ps) if r not in req]
                elif k not in merged:
                    merged[k] = v
        if props:
            merged["properties"] = props
        if req:
            merged["required"] = req
        s = merged
    for key in ("oneOf", "anyOf"):
        if s.get(key):
            base = {k: v for k, v in s.items() if k not in ("oneOf", "anyOf")}
            s = {**base, **resolve(spec, s[key][0])}
            break
    t = s.get("type")
    if isinstance(t, list):
        non_null = [x for x in t if x != "null"]
        s = {**s, "type": non_null[0] if non_null else "string"}
        if "null" in t:
            s["nullable"] = True
    return s


def required_of(s):
    r = s.get("required")
    return r if isinstance(r, list) else []


def param_schema(spec, p):
    return resolve(spec, p["schema"]) if "schema" in p else resolve(spec, p)


def matches(pat, v):
    try:
        return re.search(pat, v) is not None
    except re.error:
        return True  # không kiểm tra được thì coi như khớp


def from_pattern(pat):
    if exrex:
        for _ in range(5):
            try:
                v = exrex.getone(pat)
            except Exception:
                break
            if matches(pat, v):
                return v
    for c in CANDIDATES:
        if matches(pat, c):
            return c
    return None


def nonmatching(pat):
    for c in ("not-valid!", "", " ", "!!!", "###", "xyz"):
        try:
            if re.search(pat, c) is None:
                return c
        except re.error:
            return None
    return None


def fit_length(v, s):
    mn, mx = s.get("minLength"), s.get("maxLength")
    if mn and len(v) < mn:
        v = (v * (mn // len(v) + 1))[:mn]
    if mx is not None and len(v) > mx:
        v = v[:mx]
    return v


def str_sample(s):
    pat, fmt = s.get("pattern"), s.get("format")
    if pat == P_ADDR:
        return ADDR
    if pat == P_OID:
        return ZERO_OID
    if pat == P_NUM:
        return "1"
    if fmt in FORMAT_SAMPLES:
        return FORMAT_SAMPLES[fmt]
    if pat:
        v = from_pattern(pat)
        if v is not None:
            return v
        warn(f"Không sinh được giá trị hợp lệ cho pattern {pat!r}; đang dùng 'test'. "
             f"Hãy khai báo giá trị trong 'field_samples' của config.")
    return fit_length("test", s)


def num_sample(s, t):
    lo, hi = s.get("minimum"), s.get("maximum")
    v = lo if lo is not None else (1 if hi is None or hi >= 1 else hi)
    ex = s.get("exclusiveMinimum")
    if ex is True and lo is not None:
        v = lo + 1
    elif isinstance(ex, (int, float)) and not isinstance(ex, bool):
        v = max(v, ex + 1)
    return int(v) if t == "integer" else v


def sample(spec, schema, cfg=None, name=None, full=True, depth=0):
    s = resolve(spec, schema)
    fs = cfg["field_samples"] if cfg else {}
    if name is not None and name in fs:
        return fs[name]
    if s.get("example") is not None:
        return s["example"]
    if s.get("enum"):
        return s["enum"][0]
    t = s.get("type")
    d = s.get("default")
    if d is not None and not isinstance(d, (dict, list)):
        return d
    if t == "object" or "properties" in s:
        if depth > 4:
            return {}
        req, out = required_of(s), {}
        for k, v in s.get("properties", {}).items():
            rv = resolve(spec, v)
            if rv.get("readOnly"):
                continue
            if full or k in req:
                out[k] = sample(spec, rv, cfg, k, full, depth + 1)
        return out
    if t == "array":
        if "items" not in s or depth > 4:
            return []
        n = min(max(1, s.get("minItems", 1)), 3)
        return [sample(spec, s["items"], cfg, name, full, depth + 1) for _ in range(n)]
    if t == "string":
        return str_sample(s)
    if t in ("number", "integer"):
        return num_sample(s, t)
    if t == "boolean":
        return True
    return "test"


def fields(spec, schema, path=(), depth=0):
    """Duyệt các property (tối đa 2 cấp object lồng nhau): (path, schema đã resolve, bắt buộc?)."""
    s = resolve(spec, schema)
    req = required_of(s)
    for k, v in s.get("properties", {}).items():
        rv = resolve(spec, v)
        if rv.get("readOnly"):
            continue
        p = path + (k,)
        yield p, rv, k in req
        if depth < 1 and rv.get("properties"):
            yield from fields(spec, rv, p, depth + 1)


def set_path(obj, path, value):
    o = copy.deepcopy(obj)
    cur = o
    for k in path[:-1]:
        cur = cur[k]
    cur[path[-1]] = value
    return o


def del_path(obj, path):
    o = copy.deepcopy(obj)
    cur = o
    for k in path[:-1]:
        cur = cur[k]
    cur.pop(path[-1], None)
    return o


def wrong_type(s):
    return {"string": 12345, "number": "abc", "integer": "abc", "boolean": "yes",
            "array": "text", "object": "text"}.get(s.get("type"), 12345)


def invalid_values(spec, s):
    """Danh sách (mô tả, giá trị, expected, category) vi phạm ràng buộc của schema."""
    s = resolve(spec, s)
    out, t, pat, fmt = [], s.get("type"), s.get("pattern"), s.get("format")

    def add(desc, val, exp="400", cat="validation"):
        out.append((desc, val, exp, cat))

    if "enum" in s:
        add("giá trị ngoài enum", "INVALID_ENUM")
    if pat == P_ADDR:
        add("địa chỉ quá ngắn", "0x123")
        add("địa chỉ có ký tự không phải hex", "0x" + "g" * 40)
    elif pat == P_OID:
        add("ObjectId sai format", "not-an-objectid")
    elif pat == P_NUM:
        add("không phải số", "abc")
    elif pat:
        bad = nonmatching(pat)
        if bad is not None:
            add(f"sai pattern {pat}", bad)
    if fmt in FORMAT_INVALID:
        add(f"sai format {fmt}", FORMAT_INVALID[fmt])
    if t == "string" and "enum" not in s and not pat and not fmt:
        add("chuỗi rỗng", "", "4xx nếu field không cho phép rỗng; không được 5xx", "edge")
        if s.get("minLength", 0) > 1:
            add("ngắn hơn minLength", "x" * (s["minLength"] - 1))
        if "maxLength" in s:
            add("dài hơn maxLength", "x" * (s["maxLength"] + 1))
        else:
            add("chuỗi rất dài (2.000 ký tự)", "a" * 2000, "4xx (400/413), không được 5xx", "edge")
        add("ký tự đặc biệt / NoSQL injection", "'\"><script>alert(1)</script>{\"$ne\":null}",
            "không được 5xx; dữ liệu không bị thực thi", "security")
    if t in ("number", "integer"):
        if "minimum" in s:
            add("nhỏ hơn minimum", s["minimum"] - 1)
        else:
            add("số âm", -1, "400 nếu có ràng buộc (spec không giới hạn)", "edge")
        if "maximum" in s:
            add("lớn hơn maximum", s["maximum"] + 1)
        if t == "integer":
            add("số thập phân thay vì số nguyên", 1.5)
    if t == "array":
        if s.get("minItems", 0) > 0:
            add(f"mảng ít hơn minItems ({s['minItems']})", [])
        if "items" in s:
            if 0 < s.get("maxItems", 0) <= 100:
                add(f"mảng nhiều hơn maxItems ({s['maxItems']})",
                    [sample(spec, s["items"])] * (s["maxItems"] + 1))
            for d, v, e, c in invalid_values(spec, s["items"])[:2]:
                add(f"phần tử mảng: {d}", [v], e, c)
    return out


def body_cases(spec, schema, valid, body_required):
    """(category, mô tả, body, expected, note) cho body không hợp lệ."""
    s = resolve(spec, schema)
    has_req = bool(required_of(s))
    if has_req:
        yield "validation", "Body rỗng {}", {}, "400", ""
    yield ("validation", "Không gửi body", None, "400" if (body_required or has_req) else "2xx hoặc 400",
           "Nếu nhận 2xx: body không bắt buộc và hành động đã chạy.")
    if s.get("type") == "object" or "properties" in s:
        yield "validation", "Body sai kiểu (mảng thay vì object)", [], "400", ""
    for path, ps, req in fields(spec, s):
        name = ".".join(path)
        if req:
            yield "validation", f"Thiếu field bắt buộc: {name}", del_path(valid, path), "400", ""
        yield "validation", f"{name}: sai kiểu dữ liệu", set_path(valid, path, wrong_type(ps)), "400", ""
        if not ps.get("nullable"):
            yield "validation", f"{name}: null", set_path(valid, path, None), "400", ""
        for desc, val, exp, cat in invalid_values(spec, ps):
            yield cat, f"{name}: {desc}", set_path(valid, path, val), exp, ""
    if isinstance(valid, dict):
        yield ("validation", "Field lạ ngoài spec", {**valid, "unexpectedField": 1}, "400",
               "Nếu nhận 2xx: server cho field lạ và hành động đã được chạy.")


def bad_body(spec, schema, valid):
    """(body chắc chắn không hợp lệ, có chắc không?) - dùng cho case auth để không vô tình thực thi."""
    s = resolve(spec, schema)
    if required_of(s):
        return {}, True
    for path, ps, _ in fields(spec, s):
        return set_path(valid, path, wrong_type(ps)), True
    return {}, False


# ------------------------------------------------------------------ đọc operation
def iter_ops(spec, methods):
    for path, item in spec.get("paths", {}).items():
        item = resolve(spec, item)
        common = item.get("parameters", [])
        for m in methods:
            op = item.get(m)
            if isinstance(op, dict):
                yield path, m, op, common


def op_params(spec, op, common):
    merged = {}
    for p in list(common) + list(op.get("parameters", [])):
        p = resolve(spec, p)
        merged[(p.get("in"), p.get("name"))] = p
    return list(merged.values())


def get_body(spec, op, params):
    """(schema, bắt buộc?, content-type). Hỗ trợ Swagger 2.0 (in: body) và OpenAPI 3 (requestBody)."""
    for p in params:
        if p.get("in") == "body":
            return p.get("schema", {}), bool(p.get("required")), "application/json"
    if op.get("requestBody"):
        rb = resolve(spec, op["requestBody"])
        content = rb.get("content", {})
        cts = [c for c in content if "json" in c] or list(content)
        if cts:
            return content[cts[0]].get("schema", {}), bool(rb.get("required")), cts[0]
    return None, False, None


def op_security(spec, op):
    return op["security"] if "security" in op else spec.get("security")


def security_summary(spec):
    schemes = spec.get("securityDefinitions") or spec.get("components", {}).get("securitySchemes") or {}
    out = []
    for name, d in schemes.items():
        d = resolve(spec, d)
        t = d.get("type", "?")
        if t == "apiKey":
            t += f" ({d.get('in')}: {d.get('name')})"
        elif t == "http":
            t += f" ({d.get('scheme')})"
        out.append(f"{name}: {t}")
    return out


def words(text):
    return {w.lower() for w in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])", text or "")}


def classify(cfg, method, path, op):
    """-> (loại, ghi chú). loại: 'explicit' | 'auto' | 'normal' (khai báo không nguy hiểm) | None."""
    for r in cfg["risky"]:
        mm = r.get("method", "*").upper()
        if mm in ("*", method.upper()) and fnmatch.fnmatchcase(path, r.get("path", "")):
            r["_used"] = True
            if r.get("risky") is False:
                return "normal", None
            return "explicit", r.get("note") or "Endpoint nguy hiểm (khai báo trong config)."
    if cfg["auto_risky"]:
        reasons = []
        if method == "delete":
            reasons.append("method DELETE")
        kws = set(cfg["risky_keywords"]) & (words(path) | words(op.get("operationId")) | words(op.get("summary")))
        if kws:
            reasons.append("từ khoá: " + ", ".join(sorted(kws)))
        if reasons:
            return "auto", f"Tự phát hiện nguy hiểm ({'; '.join(reasons)}). Hãy xác nhận và khai báo trong config."
    return None, None


def nonexistent_id(s):
    """(giá trị ID chắc chắn không tồn tại, có đáng tin không)."""
    if s.get("pattern") == P_OID:
        return ZERO_OID, True
    if s.get("format") == "uuid":
        return ZERO_UUID, True
    if s.get("type") in ("integer", "number"):
        return min(999999999, s.get("maximum", 999999999)), True
    pat = s.get("pattern")
    v = "nonexistent-0000"
    if pat and not matches(pat, v):
        return from_pattern(pat) or v, False
    return v, True


# ------------------------------------------------------------------ sinh case
COLS = ["id", "method", "path", "category", "description", "auth", "path_params", "query", "body",
        "content_type", "expected_status", "risk", "note", "source"]


class Out:
    def __init__(self):
        self.rows = []

    def add(self, method, path, cat, desc, auth, params, query, body, ctype, expected, risk, note="", source="spec"):
        j = lambda v: json.dumps(v, ensure_ascii=False) if v else ""
        self.rows.append({
            "id": f"TC{len(self.rows) + 1:03d}", "method": method.upper(), "path": path, "category": cat,
            "description": desc, "auth": auth, "path_params": j(params), "query": j(query),
            "body": "" if body is None else json.dumps(body, ensure_ascii=False),
            "content_type": ctype or "", "expected_status": expected, "risk": risk, "note": note, "source": source,
        })


def gen_for_op(spec, cfg, method, path, op, common, note, out):
    params = op_params(spec, op, common)
    path_ps = [p for p in params if p.get("in") == "path"]
    query_ps = [p for p in params if p.get("in") == "query"]
    schema, body_required, ctype = get_body(spec, op, params)
    risky = note is not None
    M = method.upper()

    # giá trị hợp lệ cho path param; với endpoint nguy hiểm, ID tài nguyên dùng ID KHÔNG tồn tại
    ok_path, noeffect, id_names = {}, False, []
    for p in path_ps:
        ps = param_schema(spec, p)
        is_id = p["name"] in cfg["id_params"] or ps.get("pattern") == P_OID or ps.get("format") == "uuid"
        if risky and is_id:
            val, reliable = nonexistent_id(ps)
            if reliable:
                ok_path[p["name"]] = val
                noeffect = True
                id_names.append(p["name"])
                continue
        ok_path[p["name"]] = sample(spec, ps, cfg, p["name"])
    ok_query = {p["name"]: sample(spec, param_schema(spec, p), cfg, p["name"])
                for p in query_ps if p.get("required")}

    neg = "NORMAL" if not risky else ("SAFE" if noeffect else "LOW")
    pos_risk = "NORMAL" if not risky else ("SAFE" if noeffect else "DESTRUCTIVE")
    pos_expected = "404" if noeffect else "2xx"
    if noeffect:
        pos_note = "ID không tồn tại nên không ghi dữ liệu."
    else:
        pos_note = (note or "") + (" Thay bằng ID có thật." if path_ps and not risky else "")
    if schema:
        pos_note += " Giá trị body là giá trị mẫu sinh từ spec, thay bằng dữ liệu thật."
    pos_note = pos_note.strip()

    valid_min = sample(spec, schema, cfg, None, full=False) if schema else None
    valid_full = sample(spec, schema, cfg, None, full=True) if schema else None

    def row(cat, desc, auth, params_, query, body, expected, risk, n=""):
        out.add(M, path, cat, desc, auth, params_, query, body, ctype if body is not None else "",
                expected, risk, n)

    # 1. dữ liệu đúng
    row("happy path", "Dữ liệu hợp lệ (chỉ field bắt buộc)" if schema else "Gọi hợp lệ", "token hợp lệ",
        ok_path, ok_query, valid_min, pos_expected, pos_risk, pos_note)
    if schema and valid_full != valid_min:
        row("happy path", "Dữ liệu hợp lệ (đủ mọi field)", "token hợp lệ", ok_path, ok_query, valid_full,
            pos_expected, pos_risk, pos_note)
    if risky and noeffect:
        real = {k: ("{{REAL_ID}}" if k in id_names else v) for k, v in ok_path.items()}
        row("happy path", "Với bản ghi test có thật", "token hợp lệ", real, ok_query, valid_min, "2xx",
            "DESTRUCTIVE", note)

    # 2. body không hợp lệ
    if schema:
        for cat, desc, body, exp, n in body_cases(spec, schema, valid_full, body_required):
            row(cat, desc, "token hợp lệ", ok_path, ok_query, body, exp, neg, n)

    # 3. path / query param không hợp lệ
    for p in path_ps + query_ps:
        ps = param_schema(spec, p)
        bad = [("sai kiểu dữ liệu", wrong_type(ps), "400", "validation")] + invalid_values(spec, ps)
        for desc, val, exp, cat in bad:
            if p["in"] == "path":
                row(cat, f"Path param {p['name']}: {desc}", "token hợp lệ", {**ok_path, p["name"]: val},
                    ok_query, valid_min, exp, neg)
            else:
                row(cat, f"Query param {p['name']}: {desc}", "token hợp lệ", ok_path,
                    {**ok_query, p["name"]: val}, valid_min, exp, neg)
        if p["in"] == "query" and p.get("required"):
            row("validation", f"Thiếu query param bắt buộc: {p['name']}", "token hợp lệ", ok_path,
                {k: v for k, v in ok_query.items() if k != p["name"]}, valid_min, "400", neg)

    # 4. xác thực / phân quyền: dùng input chắc chắn sai để không vô tình chạy hành động
    sec = op_security(spec, op)
    if sec != []:  # [] = endpoint công khai
        if schema:
            a_body, a_safe = bad_body(spec, schema, valid_full)
            a_query = ok_query
        elif any(p.get("required") for p in query_ps):
            a_body, a_query, a_safe = None, {}, True  # thiếu query bắt buộc -> bị từ chối
        else:
            a_body, a_query, a_safe = None, ok_query, noeffect
        a_risk = "NORMAL" if not risky else ("SAFE" if noeffect else ("LOW" if a_safe else "DESTRUCTIVE"))
        a_note = "" if a_safe or not risky else (
            "Không thể gửi request vô hại: nếu thiếu auth thì hành động sẽ chạy. Chỉ chạy trên môi trường test."
            + (f" Nếu {', '.join(p['name'] for p in path_ps)} là ID tài nguyên, thêm vào 'id_params' trong config "
               f"để script dùng ID không tồn tại." if path_ps else ""))
        if sec is None:
            a_note = ("Spec không khai báo security: xác nhận endpoint có yêu cầu đăng nhập không. " + a_note).strip()
        exp = "401 hoặc 403"
        row("auth", "Không gửi token", "không token", ok_path, a_query, a_body, exp, a_risk,
            (a_note + " Nếu nhận 400/2xx: endpoint chưa chặn truy cập không token.").strip())
        row("auth", "Token không hợp lệ", "token rác (Bearer not.a.jwt)", ok_path, a_query, a_body, exp, a_risk, a_note)
        admin_hit = set(cfg["admin_keywords"]) & (words(path) | words(op.get("summary")))
        if risky or admin_hit:
            row("auth", "Token của user thường (không phải admin)", "token user thường", ok_path, a_query, a_body,
                "403 nếu endpoint chỉ dành cho admin", a_risk,
                (a_note + " Nếu nhận 400: phân quyền chưa được kiểm tra trước khi validate.").strip())


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("spec", help="đường dẫn hoặc URL của swagger.json / openapi.json")
    ap.add_argument("-o", "--output", default="test_cases.csv")
    ap.add_argument("-c", "--config", help="file cấu hình JSON của dự án")
    ap.add_argument("--methods", default="post,put,patch,delete", help="các method cần sinh case (mặc định: method ghi)")
    ap.add_argument("--risky-only", action="store_true", help="chỉ sinh cho endpoint nguy hiểm")
    ap.add_argument("--list", action="store_true", help="chỉ liệt kê endpoint và phân loại, không sinh CSV")
    a = ap.parse_args()

    spec = load(a.spec)
    cfg = load_config(a.config)
    methods = [m.strip().lower() for m in a.methods.split(",") if m.strip()]
    bad = [m for m in methods if m not in ALL_METHODS]
    if bad:
        sys.exit(f"Method không hợp lệ: {bad}")

    ver = spec.get("openapi") or ("Swagger " + str(spec.get("swagger", "?")))
    title = spec.get("info", {}).get("title", "?")
    print(f"Spec: {title} ({ver})")
    sec = security_summary(spec)
    print("Xác thực khai báo:", "; ".join(sec) if sec else "không có")

    ops = [(path, m, op, common) + classify(cfg, m, path, op) for path, m, op, common in iter_ops(spec, methods)]
    unclassified = [f"{m.upper()} {p}" for p, m, _, _, kind, _ in ops if m in MUTATING and kind is None]

    if a.list:
        print(f"\n{'METHOD':7} {'PATH':50} PHÂN LOẠI")
        for p, m, op, _, kind, note in ops:
            label = {"explicit": "NGUY HIỂM (config)", "auto": "NGUY HIỂM (tự phát hiện)",
                     "normal": "bình thường (config)", None: "chưa phân loại"}[kind]
            print(f"{m.upper():7} {p:50} {label}")
    else:
        out = Out()
        for path, m, op, common, kind, note in ops:
            if a.risky_only and note is None:
                continue
            gen_for_op(spec, cfg, m, path, op, common, note, out)
        for e in cfg["extra"]:
            e_path, e_m = e.get("path"), str(e.get("method", "")).lower()
            if e_m not in spec.get("paths", {}).get(e_path, {}):
                warn(f"Case 'extra' bị bỏ qua vì spec không có endpoint {e_m.upper()} {e_path}: {e.get('description')}")
                continue
            body = expand(e["body"]) if "body" in e else None
            out.add(e_m, e_path, e.get("category", "edge"), e.get("description", ""), e.get("auth", "token hợp lệ"),
                    e.get("path_params"), e.get("query"), body, "application/json" if body is not None else "",
                    e.get("expected", ""), e.get("risk", "LOW"), e.get("note", ""), "config")
        os.makedirs(os.path.dirname(os.path.abspath(a.output)), exist_ok=True)
        with open(a.output, "w", newline="", encoding="utf-8-sig") as f:  # utf-8-sig: Excel đọc đúng tiếng Việt
            w = csv.DictWriter(f, fieldnames=COLS)
            w.writeheader()
            w.writerows(out.rows)
        print(f"\n{len(out.rows)} test case -> {a.output}")
        print("Theo mức rủi ro:", dict(Counter(r["risk"] for r in out.rows)))

    if unclassified:
        shown = ", ".join(unclassified[:12]) + (f" ... (+{len(unclassified) - 12})" if len(unclassified) > 12 else "")
        warn(f"{len(unclassified)} endpoint ghi chưa được phân loại, đang coi là NORMAL"
             f"{' và bị bỏ qua do --risky-only' if a.risky_only else ''}: {shown}. "
             f"Nếu có endpoint nguy hiểm, thêm vào 'risky' trong config.")
    for r in cfg["risky"]:
        if not r.get("_used"):
            warn(f"Mục 'risky' không khớp endpoint nào: {r.get('method', '*')} {r.get('path')}")
    if WARNINGS:
        print("\nCẢNH BÁO:")
        for w in WARNINGS:
            print(" -", w)


if __name__ == "__main__":
    main()
