# Logic mã nguồn: coinpoolpro-kit

Mô tả cách mã hiện tại hoạt động. Yêu cầu nghiệp vụ nằm ở [requirement.md](requirement.md); mã FR/NFR dưới đây trỏ về tài liệu đó.

## 1. Cấu trúc

```
run.sh                 chạy nhanh macOS/Linux (tạo .venv, cài thư viện, gọi run.py)
run.py                 điều khiển: spec, test, cases, postman, scan, find-public, check
config.json            cấu hình riêng của API (risky, extra, samples, tests.*)
swagger.json           spec đã tải
.env / .env.example    thông tin đăng nhập, BASE_URL (.env không commit)
tests/conftest.py      ghi lại request mà mỗi test đã gửi
tests/test_generic.py  test chung sinh từ swagger
tests/test_admin_access.py  phân quyền user thường vs admin
tests/test_security.py      test bảo mật nâng cao
tools/gen_cases.py     sinh bảng test case CSV, tra tham số theo spec
output/                kết quả (ket_qua.csv, report.html, results.xml, postman_collection.json)
```

## 2. Luồng chạy `./run.sh` (`python run.py check`)

1. `load_env()`: đọc `.env` (không ghi đè biến đã có), đặt mặc định `BASE_URL`, đặt `SPEC_FILE` và `PROJECT_CONFIG` cho pytest.
2. `cmd_spec()`: tải swagger mới, so với bản cũ, báo nếu API đổi (FR-01).
3. `prepare()` gọi `login()`. Thứ tự: `AUTH_HEADER` > `ADMIN_USERNAME`/`ADMIN_PASSWORD` qua `POST /auth/admin/login` > `TEST_PRIVATE_KEY` (lấy challenge `GET /auth/sign`, ký bằng `eth_account`, `POST /auth/sign`). Lỗi đăng nhập chỉ in cảnh báo, test chạy tiếp không token. Kết quả đặt vào biến môi trường `AUTH_HEADER` cho pytest (FR-02, FR-03).
4. `cmd_test()` chạy `pytest tests --junitxml=output/results.xml` (kèm `--html` nếu có `pytest-html`). Tham số thêm (`-k security`) chuyển thẳng cho pytest.
5. `summarize_junit()` đọc `results.xml`, phân nhóm theo tên hàm test, in bảng và danh sách lỗi, ghi `ket_qua.csv` (FR-60 đến FR-62).
6. `cmd_postman()` sinh collection (FR-64).

Các lệnh khác: `cases` (gọi `tools/gen_cases.py`), `find-public`, `scan` (Schemathesis).

## 3. Ghi lại request (`tests/conftest.py`)

- Bọc `requests.Session.request`: mỗi lần gọi lưu `method`, `url`, header `Authorization`/`Content-Type`, 500 ký tự đầu của body.
- `_mask()` che Authorization dài hơn 40 ký tự thành `<token>`, token giả ngắn giữ nguyên (FR-63).
- Hook `pytest_runtest_call` xóa danh sách trước thân test (bước đăng nhập ở fixture không tính) rồi gắn JSON vào `user_properties["request"]`. `describe_request()` trong `run.py` dùng nó dựng lệnh curl, tham số và Postman.

## 4. Test chung (`tests/test_generic.py`)

**Đọc spec**
- `SPEC` từ `SPEC_FILE`; `CONFIG` từ `PROJECT_CONFIG`; `T = CONFIG["tests"]`.
- `_resolve()` giải `$ref` nội bộ một tầng (swagger 2 dùng `#/definitions/...`).
- `operations()` sinh `(method, path, op, params)`, tham số cấp path cộng cấp operation.
- `sample(p)` chọn giá trị mẫu theo thứ tự: `tests.samples` > `field_samples` > enum > pattern (`0x...`, 24 ký tự, uuid) > kiểu > id mặc định 24 số 0 > default.
- `build(path, params)` thay tham số path và gom query bắt buộc.
- `is_protected()`: endpoint trong `tests.public` là công khai; `security` cấp operation; rồi `security` toàn cục; rồi `assume_protected`. Spec này chỉ có `security` toàn cục (`jwt`), nên mọi endpoint ngoài `public` được coi là cần token.
- `is_risky()`: DELETE, hoặc có trong `risky` của config, hoặc đường dẫn chứa từ khóa nguy hiểm (`delete`, `send`, `transfer`...); `"risky": false` trong config loại trừ.

