# Yêu cầu nghiệp vụ: coinpoolpro-kit

Tài liệu BA mô tả yêu cầu của bộ công cụ kiểm thử tự động cho Coinpool V2 API. Cách mã nguồn thực hiện từng yêu cầu nằm ở [logic.md](logic.md).

## 1. Bối cảnh và mục tiêu

Coinpool V2 API quản lý ví, vị thế thanh khoản, pool, thông báo và các thao tác quản trị. API xử lý dữ liệu liên quan đến tài sản nên lỗi phân quyền hoặc lỗi xác thực có hậu quả trực tiếp.

Đội QA cần một bộ công cụ chạy bằng một lệnh, không phải viết test thủ công cho từng endpoint, và cho ra báo cáo mà người không chuyên lập trình vẫn đọc được.

**Mục tiêu**
1. Tự sinh và chạy test từ file swagger của API, khi API đổi thì test tự đổi theo.
2. Phát hiện sớm lỗi server (5xx), lỗi xác thực, lỗi phân quyền và các lỗ hổng bảo mật phổ biến.
3. Xuất báo cáo cho tester và dev: endpoint nào lỗi, gửi gì, nhận gì, cách tái hiện, cách xử lý.
4. Không làm hỏng dữ liệu của môi trường test.

## 2. Đối tượng sử dụng

| Vai trò | Nhu cầu |
|---|---|
| Tester (QA) | Chạy một lệnh, đọc bảng ĐẠT/LỖI, lọc theo mã trả về, gửi dev kèm lệnh tái hiện |
| Dev backend | Nhận danh sách lỗi có request cụ thể và gợi ý sửa |
| BA / quản lý | Xem tổng số đạt/lỗi theo nhóm, biết mức rủi ro |

## 3. Phạm vi

**Trong phạm vi**
- Môi trường test của Coinpool V2 API (`BASE_URL` trong `.env`).
- Kiểm thử hộp đen qua HTTP, dựa trên `swagger.json`.
- Xác thực, phân quyền, đầu vào không hợp lệ, injection, cấu hình bảo mật HTTP.
- Xuất CSV, HTML, collection Postman.

**Ngoài phạm vi**
- Môi trường production và ví có tiền thật.
- Kiểm thử hiệu năng tải, kiểm thử giao diện.
- Kiểm tra đúng sai nghiệp vụ của từng response (số liệu pool, lợi nhuận...).
- Kiểm thử smart contract và giao dịch on-chain.
- Khai thác lỗ hổng để lấy dữ liệu. Test chỉ xác nhận API từ chối hoặc không lỗi.

## 4. Ràng buộc và giả định

- Python 3.9 trở lên. macOS/Linux dùng `./run.sh`, Windows dùng `py run.py`.
- Chỉ chạy trên môi trường test. Chỉ dùng ví test.
- Mặc định không thực hiện thao tác tạo/sửa/xóa thật: chỉ gửi body hỏng hoặc id không tồn tại. Chỉ bật `ALLOW_DESTRUCTIVE=1` sau khi hỏi dev.
- Bí mật (`.env`, token, private key) không được commit và không được ghi vào file kết quả.
- Giả định định dạng response của `/auth/sign` và `/auth/admin/login` là JSON có trường `token`.
- Giả định token là JWT khi chạy các test sửa token thật.

## 5. Yêu cầu chức năng

Mức ưu tiên: **P1** bắt buộc, **P2** nên có, **P3** có thì tốt.

### 5.1 Quản lý spec, đăng nhập và cấu hình

| Mã | Yêu cầu | Ưu tiên |
|---|---|---|
| FR-01 | Tải `swagger.json` mới nhất và báo khi API đổi so với bản đã lưu (`run.py spec`) | P1 |
| FR-02 | Đọc cấu hình đăng nhập từ `.env`, chấp nhận một trong ba cách: `AUTH_HEADER`, `ADMIN_USERNAME` + `ADMIN_PASSWORD`, hoặc `TEST_PRIVATE_KEY` | P1 |
| FR-03 | Không có thông tin đăng nhập thì vẫn chạy phần không cần token, các test cần token tự bỏ qua kèm lý do | P1 |
| FR-04 | Tinh chỉnh bằng `config.json` mà không sửa code: endpoint công khai, bỏ qua, giá trị mẫu, mã coi là từ chối, danh sách endpoint nguy hiểm | P1 |
| FR-05 | Một lệnh duy nhất `./run.sh` tự tạo môi trường ảo, cài thư viện, tải spec, chạy test, in kết quả | P1 |

### 5.2 Test chung (sinh từ swagger)

