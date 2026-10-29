# Trạng thái triển khai — 20/10/2026

Backend (Django, `backend/`) đang chạy production trên Render (https://eightytwenty-api.onrender.com) + Supabase (Postgres + Storage). Đã có: BOS workspace (Django Admin ở `/`, 3 cấp quyền), API cho website (`/api/v1/public`), app điện thoại (`/api/v1/app`), máy POS + màn hình bếp (`/api/v1/pos`), báo cáo doanh thu trong BOS và push Expo. Định hướng của chủ dự án: chạy được trên Render free là đủ, làm cách đơn giản nhất.

## Lần kiểm tra gần nhất

- 20/10/2026, SQLite: **260 pass, 3 skip**. Ba test cần PostgreSQL chỉ chạy trên CI (GitHub Actions, Postgres 17), mọi commit gần đây đều xanh. Lần chạy Postgres ở máy gần nhất là 12/10/2026 (195 pass, Docker ở máy hiện không lên).
- Ruff check/format, `manage.py check`, kiểm tra migration drift, OpenAPI validate (CI fail nếu `docs/openapi.yaml` lệch code) đều qua.
- Production sau mỗi lần deploy: `/health`, bootstrap (10 quán / 12 danh mục / 84 món / 3 gói catering), `/api/docs/app/`, `/api/docs/pos/` trả 200; các trang admin chuyển về đăng nhập.

## Đã kiểm thử

- **Website:** bootstrap đúng shape `mock-data.js` (ETag theo `menu_version`), form đặt bàn + enquiry (honeypot, rate limit DB, email outbox). Deploy Cloudflare Pages https://eightytwenty.pages.dev (21/10/2026). Đăng ký / đăng nhập / hồ sơ / đăng xuất dùng `/api/v1/auth` + `/api/v1/me` (`website/account.js`, `scripts/test-frontend.mjs`); tab Reservations đọc `/api/v1/me/bookings`, đặt bàn khi đã đăng nhập gắn vào tài khoản; CMS BOS hiện trên website (`website/cms.js`): announcement, footer/social/contact, navigation header+footer, SEO theo page, 4 trang legal lấy body từ page CMS có block, trang CMS khác mở theo `/slug`; hero xoay trang chủ (5 slot: headline/ảnh/tagline) + đoạn intro/CTA từ page `home` (block `hero-1…5`, `intro`, `intro-cta`), chuyển động giữ nguyên; quên mật khẩu trên website đã nối API (gửi mail cần `EMAIL_PROVIDER_API_KEY` trên server), đặt món trên website vẫn tạm dừng. **Đặt bàn từ website vào thẳng BOS** (22/10/2026): mọi quán `open` nhận yêu cầu `requested` dù BookingRule tắt (rule bật = thêm kiểm slot + sức chứa); website hiện lỗi API dưới form (quán đóng cửa ngày đó, thiếu 12 giờ báo trước, nhóm quá lớn) thay vì mở email draft; chỉ khi API không trả lời mới mở email draft như cũ; nút ghi "Request a table" khi bootstrap đến từ API. Catering/wholesale (22/10/2026): lỗi API hiện dưới form (không mở email draft), catering gửi `payload` occasion/eventLocation; thành công chỉ khi API trả `reference`.
- **BOS workspace:** phân quyền 3 cấp chủ chuỗi / chủ quán / nhân viên theo quán (`tests/test_bos_roles.py`), dashboard, Reports (doanh thu, CSV), Schedule, Availability board, System status, My profile, upload ảnh WebP lên Supabase (mock trong test).
  - Trang chi tiết Order / Booking / Enquiry dạng tóm tắt; khối tổng quan khách hàng (`tests/test_record_pages.py`).
  - Quản lý tài khoản theo quán (22/10/2026): Team by café, filter Role/Café trên Team, tóm tắt tài khoản team (`tests/test_team_pages.py`). Chỉ UI; quyền server giữ nguyên — xem `docs/BRANCH_ACCESS_HANDOFF.md` cho phần backend cần xác minh.
  - Danh sách: bấm cả dòng để mở; Bookings mặc định hôm nay + sắp tới; tách Team và Customers; Coffee cards có nút +1 stamp / Redeem; Orders có cột món; Overview đếm đơn app đang chờ (`tests/test_admin_lists.py`).
- **API app:** 41 path theo hợp đồng app Expo; giá tính khớp từng cent với app (`compute_totals`); không N+1 (đếm truy vấn); ẩn danh hóa; push Expo (mock).
- **API POS:** PIN, snapshot, đơn quầy + idempotency, thanh toán, bếp, hoàn/void/hủy, ca két, sổ giao dịch, khách hàng + thẻ cà phê, còn/hết món, đặt bàn, catering, bàn, mã giảm giá. Mã giảm giá được server kiểm lại khi tạo đơn: sai/hết hạn/sai quán/giảm quá mức thì ghi chú "Check prices", không từ chối đơn. Từ 26/10/2026 (code của Ân): phiên nhân viên `X-POS-Staff` cho lệnh tiền/thẻ, thanh toán/hoàn gắn ca két, giá riêng/thứ tự danh mục/tắt modifier/toggle theo quán, nút +1 stamp / redeem từ máy POS. Từ 29/10/2026: ly cà phê free cộng dồn (9 dấu = 1 ly, mỗi đơn dùng 1 ly) ở BOS, app, Apple Wallet và POS; đơn quầy gửi `serverLoyalty: true` được server tự cộng dấu khi trả đủ và trả lại khi hoàn tiền (frontend POS phải bật cờ này và không gửi `loyalty.balance` cho các đơn đó).
- **PostgreSQL (CI):** khóa slot đặt bàn khi tranh nhau, refresh token dùng một lần, RLS bật trên mọi bảng.
- **Dữ liệu dev:** `seed_demo` dựng DB SQLite local có dữ liệu mẫu và từ chối mọi DB không phải local (`tests/test_seed_demo.py`).

## Chưa chạy với hệ thống thật

- App Expo (nay ở `mobile/`, SDK 58): 26/10/2026 đã chạy end-to-end bản web của app với backend local (đăng ký/đăng nhập, menu, đặt món demo → café đổi trạng thái → app cập nhật, đặt bàn, catering, thẻ cà phê, feed) và đọc dữ liệu production; chủ dự án đã chạy được trên Expo Go và đăng nhập. Đã thêm xóa tài khoản, đổi mật khẩu, push Expo (cần `eas init` để có projectId; Android cần development build), barcode thật cho thẻ cà phê. Frontend POS thật: dev khác đang nối, các luồng đã test bằng client giả theo hợp đồng.
- Resend (email) và Expo push mới được mock. Email cần domain có SPF/DKIM (ARCHITECTURE mục 14, câu 10).
- Production chưa có dữ liệu vận hành, chỉ có menu và quán. Chưa có superuser (chủ dự án tự tạo), chưa ai đặt Till PIN.
- Job định kỳ chạy trong server từ 27/10/2026 (`apps/core/jobs.py`, không dùng GitHub Actions). Backup (`db-backup.yml`) chưa được xác nhận là chạy được trên production.

## Chưa làm (chờ khách hoặc bên thứ ba)

- Cổng thanh toán: đơn app có tiền phải trả → 501 (`PAYMENT_PROVIDER=demo` chỉ dùng để thử). Chưa có thẻ lưu, chưa gửi eGift thật.
- Máy quẹt thẻ và máy in nối POS, bán gift card qua Square (dùng gift card có sẵn của khách ở quầy đã có API từ 29/10/2026: `GET /pos/customers/{id}/gift-card`, thanh toán `method: gift` + `giftCardId`, hoàn về đúng thẻ), kho, hóa đơn điện tử. (Apple Wallet đã phát hành thẻ trên production từ 27/10/2026.)
- Bật `BookingRule.is_enabled` cho từng quán = bật kiểm sức chứa/slot online (cần khách xác nhận `max_covers_per_slot`, `max_party_size`, `min_lead_minutes`); yêu cầu đặt bàn đã vào BOS từ 22/10/2026 không cần bật. Quán Akuna St vẫn TBC (`status=tbc` → `bookings_disabled`). Email thật cần `EMAIL_PROVIDER_API_KEY` + `EMAIL_FROM` trên Render.
- Website hiển thị món hết hàng theo quán: backend đã có (`?location=`), giao diện chờ khách duyệt.
- React BOS: chưa làm, Django Admin đang là BOS.

## Giới hạn có chủ đích

- Không hard delete dữ liệu kinh doanh: dùng khóa / bỏ publish / trạng thái. Admin không có nút xóa.
- Thời gian đặt bàn không kéo qua ngày hoặc chiếm nhiều slot. Hai form public chưa có idempotency key.
- Auth dùng bearer JSON, chưa có cookie HttpOnly. Khách chưa verify email vẫn đăng nhập được.
- Đơn quầy: server kiểm tổng tiền (lệch ≤ 1¢), giá lệch menu và mã giảm giá chỉ được ghi chú, không từ chối. Server kiểm quyền từng nhân viên (qua `X-POS-Staff`) chỉ ở lệnh tiền/thẻ; lệnh khác chỉ kiểm tài khoản máy.
- Thay đổi catalog phải đi qua service/save để tăng `menu_version`. SQL tay hoặc bulk update không được bảo đảm làm mới cache.
- Ẩn danh hóa khách xóa PII trong user/profile, booking, đơn app, địa chỉ, feed. Audit do nhân viên tạo vẫn giữ lại.
- Theo yêu cầu chủ dự án (16/10/2026): host + CORS mở hoàn toàn, không thêm lớp bảo mật nếu không cần.