**Các test**
| Hàm | Logic |
|---|---|
| `test_get_never_5xx` | GET với tham số mẫu, mã < 500; có `max_get_ms` thì kiểm thời gian (FR-10, FR-16) |
| `test_unknown_route_not_5xx` | GET/POST/DELETE `/__khong_ton_tai__/abc` (FR-11) |
| `test_protected_rejects_missing_token` | Không token, mã phải thuộc `AUTH_STATUSES` (FR-12) |
| `test_protected_rejects_bad_token` | `Bearer token.sai.hoan.toan` (FR-13) |
| `test_malformed_json_not_5xx` | Có token, gửi `{khong phai json`; chỉ chạy khi có `AUTH_HEADER` và endpoint không nguy hiểm (trừ `ALLOW_DESTRUCTIVE`) (FR-14) |
| `test_invalid_enum_rejected` | GET, tham số enum đổi thành giá trị lạ, mong 4xx (FR-15) |

An toàn của FR-12/13: `_safe_without_token()` chỉ chạy endpoint nguy hiểm khi có body (gửi JSON hỏng, nên thiếu kiểm tra token cũng không thực thi được) hoặc có tham số path (id giả) hoặc `ALLOW_DESTRUCTIVE=1`.

## 5. Phân quyền (`tests/test_admin_access.py`)

- `_user_token()`: `USER_AUTH_HEADER`, nếu không có thì đăng nhập bằng `TEST_PRIVATE_KEY` qua `/auth/sign`. Không có cả hai thì fixture `user_hdr` gọi `pytest.skip` (FR-24).
- `ADMIN_PREFIXES` lấy từ `tests.admin_prefixes`, mặc định `/admin`, `/keyring` (FR-23).
- `test_user_token_forbidden_on_admin`: mọi GET của vùng admin, tham số path thay bằng địa chỉ `0x…01`, query bắt buộc lấy enum hoặc giá trị mẫu; mong 401/403 (FR-20).
- `test_user_token_forbidden_on_admin_write`: POST/PUT/PATCH có body gửi `{khong phai json}` với token user thường (nếu thiếu kiểm tra quyền, hành động vẫn không chạy). DELETE hoặc endpoint không body chỉ được sinh khi `ALLOW_DESTRUCTIVE=1` (FR-21, FR-22).

## 6. Test bảo mật (`tests/test_security.py`)

Import `test_generic as g` để dùng lại `operations`, `build`, `is_protected`, `BASE`, `AUTH_HEADER`, `AUTH_STATUSES`.

**Hằng số và tiện ích**
- `LEAK_RE`: mẫu rò rỉ nội bộ (stack Node/Python, lỗi Sequelize/Prisma/Mongo/Postgres/MySQL/Oracle, đường dẫn `/app/...js`, `"stack":`...). `SECRET_KEY_RE`: trường `password`, `privateKey`, `mnemonic`, `secret`, `apiKey`... có giá trị chuỗi.
- `PAYLOADS`: 12 payload tấn công (SQLi, NoSQL, XSS, traversal, command, template, null byte, chuỗi dài, unicode).
- `REJECT = AUTH_STATUSES ∪ {400, 413, 431}`: mã chấp nhận khi từ chối token sai định dạng hoặc quá lớn.
- `_leak_check(r, label)`: assert response không khớp `LEAK_RE`.
- `security_skip` (config): `_skip_if_configured(id)` bỏ qua test header theo id.

**Chọn endpoint probe (FR-56)**
`_protected_probes()` chạy lúc import: duyệt GET được coi là protected, sắp ưu tiên `/auth/me` > `/admin*`, `/keyring*` > còn lại, gọi thử không token, chỉ giữ endpoint trả 401/403 (tối đa 3, hoặc đúng danh sách `jwt_probe`). Lý do: lần chạy đầu cho thấy endpoint công khai thật (200) hoặc kiểm tra tham số trước token (400) làm test JWT báo sai.
`_real_token_works(url, query)`: gọi với `AUTH_HEADER`; nếu bị từ chối thì các test cần token thật tự `skip` (NFR-07).