| Mã | Yêu cầu | Tiêu chí chấp nhận | Ưu tiên |
|---|---|---|---|
| FR-10 | Mọi GET không trả 5xx | Mã trả về < 500 với tham số mẫu hợp lệ | P1 |
| FR-11 | Route không tồn tại không trả 5xx | GET/POST/DELETE vào đường dẫn lạ trả < 500 | P1 |
| FR-12 | Endpoint cần đăng nhập từ chối request không token | Mã thuộc `auth_statuses` (mặc định 401, 403) | P1 |
| FR-13 | Endpoint cần đăng nhập từ chối token sai | Như FR-12 | P1 |
| FR-14 | Endpoint ghi dữ liệu không trả 5xx khi nhận JSON hỏng | Mã < 500 | P1 |
| FR-15 | Tham số enum từ chối giá trị ngoài danh sách | Mã 4xx | P2 |
| FR-16 | Cảnh báo GET chậm hơn ngưỡng `max_get_ms` nếu được cấu hình | Fail khi vượt ngưỡng | P3 |

### 5.3 Phân quyền

| Mã | Yêu cầu | Tiêu chí chấp nhận | Ưu tiên |
|---|---|---|---|
| FR-20 | Token user thường không gọi được GET của `/admin` và `/keyring` | 401 hoặc 403 | P1 |
| FR-21 | Token user thường không gọi được POST/PUT/PATCH của `/admin` và `/keyring` | 401 hoặc 403; request chỉ gửi body hỏng nên không ghi được dữ liệu | P1 |
| FR-22 | DELETE không body của vùng admin chỉ kiểm tra khi `ALLOW_DESTRUCTIVE=1` | Mặc định không gửi | P1 |
| FR-23 | Tiền tố vùng admin cấu hình được (`tests.admin_prefixes`) | Mặc định `/admin`, `/keyring` | P2 |
| FR-24 | Không có token user thường thì test tự bỏ qua | Trạng thái BỎ QUA, không phải LỖI | P1 |

### 5.4 Bảo mật nâng cao

| Mã | Yêu cầu | Tiêu chí chấp nhận | Ưu tiên |
|---|---|---|---|
| FR-30 | Từ chối JWT giả: `alg=none` (các biến thể hoa/thường), ký bằng secret yếu (`secret`, `changeme`...), hết hạn, thiếu chữ ký, rỗng, `null`, 4 phần, 10KB | 401/403, hoặc 400/413/431 khi sai định dạng hoặc quá lớn | P1 |
| FR-31 | Từ chối token thật bị sửa: đổi payload giữ chữ ký, đảo ký tự chữ ký, bỏ chữ ký, đổi `alg` | Như FR-30; test bỏ qua khi `AUTH_HEADER` không phải JWT hoặc đã hết hạn | P1 |
| FR-32 | Từ chối header `Authorization` sai scheme (`Bearer` trống, `Basic`, `Token`...) | Như FR-30 | P2 |
| FR-33 | Không chấp nhận token ở query string (`?token=`, `?access_token=`, `?jwt=`, `?authorization=`) | 401/403 | P1 |
| FR-34 | Payload injection (SQL, NoSQL, XSS, path traversal, command, template, null byte, chuỗi 5000 ký tự, unicode điều khiển) vào mọi tham số chuỗi của GET không gây 5xx | Không 5xx; response không chứa dấu hiệu lỗi DB/stack trace; không phản chiếu XSS nguyên văn trong HTML; không đọc được `/etc/passwd`; không thực thi template | P1 |
| FR-35 | Prototype pollution (`__proto__`, `constructor[prototype]`) và trùng tham số không gây 5xx | Không 5xx, không lộ lỗi nội bộ | P2 |
| FR-36 | `/auth/admin/login` chịu được body tấn công: SQLi, NoSQL operator, kiểu sai, mảng, null, chuỗi rất dài, trường thừa, `__proto__` | Không 5xx, không đăng nhập thành công, response không chứa token | P1 |
| FR-37 | `/auth/sign` từ chối chữ ký không hợp lệ (toàn 0, rỗng, không phải hex, message tự chế, kiểu sai, message 1MB) | 4xx, không có token trong response | P1 |
| FR-38 | `GET /auth/sign` từ chối địa chỉ sai định dạng | 4xx | P2 |
| FR-39 | Challenge đăng nhập khác nhau giữa hai địa chỉ | Nội dung response khác nhau | P2 |
| FR-40 | Đăng nhập sai không lộ tài khoản có tồn tại: user thật và user giả cho cùng mã và cùng thông báo | Cần `ADMIN_USERNAME`, không có thì bỏ qua | P2 |
| FR-41 | Giới hạn kích thước/độ sâu body: body 5MB, JSON lồng 5000 tầng, số 10.000 chữ số | 4xx | P2 |
| FR-42 | Sai `Content-Type` (text/plain, xml, urlencoded, multipart, json-utf7) không gây 5xx và không cấp token | Không 5xx | P2 |
| FR-43 | Rate limit đăng nhập admin: 40 lần sai liên tiếp phải bị 429 hoặc 423 | Chỉ chạy khi `RUN_RATE_LIMIT=1` | P2 |
| FR-44 | Header bảo mật có mặt: `X-Content-Type-Options: nosniff`, `Strict-Transport-Security` (max-age tối thiểu 15552000), `X-Frame-Options` DENY hoặc SAMEORIGIN | Kiểm trên route 404, một GET công khai, một lỗi đăng nhập | P2 |
| FR-45 | Không lộ công nghệ/phiên bản: `X-Powered-By`, `Server` kèm số phiên bản, `X-AspNet-Version`... | Header không có mặt | P3 |
| FR-46 | Cookie (nếu có) có `HttpOnly`, `Secure`, `SameSite` | Đủ ba cờ | P2 |
| FR-47 | Response riêng tư (ví dụ `/auth/me`) có `Cache-Control` là `no-store`, `private` hoặc `no-cache` | Có một trong các giá trị | P3 |
| FR-48 | CORS không phản chiếu `Origin` tuỳ ý, không cho `null`, không dùng `*` kèm credentials; kiểm cả response thường và preflight | ACAO khác Origin gửi lên | P1 |
| FR-49 | HTTP thường phải chuyển sang HTTPS hoặc bị từ chối | 301/302/307/308 tới `https://`, hoặc 400/403/404/421/426, hoặc cổng đóng | P2 |
| FR-50 | Phương thức PUT/PATCH/DELETE không khai báo trong spec không chạy được khi chưa có token | 401/403/404/405/400 | P1 |
| FR-51 | TRACE, TRACK, CONNECT, PROPFIND, phương thức lạ bị từ chối; không phản chiếu header request | Không 2xx, không 5xx | P2 |
| FR-52 | Header đổi method (`X-HTTP-Method-Override`, `X-HTTP-Method`, `X-Method-Override`) không được tin khi chưa có token | 401/403/400/404/405 | P2 |
| FR-53 | File nhạy cảm và trang debug (`/.env`, `/.git/config`, `/actuator/env`, `/metrics`...) không công khai | Không trả 200 dạng không phải HTML; nội dung không chứa dấu hiệu bí mật | P1 |
| FR-54 | Response GET không chứa trường bí mật (`password`, `privateKey`, `mnemonic`, `secret`, `apiKey`...) | Không khớp mẫu trường bí mật có giá trị | P1 |
| FR-55 | Mọi thông báo lỗi không lộ stack trace, đường dẫn máy chủ, tên thư viện/DB | Không khớp mẫu rò rỉ | P1 |
| FR-56 | Endpoint dùng để thử JWT giả được chọn tự động: chỉ lấy GET thật sự trả 401/403 khi không token, tối đa 3, ưu tiên `/auth/me`, `/admin`, `/keyring`; ghi đè bằng `tests.jwt_probe` | Endpoint công khai thật hoặc kiểm tra tham số trước token bị loại | P1 |

