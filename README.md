# coinpoolpro-kit: test Coinpool V2 API

Test tự động từ file swagger của API. Chỉ cần Python 3.9+.

## Cài và chạy

```
pip install -r requirements.txt
```
Copy `.env.example` thành `.env`, bỏ dấu `#` ở **một** dòng đăng nhập (`AUTH_HEADER`, hoặc `ADMIN_USERNAME` + `ADMIN_PASSWORD`, hoặc `TEST_PRIVATE_KEY`). Rồi:

```
python run.py check
```
Lệnh này tải swagger mới, chạy test, in bảng ĐẠT/LỖI. Báo cáo chi tiết: `output/report.html`.

(Windows nếu `python` không chạy thì dùng `py run.py check`.)

## Các lệnh

| Lệnh | Việc |
|---|---|
| `python run.py check` | tải spec + chạy test (thêm `--scan` để quét lỗi 5xx) |
| `python run.py test -k admin` | chỉ chạy test, tham số thêm chuyển cho pytest |
| `python run.py cases` | sinh 636 test case ra `output/test_cases.csv` (mở bằng Excel) |
| `python run.py find-public` | liệt kê GET thực tế công khai dù spec khai báo cần token |
| `python run.py spec` | chỉ tải swagger, báo nếu API đã đổi |
| `python run.py scan` | chỉ quét lỗi 5xx bằng Schemathesis |

## Test gồm những gì

Tự sinh từ spec, nằm trong `tests/test_generic.py`:

1. Mọi GET không được trả lỗi 5xx
2. Route không tồn tại không được trả 5xx
3. Endpoint cần đăng nhập: không token hoặc token sai phải bị 401/403
4. Endpoint ghi dữ liệu: gửi JSON hỏng không được trả 5xx
5. Tham số enum: giá trị ngoài danh sách phải bị 4xx

Thêm `tests/test_admin_access.py`: token user thường vào endpoint `/admin`, `/keyring` phải bị 401/403 (cần `USER_AUTH_HEADER` hoặc `TEST_PRIVATE_KEY` của ví không phải admin, không có thì tự bỏ qua).

## Khi có test fail

- **Thiếu token nhưng trả 200**: chạy `python run.py find-public`, hỏi dev endpoint đó có công khai không. Nếu có, dán đoạn lệnh in ra vào `config.json`. Nếu không, đó là lỗ hổng.
- **Trả 400/404 thay vì 401**: API kiểm tra dữ liệu trước token. Thêm giá trị đúng vào `tests.samples` trong `config.json`.
- Lỗi khác: gửi nguyên đoạn thông báo cho người hỗ trợ.

## config.json

| Khóa | Ý nghĩa |
|---|---|
| `risky` | endpoint ghi dữ liệu nguy hiểm kèm ghi chú (28 endpoint đã khai báo). `"risky": false` để loại trừ |
| `extra` | test case viết tay thêm vào CSV (18 case đã có) |
| `field_samples`, `id_params` | giá trị mẫu theo tên trường, tên tham số path được coi là id |
| `tests.public` | endpoint công khai có chủ đích |
| `tests.skip` | endpoint bỏ qua trong test chung, ví dụ `["GET /health"]` |
| `tests.samples` | giá trị mẫu theo tên tham số, ví dụ `{"poolId": "0xabc"}` |
| `tests.auth_statuses` | mã coi là "từ chối" (mặc định `[401, 403]`) |
| `tests.assume_protected` | `true` nếu spec không khai báo security nhưng thực tế cần đăng nhập |

## An toàn

- Chỉ chạy trên môi trường **test**.
- Mọi test mặc định **không** thực hiện thao tác tạo/sửa/xoá thật: chỉ gửi body hỏng hoặc id không tồn tại. Đừng bật `ALLOW_DESTRUCTIVE=1` khi chưa hỏi dev về `/notification`, `/pair/mint-position`, `/multisig-contract/create`, `/upload/file`.
- `.env` chứa bí mật, đã nằm trong `.gitignore`.
- Chỉ dùng ví test, không dùng ví có tiền.

## Chưa kiểm chứng

Chưa chạy trên API Coinpool thật. Có thể phải chỉnh sau lần chạy đầu: danh sách endpoint công khai, mã trả về khi thiếu token, định dạng response của `/auth/sign` và `/auth/admin/login` (hàm `login()` trong `run.py`), và giá trị mẫu cho `poolId`, `tokenA/tokenB`, `contractAddress`.

## Cấu trúc

```
run.py                 file điều khiển (mọi lệnh ở trên)
config.json            cấu hình riêng của API này
swagger.json           spec đã tải (nên commit để thấy API đổi gì)
tests/                 test_generic.py, test_admin_access.py
tools/gen_cases.py     bộ sinh test case
.env.example           mẫu cấu hình đăng nhập
```
