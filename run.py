#!/usr/bin/env python3
"""
Một file điều khiển cho cả bộ test.   Dùng:  python run.py <lệnh>

  check          tải spec, chạy test, in bảng ĐẠT/LỖI  (thêm --scan để quét lỗi 5xx)
  spec           tải swagger.json mới nhất, báo nếu API đã đổi
  test [...]     chỉ chạy pytest (tham số thêm chuyển cho pytest, vd:  python run.py test -k admin)
  cases [--all]  sinh bảng test case CSV -> output/test_cases.csv
  find-public    liệt kê GET khai báo cần token nhưng thực tế công khai
  scan [...]     Schemathesis: quét lỗi 5xx

Cấu hình: file .env (xem .env.example) và config.json.
"""
import importlib.util
import json
import os
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


def cmd_test(args=()):
    prepare()
    if not SPEC.exists():
        print("Chưa có swagger.json. Chạy trước: python run.py spec")
        return 2
    cmd = [sys.executable, "-m", "pytest", "tests", "-ra"]
    if importlib.util.find_spec("pytest_html"):
        OUT.mkdir(exist_ok=True)
        cmd += [f"--html={OUT / 'report.html'}", "--self-contained-html"]
    return subprocess.call(cmd + list(args), cwd=ROOT)


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