### 5.5 Báo cáo và đầu ra

| Mã | Yêu cầu | Ưu tiên |
|---|---|---|
| FR-60 | In bảng kết quả theo nhóm (✓ đạt, ✗ lỗi, - bỏ qua) bằng tiếng Việt, mỗi nhóm có tên dễ hiểu | P1 |
| FR-61 | Với mỗi nhóm lỗi: gộp endpoint theo mã trả về và in gợi ý cách xử lý | P1 |
| FR-62 | Xuất `output/ket_qua.csv` (mở bằng Excel, hiển thị tiếng Việt đúng): nhóm, endpoint, kết quả, mong đợi, mã thực tế, ghi chú, lệnh curl đã gửi, tham số đã gửi, tham số theo spec, curl hợp lệ, Postman; dòng LỖI xếp trên cùng | P1 |
| FR-63 | Token thật không bao giờ ghi vào file kết quả (che thành `<token>`, curl dùng biến `$TOKEN`) | P1 |
| FR-64 | Xuất `output/report.html` (báo cáo kỹ thuật) và `output/postman_collection.json` (import là chạy, request ghi dữ liệu có cảnh báo) | P2 |
| FR-65 | Sinh bảng test case CSV từ spec cộng các case viết tay trong `config.json` (`python run.py cases`) | P2 |
| FR-66 | Liệt kê GET thực tế công khai dù spec khai báo cần token (`python run.py find-public`) để dev xác nhận có chủ đích hay là lỗ hổng | P2 |
| FR-67 | Quét lỗi 5xx bằng Schemathesis (`python run.py scan`) | P3 |
| FR-68 | Mỗi nhóm test mới đều có tên và gợi ý xử lý tiếng Việt trong bảng kết quả | P1 |

## 6. Yêu cầu phi chức năng

