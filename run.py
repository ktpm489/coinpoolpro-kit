#!/usr/bin/env python3
"""
Một file điều khiển cho cả bộ test.   Dùng:  python run.py <lệnh>

  check          tải spec, chạy test, in bảng ĐẠT/LỖI  (thêm --scan để quét lỗi 5xx)
  spec           tải swagger.json mới nhất, báo nếu API đã đổi
  test [...]     chỉ chạy pytest (tham số thêm chuyển cho pytest, vd:  python run.py test -k admin; thêm --raw để xem output gốc)
  cases [--all]  sinh bảng test case CSV -> output/test_cases.csv
  find-public    liệt kê GET khai báo cần token nhưng thực tế công khai
  scan [...]     Schemathesis: quét lỗi 5xx

Cấu hình: file .env (xem .env.example) và config.json.
"""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
SPEC = ROOT / "swagger.json"
CONFIG = ROOT / "config.json"
OUT = ROOT / "output"
TIMEOUT = 20


# ------------------------------------------------------------------ môi trường
def load_env():
    f = ROOT / ".env"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    os.environ.setdefault("BASE_URL", "https://cpv2-api.bacoor-test001.xyz")
    os.environ["BASE_URL"] = os.environ["BASE_URL"].rstrip("/")
    os.environ["SPEC_FILE"] = str(SPEC)
    if CONFIG.exists():
        os.environ["PROJECT_CONFIG"] = str(CONFIG)


BASE = lambda: os.environ["BASE_URL"]  # noqa: E731


def find_key(data, names):
    if isinstance(data, dict):
        for n in names:
            if data.get(n):
                return data[n]
        data = list(data.values())
    if isinstance(data, list):
        for v in data:
            found = find_key(v, names)
            if found:
                return found
    return None


def login():
    """Trả về giá trị header Authorization ('' nếu không có thông tin đăng nhập).
    Thứ tự: AUTH_HEADER, tài khoản admin, ví TEST. API đăng nhập kiểu khác thì sửa hàm này."""
    env = os.environ
    scheme = env.get("AUTH_SCHEME", "Bearer ")
    token_keys = ["token", "accessToken", "access_token", "jwt"]
    if env.get("AUTH_HEADER"):
        return env["AUTH_HEADER"]

    if env.get("ADMIN_USERNAME") and env.get("ADMIN_PASSWORD"):
        r = requests.post(f"{BASE()}/auth/admin/login", timeout=TIMEOUT,
                          json={"username": env["ADMIN_USERNAME"], "password": env["ADMIN_PASSWORD"]})
        token = find_key(r.json(), token_keys) if r.ok else None
        if not token:
            raise RuntimeError(f"POST /auth/admin/login -> {r.status_code}: {r.text[:200]}")
        return scheme + token

    if env.get("TEST_PRIVATE_KEY"):
        from eth_account import Account
        from eth_account.messages import encode_defunct
        key = env["TEST_PRIVATE_KEY"]
        addr = Account.from_key(key).address
        r = requests.get(f"{BASE()}/auth/sign", params={"address": addr}, timeout=TIMEOUT)
        body = r.json() if "json" in r.headers.get("content-type", "") else r.text
        message = body if isinstance(body, str) else find_key(body, ["message"])
        if not r.ok or not message:
            raise RuntimeError(f"GET /auth/sign -> {r.status_code}: {r.text[:200]}")
        sig = Account.sign_message(encode_defunct(text=message), key).signature.hex()
        r = requests.post(f"{BASE()}/auth/sign", timeout=TIMEOUT,
                          json={"message": message, "signature": sig if sig.startswith("0x") else "0x" + sig})
        token = find_key(r.json(), token_keys) if r.ok else None
        if not token:
            raise RuntimeError(f"POST /auth/sign -> {r.status_code}: {r.text[:200]}")
        return scheme + token
    return ""


def prepare(need_login=True):
    load_env()
    if not need_login:
        return
    try:
        header = login()
    except Exception as e:  # noqa: BLE001
        print(f"CẢNH BÁO: không đăng nhập được ({e}). Chạy tiếp mà không có token.")
        header = ""
    if header:
        os.environ["AUTH_HEADER"] = header
    else:
        os.environ.pop("AUTH_HEADER", None)
        print("Lưu ý: chưa có thông tin đăng nhập trong .env, các test cần token sẽ bị bỏ qua hoặc thấy 401.")