**Nhóm test và logic**
| Hàm | Logic |
|---|---|
| `test_forged_jwt_rejected` | `_forged_tokens()` dựng token giả bằng `base64url` + `hmac` (alg none 4 biến thể, HS256 với 6 secret yếu, hết hạn, thiếu chữ ký, rỗng, null, 4 phần, 10KB); mỗi token × mỗi probe, mong `REJECT` (FR-30) |
| `test_odd_auth_scheme_rejected` | Giá trị `Authorization` lạ; mong `REJECT` (FR-32). Không dùng giá trị toàn dấu cách vì `requests` từ chối gửi |
| `test_tampered_real_jwt_rejected` | `_real_jwt()` tách `AUTH_HEADER` thành 3 phần nếu giải mã được; `_tampered(case)` sửa payload / đảo chữ ký / bỏ chữ ký / `alg=none` / ký lại bằng `secret`; skip khi không phải JWT hoặc token đã hết hạn (FR-31) |
| `test_token_in_query_not_accepted` | Token thật đưa vào `?token`, `?access_token`, `?jwt`, `?authorization`, không có header; mong 401/403 (FR-33) |
| `test_get_injection_safe` | Mỗi GET có tham số chuỗi không enum: với từng payload, thay mọi tham số chuỗi (path được `quote`) bằng payload, gom lỗi: 5xx, khớp `LEAK_RE`, XSS phản chiếu trong `text/html`, `root:x:0:0`, template `7*7` thành `49`. Assert danh sách lỗi rỗng (FR-34) |
| `test_prototype_pollution_and_hpp_not_5xx` | Ghép query: `__proto__[isAdmin]=true`, `constructor[prototype][isAdmin]=true`, `limit[]` trùng, `page` trùng, tham số đầu tiên lặp hai lần; mong < 500 và không lộ lỗi (FR-35) |
| `test_admin_login_attack_payloads` | 11 body (`LOGIN_BODIES`) tới `/auth/admin/login`: không 5xx, `not r.ok`, không có token, không lộ lỗi (FR-36) |
| `test_admin_login_wrong_password_same_message_as_unknown_user` | So mã và nội dung giữa `ADMIN_USERNAME` với mật khẩu sai và user giả; skip nếu không có `ADMIN_USERNAME` (FR-40) |
| `test_sign_in_invalid_signature_rejected` | 9 body (`SIGN_BODIES`) tới `POST /auth/sign`: không 5xx, bị từ chối, không có token (FR-37) |
| `test_sign_challenge_invalid_address_rejected` | `GET /auth/sign?address=` sai định dạng, mong 4xx (FR-38) |
| `test_sign_challenge_unique_per_address` | Hai địa chỉ khác nhau phải ra challenge khác nhau; skip nếu không 2xx (FR-39). Không yêu cầu hai lần xin cùng địa chỉ ra khác nhau vì API trả nonce cố định theo địa chỉ |
| `test_login_oversized_body_rejected` | Body 5MB, `[`×5000, số 10.000 chữ số; mong 4xx (FR-41) |
| `test_login_wrong_content_type_not_5xx` | 5 Content-Type; không 5xx, không token (FR-42) |
| `test_admin_login_rate_limited` | Chỉ khi `RUN_RATE_LIMIT=1`: 40 lần sai, đạt nếu có 429/423 (FR-43) |
| `probe_responses` (fixture) + `test_security_header_present`, `test_no_version_disclosure_headers`, `test_cookies_have_secure_flags`, `test_error_responses_do_not_leak` | Thu 3 response (route 404, một GET công khai đầu tiên, đăng nhập sai) rồi kiểm header nosniff / HSTS ≥ 15552000 (chỉ với https) / X-Frame-Options, lộ `X-Powered-By` và `Server/<số>`, cờ cookie (`raw.headers.getlist("Set-Cookie")`), lộ stack (FR-44 đến FR-46, FR-55). CSP và Referrer-Policy không yêu cầu vì API trả JSON |
| `test_responses_are_not_cached_if_private` | GET `/auth/me` (hoặc probe đầu tiên) có token: `Cache-Control` phải có no-store/private/no-cache nếu 2xx (FR-47) |
| `test_cors_does_not_trust_arbitrary_origin` | Origin `https://evil.example`, `null`, `<host>.evil.example` trên probe: cả GET và OPTIONS preflight; ACAO không bằng Origin gửi lên, không `*` kèm credentials, không `null` (FR-48) |
| `test_http_redirects_to_https` | Gọi `http://` cùng host với `allow_redirects=False`; chấp nhận lỗi kết nối, redirect tới https, hoặc 400/403/404/421/426 (FR-49) |
| `test_undeclared_method_not_executed_without_token` | Mỗi path: các method trong PUT/PATCH/DELETE không có trong spec, gửi không token với id giả; mong 401/403/404/405/400 (FR-50) |
| `test_dangerous_http_methods_disabled` | TRACE, TRACK, CONNECT, PROPFIND, FOO vào `/auth/me` kèm header marker; không 2xx, không 5xx, marker không được phản chiếu; lỗi kết nối được chấp nhận (FR-51) |
| `test_method_override_header_ignored_without_token` | POST + header override giá trị `DELETE` vào 3 GET protected đầu tiên (FR-52) |
| `test_sensitive_path_not_exposed` | 30 đường dẫn nhạy cảm; coi là lộ khi 200 và `Content-Type` không phải `text/html` (tránh báo sai do trang SPA catch-all), hoặc nội dung có `DB_PASSWORD`, `PRIVATE_KEY`, `[core]`... (FR-53) |
| `test_get_response_has_no_secret_fields` | Mọi GET (có token nếu có): response không khớp `SECRET_KEY_RE`, không lộ stack (FR-54, FR-55) |
| `test_malformed_json_error_does_not_leak` | JSON hỏng vào `/auth/sign`: không 5xx, không lộ lỗi nội bộ (FR-55) |