| Mã | Yêu cầu |
|---|---|
| NFR-01 | An toàn: không gửi thao tác ghi thật mặc định; payload tấn công chỉ gửi vào GET và hai endpoint đăng nhập; các request còn lại không token hoặc dùng body hỏng |
| NFR-02 | Bảo mật bí mật: `.env` nằm trong `.gitignore`; token không xuất hiện trong file kết quả |
| NFR-03 | Dùng được: người không biết lập trình chạy được bằng một lệnh; thông báo bằng tiếng Việt |
| NFR-04 | Ổn định: test lỗi mạng hoặc timeout (20 giây) báo rõ, không làm hỏng cả phiên chạy |
| NFR-05 | Thời gian: toàn bộ test bảo mật mất khoảng 7 phút trên môi trường test; có thể chạy riêng bằng `-k security` |
| NFR-06 | Bảo trì: thêm endpoint vào API không cần sửa test; chỉ chỉnh `config.json` khi cần |
| NFR-07 | Giảm báo sai: test vô nghĩa (token hết hạn, endpoint probe thực tế công khai) tự bỏ qua hoặc loại, kèm lý do |

## 7. Cấu hình

| Nơi | Khóa | Ý nghĩa |
|---|---|---|
| `.env` | `BASE_URL`, `AUTH_HEADER`, `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `TEST_PRIVATE_KEY`, `AUTH_SCHEME`, `USER_AUTH_HEADER`, `ALLOW_DESTRUCTIVE`, `RUN_RATE_LIMIT` | Môi trường, đăng nhập, bật test nguy hiểm |
| `config.json` | `risky`, `extra`, `field_samples`, `id_params` | Danh sách endpoint nguy hiểm, case viết tay, giá trị mẫu |
| `config.json` > `tests` | `public`, `skip`, `samples`, `auth_statuses`, `assume_protected`, `max_get_ms`, `admin_prefixes`, `jwt_probe`, `security_skip` | Tinh chỉnh các nhóm test |

## 8. Kết quả quan sát lần chạy đầu (môi trường test)

| Mã | Phát hiện | Mức độ | Việc cần làm |
|---|---|---|---|
| F-01 | CORS phản chiếu mọi `Origin` (kể cả `null` và tên miền giả có hậu tố). Không kèm credentials | Thấp đến trung bình | Dev dùng allowlist domain |
| F-02 | `GET /blacklist-token` và `GET /featured-earning-user` trả 200 khi không có token dù spec khai báo cần token | Cần xác nhận | Hỏi dev có công khai có chủ đích không; nếu có thì thêm vào `tests.public` |
| F-03 | `GET /exchange-history` kiểm tra tham số trước token (trả 400) | Thấp | Thêm giá trị đúng vào `tests.samples` hoặc đổi thứ tự kiểm tra |
| F-04 | `AUTH_HEADER` trong `.env` đã hết hạn nên 27 test sửa token và token-ở-query bị bỏ qua | Môi trường | Cấp token mới trước lần chạy tiếp |
| F-05 | Thiếu `USER_AUTH_HEADER` hoặc `TEST_PRIVATE_KEY` của ví không phải admin nên test phân quyền bị bỏ qua | Môi trường | Cấp ví user thường |
| F-06 | `GET /auth/sign` trả cùng nonce cho cùng địa chỉ trong một khoảng thời gian | Cần xác nhận | Hỏi dev nonce có hết hạn và dùng một lần không |

## 9. Tiêu chí nghiệm thu

1. `./run.sh` chạy hết không lỗi công cụ, in bảng kết quả và tạo `ket_qua.csv`, `report.html`, `postman_collection.json`.
2. Mỗi yêu cầu P1 ở mục 5 có ít nhất một test tương ứng và một dòng gợi ý xử lý trong bảng kết quả.
3. Không có token thật trong `output/`.
4. Chạy lại hai lần liên tiếp trên môi trường test không làm thay đổi dữ liệu (không tạo, sửa, xóa bản ghi).
5. Dev xác nhận các phát hiện F-01 đến F-06 là lỗi thật hoặc là thiết kế có chủ đích.

## 10. Rủi ro

| Rủi ro | Cách giảm |
|---|---|
| Test gửi payload tấn công làm API chậm hoặc khóa IP | Chỉ chạy trên môi trường test; test rate limit mặc định tắt |
| Báo sai do API kiểm tra dữ liệu trước token | `tests.samples`, `tests.public`, tự chọn probe thật sự cần token |
| Token hết hạn giữa chừng | Test sửa token tự bỏ qua kèm lý do |
| Spec không khớp thực tế (endpoint công khai nhưng khai báo cần token) | `python run.py find-public` |
| Chưa kiểm chứng định dạng response `/auth/sign`, `/auth/admin/login` với mọi ví | Chỉnh hàm `login()` trong `run.py` nếu khác |