# ------------------------------------------------------------------ các lệnh
def cmd_spec(_args=()):
    load_env()
    url = os.environ.get("SPEC_URL") or f"{BASE()}/swagger.json"
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        new = r.json()
    except Exception as e:  # noqa: BLE001
        if SPEC.exists():
            print(f"Không tải được {url} ({e}). Dùng swagger.json có sẵn.")
            return 0
        print(f"Không tải được {url}: {e}")
        return 1
    old = json.loads(SPEC.read_text(encoding="utf-8")) if SPEC.exists() else None
    SPEC.write_text(json.dumps(new, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Đã lưu swagger.json ({len(new.get('paths', {}))} đường dẫn).")
    if old is not None and old != new:
        print("CẢNH BÁO: API đã THAY ĐỔI so với bản trước. Xem: git diff swagger.json, rồi chạy lại 'python run.py cases'.")
    return 0


# Tên dễ hiểu + gợi ý xử lý cho từng nhóm test
GROUPS = {
    "test_spec_has_paths": ("Đọc được swagger", "File swagger.json trống hoặc hỏng: chạy 'python run.py spec'."),
    "test_get_never_5xx": ("GET không được lỗi server (5xx)", "Server bị crash khi gọi endpoint này: báo dev kèm đường dẫn và mã lỗi."),
    "test_unknown_route_not_5xx": ("Đường dẫn không tồn tại không được lỗi 5xx", "Server phải trả 404, không phải 5xx: báo dev."),
    "test_protected_rejects_missing_token": ("Không có token thì phải bị từ chối (401/403)",
        "Nhận 200: endpoint thiếu kiểm tra đăng nhập (lỗ hổng) hoặc thực ra công khai: chạy 'python run.py find-public'. "
        "Nhận 400/404: thêm giá trị đúng vào tests.samples trong config.json."),
    "test_protected_rejects_bad_token": ("Token sai thì phải bị từ chối (401/403)", "Token sai mà vẫn qua hoặc lỗi khác: hỏi dev."),
    "test_malformed_json_not_5xx": ("Gửi JSON hỏng không được lỗi 5xx", "Server crash khi nhận dữ liệu sai: báo dev."),
    "test_invalid_enum_rejected": ("Giá trị ngoài danh sách phải bị 4xx", "API nhận giá trị sai mà không báo lỗi: báo dev."),
    "test_user_token_forbidden_on_admin": ("User thường không vào được /admin", "User thường vào được trang admin (lỗ hổng phân quyền): báo dev gấp."),
}


EXPECT = {
    "test_get_never_5xx": "Không lỗi server (dưới 500)",
    "test_unknown_route_not_5xx": "Trả 404, không lỗi server",
    "test_protected_rejects_missing_token": "Bị từ chối: 401 hoặc 403",
    "test_protected_rejects_bad_token": "Bị từ chối: 401 hoặc 403",
    "test_malformed_json_not_5xx": "Báo lỗi 4xx, không lỗi server",
    "test_invalid_enum_rejected": "Báo lỗi 4xx (giá trị không hợp lệ)",
    "test_user_token_forbidden_on_admin": "Bị từ chối: 401 hoặc 403",
    "test_spec_has_paths": "swagger.json có dữ liệu",
}


def _group_of(name):
    base = name.split("[")[0]
    return GROUPS.get(base, (base, "Gửi nguyên thông báo cho người hỗ trợ."))


def summarize_junit(xml_path):
    """Đọc kết quả pytest, in bảng theo nhóm và danh sách lỗi dễ đọc."""
    import xml.etree.ElementTree as ET
    from collections import OrderedDict
    rows = OrderedDict()
    detail = []
    for tc in ET.parse(xml_path).getroot().iter("testcase"):
        name = tc.get("name", "")
        title, hint = _group_of(name)
        g = rows.setdefault(title, {"hint": hint, "ok": 0, "skip": 0, "fail": []})
        where = name[name.find("[") + 1:-1] if "[" in name else "-"
        bad = tc.find("failure") if tc.find("failure") is not None else tc.find("error")
        if bad is not None:
            msg = " ".join((bad.get("message") or "").split())
            msg = msg.replace("AssertionError: ", "").split("assert ")[0].strip() or msg
            msg = re.split(r": [\{\[]|\. Nếu ", msg)[0].strip()[:120]
            g["fail"].append((where, msg))
            m = re.search(r"(?:nhận|->)\s*(\d{3})", msg)
            detail.append((title, where, "LỖI", EXPECT.get(name.split("[")[0], ""), m.group(1) if m else "", msg))
        elif tc.find("skipped") is not None:
            g["skip"] += 1
            sk = tc.find("skipped")
            detail.append((title, where, "BỎ QUA", EXPECT.get(name.split("[")[0], ""), "", (sk.get("message") or "")[:120]))
        else:
            g["ok"] += 1
            detail.append((title, where, "ĐẠT", EXPECT.get(name.split("[")[0], ""), "", ""))
    import csv
    order = {"LỖI": 0, "BỎ QUA": 1, "ĐẠT": 2}
    detail.sort(key=lambda d: order[d[2]])
    csv_path = OUT / "ket_qua.csv"
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:  # utf-8-sig để Excel đọc đúng tiếng Việt
        w = csv.writer(f)
        w.writerow(["Nhóm test", "Endpoint", "Kết quả", "Mong đợi", "Mã thực tế", "Ghi chú"])
        w.writerows(detail)
    total_fail = sum(len(g["fail"]) for g in rows.values())
    print(f"\n{'KẾT QUẢ':<58}{'đạt':>6}{'lỗi':>6}{'bỏ qua':>8}")
    print("-" * 78)
    for title, g in rows.items():
        mark = "✗" if g["fail"] else ("-" if not g["ok"] else "✓")
        print(f"{mark} {title:<56}{g['ok']:>6}{len(g['fail']):>6}{g['skip']:>8}")
    skipped = sum(g["skip"] for g in rows.values())
    if skipped:
        print(f"\n(Bỏ qua {skipped} test vì thiếu thông tin đăng nhập hoặc không áp dụng. Xem .env.example.)")
    if total_fail:
        print(f"\n=== {total_fail} LỖI CẦN XỬ LÝ ===")
        for title, g in rows.items():
            if not g["fail"]:
                continue
            print(f"\n▶ {title}  ({len(g['fail'])} lỗi)\n  Cách xử lý: {g['hint']}")
            by_msg = OrderedDict()
            for where, msg in g["fail"]:
                by_msg.setdefault(msg, []).append(where)
            for msg, wheres in by_msg.items():
                print(f"   • {msg}  ({len(wheres)} endpoint)")
                for w in wheres[:8]:
                    print(f"       - {w}")
                if len(wheres) > 8:
                    print(f"       ... và {len(wheres) - 8} endpoint nữa (xem output/report.html)")
    else:
        print("\nTất cả test đạt.")
    print(f"\nBảng từng endpoint (mở bằng Excel, lọc cột 'Kết quả'): {csv_path}")
    return total_fail


def cmd_test(args=()):
    prepare()
    if not SPEC.exists():
        print("Chưa có swagger.json. Chạy trước: python run.py spec")
        return 2
    args = list(args)
    raw = "--raw" in args
    if raw:
        args.remove("--raw")
    OUT.mkdir(exist_ok=True)
    xml = OUT / "results.xml"
    cmd = [sys.executable, "-m", "pytest", "tests", f"--junitxml={xml}"]
    cmd += ["-ra"] if raw else ["-q", "--tb=no"]
    if importlib.util.find_spec("pytest_html"):
        cmd += [f"--html={OUT / 'report.html'}", "--self-contained-html"]
    if not raw:
        print("Đang chạy test, vui lòng đợi (có thể mất vài phút)...")
    code = subprocess.call(cmd + args, cwd=ROOT, stdout=None if raw else subprocess.DEVNULL)
    if not raw and xml.exists():
        summarize_junit(xml)
    return code


def cmd_cases(args=()):
    load_env()
    args = list(args)
    all_cases = "--all" in args
    args = [a for a in args if a != "--all"]
    OUT.mkdir(exist_ok=True)
    cmd = [sys.executable, str(ROOT / "tools" / "gen_cases.py"), str(SPEC), "-o", str(OUT / "test_cases.csv")]
    if CONFIG.exists():
        cmd += ["-c", str(CONFIG)]
    if not all_cases:
        cmd.append("--risky-only")
    return subprocess.call(cmd + args, cwd=ROOT)


def cmd_find_public(_args=()):
    prepare(need_login=False)
    sys.path.insert(0, str(ROOT / "tests"))
    import test_generic as g

    found, checked = [], 0
    for m, path, op, params in g.operations():
        if m != "get" or not g.is_protected(m, path, op):
            continue
        url, query = g.build(path, params)
        checked += 1
        try:
            r = g.call("GET", url, {}, params=query)
        except requests.RequestException as e:
            print(f"  (lỗi mạng) GET {path}: {e}")
            continue
        if r.status_code not in g.AUTH_STATUSES:
            found.append((f"GET {path}", r.status_code))
    print(f"Đã thử {checked} GET khai báo cần đăng nhập, gọi không token.")
    if not found:
        print("Tất cả đều bị từ chối (401/403) đúng như spec.")
        return 0
    print(f"\n{len(found)} endpoint KHÔNG bị từ chối khi không có token:")
    for k, c in found:
        print(f"  {c}  {k}")
    print('\nNếu dev xác nhận chúng CHỦ ĐÍCH công khai, dán vào config.json:\n  "tests": { "public": '
          + json.dumps([k for k, _ in found], ensure_ascii=False) + " }")
    print("Nếu không phải chủ đích thì đó là lỗ hổng: báo dev, đừng dán.")
    print("Mã 400/404 nghĩa là API kiểm tra dữ liệu trước token: thử thêm giá trị đúng vào tests.samples.")
    return 0


def cmd_scan(args=()):
    prepare()
    if not shutil.which("schemathesis"):
        print("Chưa cài Schemathesis. Chạy: pip install -r requirements.txt")
        return 127
    cmd = ["schemathesis", "run", str(SPEC), "--url", BASE(), "--include-method", "GET",
           "--checks", "not_a_server_error", "--max-examples", os.environ.get("SCAN_MAX_EXAMPLES", "50"),
           "--report", "junit", "--report-dir", str(OUT / "schemathesis")]
    if os.environ.get("AUTH_HEADER"):
        cmd += ["-H", f"Authorization: {os.environ['AUTH_HEADER']}"]
    env = {"HYPOTHESIS_STORAGE_DIRECTORY": str(OUT / ".hypothesis"), **os.environ}
    return subprocess.call(cmd + list(args), cwd=ROOT, env=env)


def cmd_check(args=()):
    load_env()
    results = []
    print("=== Tải swagger.json ===")
    results.append(("Tải swagger.json", cmd_spec() == 0))
    if not SPEC.exists():
        print("\nKhông có swagger.json nên dừng.")
    else:
        print("\n=== Test tự sinh từ spec ===")
        results.append(("Test tự sinh từ spec", cmd_test() == 0))
        if "--scan" in args:
            print("\n=== Quét lỗi 5xx (Schemathesis) ===")
            results.append(("Quét lỗi 5xx", cmd_scan() == 0))
    print("\n=== TÓM TẮT ===")
    for name, ok in results:
        print(f"{'ĐẠT ' if ok else 'LỖI '}  {name}")
    if (OUT / "report.html").exists():
        print(f"Báo cáo chi tiết: {OUT / 'report.html'}")
    return 0 if all(ok for _, ok in results) and SPEC.exists() else 1


COMMANDS = {"check": cmd_check, "spec": cmd_spec, "test": cmd_test, "cases": cmd_cases,
            "find-public": cmd_find_public, "scan": cmd_scan}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(0 if len(sys.argv) < 2 else 2)
    sys.exit(COMMANDS[sys.argv[1]](sys.argv[2:]))
