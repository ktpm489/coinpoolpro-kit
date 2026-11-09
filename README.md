# coinpoolpro-kit: test Coinpool V2 API

Test tự động từ file swagger của API. Chỉ cần Python 3.9+.

## Cài và chạy

**macOS / Linux**: chạy một lệnh duy nhất, không cần tự cài hay activate gì:

```sh
cd coinpoolpro-kit
./run.sh
```

`./run.sh` lần đầu tự tạo `.venv` và cài thư viện, các lần sau chạy luôn. Không có tham số thì chạy `check`. Có tham số thì dùng như `python run.py`, ví dụ `./run.sh test -k admin`, `./run.sh find-public`.

Nếu gõ `python run.py ...` mà báo `command not found: python`: máy chỉ có `python3`, và bạn chưa bật venv. Dùng `./run.sh ...`, hoặc bật venv thủ công:

```sh
source .venv/bin/activate    # đầu dòng lệnh có (.venv) là đã bật
python run.py check
```

Thoát venv: `deactivate`. Cài hỏng thì `rm -rf .venv` rồi chạy lại `./run.sh`. Không muốn dùng venv: `python3 -m pip install -r requirements.txt` rồi `python3 run.py ...`.

**Windows** (không có `run.sh`):

```
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
py run.py check
```

Copy `.env.example` thành `.env`, bỏ dấu `#` ở **một** dòng đăng nhập (`AUTH_HEADER`, hoặc `ADMIN_USERNAME` + `ADMIN_PASSWORD`, hoặc `TEST_PRIVATE_KEY`). Rồi chạy `./run.sh` (macOS/Linux) hoặc `py run.py check` (Windows). Lệnh này tải swagger mới, chạy test, in bảng ĐẠT/LỖI. Báo cáo chi tiết: `output/report.html`.

Kết quả in ra theo từng nhóm (✓ đạt, ✗ có lỗi, - bị bỏ qua), kèm danh sách endpoint lỗi gộp theo mã trả về và gợi ý cách xử lý. Muốn xem output gốc của pytest: `python run.py test --raw`.

Các file kết quả trong `output/`:

| File | Dùng để |
|---|---|
| `ket_qua.csv` | Bảng từng endpoint, mở bằng Excel. Cột: Nhóm test, Endpoint, Kết quả (LỖI/BỎ QUA/ĐẠT), Mong đợi, Mã thực tế, Ghi chú, **Cách gọi test đã gửi (curl)** và **Tham số đã gửi** (đúng request mà test gửi, để tái hiện lỗi), **Tham số theo spec** (tên, nơi đặt query/path/body, kiểu, bắt buộc hay không, các giá trị enum) và **Cách gọi hợp lệ (curl)** (request đúng theo spec kèm body mẫu, để tester gọi thử bình thường; lệnh ghi dữ liệu có dòng cảnh báo). Dòng LỖI nằm trên cùng, lọc cột "Kết quả" hoặc "Mã thực tế" để chia việc hoặc gửi dev. Hai cột **Postman** ghi sẵn từng ô cần nhập (Method, URL, Params, Headers, Body raw JSON), một cột cho request test đã gửi, một cột cho cách gọi hợp lệ. Trong Postman, tạo biến `token` (Environment hoặc Collection variable) có giá trị `Bearer xxx`; ô Authorization ghi `{{token}}` sẽ tự lấy giá trị đó. Các lệnh curl dùng biến `$TOKEN`, đặt trước khi chạy: `export TOKEN='Bearer xxx'` (token thật không bao giờ được ghi vào file) |
| `postman_collection.json` | Import thẳng vào Postman (xem mục dưới) |
| `report.html` | Báo cáo kỹ thuật đầy đủ, dùng khi cần gỡ lỗi |

## Gọi bằng Postman (import là chạy)

`./run.sh` (hoặc `python run.py postman`) sinh `output/postman_collection.json` từ swagger: mọi endpoint, chia thư mục theo tag, đã có URL, tham số (tham số tùy chọn để tắt sẵn), body mẫu, header và một test kiểm tra không lỗi 5xx.

1. Postman: **Import** > chọn `output/postman_collection.json`.
2. Mở collection > tab **Variables**: điền `token` = `Bearer xxx` (không cần token thì để trống), kiểm tra `baseUrl`. Bấm Save.
3. Chọn request > **Send**. Hoặc chuột phải collection > **Run collection** để chạy tất cả.

Lưu ý: collection có cả POST/PUT/DELETE với dữ liệu mẫu, các request ghi dữ liệu ghi chú "CẢNH BÁO" trong phần Description. Chỉ chạy trên môi trường test, và đừng bấm Run collection khi chưa loại các request nguy hiểm (ví dụ `/notification`, `/pair/mint-position`, `/multisig-contract/create`, `/upload/file`).

## Các lệnh

| Lệnh | Việc |
|---|---|
| `./run.sh` hoặc `python run.py check` | tải spec + chạy test (thêm `--scan` để quét lỗi 5xx) |
| `python run.py test -k admin` | chỉ chạy test, tham số thêm chuyển cho pytest |
| `python run.py cases` | sinh 636 test case ra `output/test_cases.csv` (mở bằng Excel) |
| `python run.py find-public` | liệt kê GET thực tế công khai dù spec khai báo cần token |
| `python run.py postman` | sinh collection Postman ra `output/postman_collection.json` |
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
run.sh                 chạy nhanh trên macOS/Linux (tự lo venv)
run.py                 file điều khiển (mọi lệnh ở trên)
config.json            cấu hình riêng của API này
swagger.json           spec đã tải (nên commit để thấy API đổi gì)
tests/                 test_generic.py, test_admin_access.py
tools/gen_cases.py     bộ sinh test case
.env.example           mẫu cấu hình đăng nhập
```