## 7. Đăng ký nhóm vào bảng kết quả (`run.py`)

- `GROUPS`: khóa là tên hàm test (phần trước `[`), giá trị là `(tên nhóm tiếng Việt, gợi ý xử lý)`. Hàm chưa đăng ký sẽ hiện tên hàm với gợi ý chung "Gửi nguyên thông báo cho người hỗ trợ". Mọi hàm mới của `test_security.py` và `test_user_token_forbidden_on_admin_write` đã được đăng ký (FR-68).
- `EXPECT`: cột "Mong đợi" trong CSV. Chưa thêm cho các nhóm bảo mật mới, cột này để trống cho chúng.
- Id tham số hóa quyết định cột "Endpoint" trong CSV: id dạng `GET /path` cho phép `endpoint_doc()` tra spec để điền tham số và curl hợp lệ. Id dạng khác (tên payload, tên token giả) hiển thị nguyên văn và không có phần tra spec.
- Lỗi được gộp theo thông điệp: `summarize_junit` cắt thông điệp tại `: {`, `: [` hoặc `. Nếu `, lấy 120 ký tự và trích mã 3 chữ số sau "nhận" hoặc "->" làm "Mã thực tế".

## 8. Quy tắc an toàn trong mã (NFR-01)

- Payload tấn công chỉ gửi vào GET (có thể có token) và hai endpoint đăng nhập công khai.
- Test không token chỉ gửi body hỏng hoặc id giả; DELETE tới path không khai báo dùng id mẫu và không token.
- Test phân quyền ghi dùng body hỏng; DELETE không body chỉ chạy khi `ALLOW_DESTRUCTIVE=1`.
- Rate limit mặc định tắt; chỉ dùng tài khoản `qa-bruteforce-N` không tồn tại.

## 9. Điểm cần lưu ý khi bảo trì

- `test_security.py` thực hiện vài request lúc import (chọn probe); mạng lỗi lúc đó dẫn tới danh sách probe rỗng và các test probe bị bỏ qua, không phải lỗi.
- `test_security.py` phụ thuộc `test_generic.py` (import trực tiếp); đổi tên hàm hoặc hằng số ở đó phải cập nhật cả hai.
- Python 3.9 trong `.venv` dùng LibreSSL nên `urllib3` in cảnh báo `NotOpenSSLWarning`; không ảnh hưởng kết quả.
- Muốn thêm nhóm test mới: viết hàm trong `tests/`, thêm mục vào `GROUPS` (và `EXPECT` nếu muốn có cột mong đợi), cập nhật README và hai tài liệu này.
