# Đưa app lên TestFlight và App Store

Cập nhật 27/10/2026 (`main`). Ký hiệu: ✅ xong và đã kiểm · ⛔ chặn bản công khai trên App Store · ⚠️ nên làm. Nội dung để điền App Store Connect: [`APP_STORE_LISTING.md`](APP_STORE_LISTING.md).

## Tóm tắt

- **TestFlight (test nội bộ): làm được ngay.** Mọi thứ chạy với API production, trừ trả tiền bằng thẻ/Apple Pay (app báo "Card payments aren't available" vì chưa có Square). Đặt món trả bằng gift card vẫn chạy.
- **App Store (công khai): còn chặn bởi Square** và vài việc nhập liệu (tài khoản demo, ảnh chụp màn hình).

## Đã xong

- ✅ **Build**: `eas.json` profile `production` gắn API production (`https://eightytwenty-api.onrender.com/api/v1/app`). Bản phát hành **không** dùng `mobile/.env` (gitignore, EAS không tải lên). Số build tự tăng trên EAS (`appVersionSource: remote`). Project EAS `@my-an-lien/eightytwenty`.
- ✅ **`app.json`**: tên `EightyTwenty`, bundle id `au.com.eightytwentyfood.app`, chỉ iPhone, `ITSAppUsesNonExemptEncryption = false`, câu xin quyền Face ID và vị trí, plugin push, icon 1024×1024 không trong suốt.
- ✅ **Backend production** có đủ API của app: xoá tài khoản, đổi mật khẩu, feed có link, Wallet, toạ độ quán, nhắc lịch đặt bàn (job), Square (chưa bật).
- ✅ **Xoá tài khoản trong app** (guideline 5.1.1(v)): Privacy & security → Close my account. Không có đăng nhập Google/Facebook nên không bắt buộc Sign in with Apple.
- ✅ **Push** chạy thật trên iPhone; bấm push / dòng Notifications / đơn trong Activity đều mở trạng thái đơn; Home có viên "Track" khi có đơn đang làm.
- ✅ **Face ID**: bật thì mở app phải quét; có "Use password instead".
- ✅ **Apple Wallet** chạy trên production và đã thử trên iPhone (27/10): thêm thẻ (trang trong app, có "Back to the app"), thẻ có dải dấu, tự cập nhật khi tích dấu ở BOS. Pass Type ID `pass.au.com.eightytwentyfood.loyalty`, chứng chỉ **hết hạn 26/11/2027** (gia hạn: `docs/OPERATIONS.md`).
- ✅ **"Use my location"**: toạ độ 9 quán trên production, quán gần nhất lên đầu, vị trí chỉ dùng trên máy.
- ✅ **Privacy policy + Terms of use** đã đăng: `https://eightytwenty.pages.dev/privacy`, `/terms` (trang CMS, sửa trong BOS → Website → Pages). Nội dung do dev soạn — khách cần đọc duyệt.
- ✅ **Email** gửi qua Brevo trên production.
- ✅ **Ảnh Keep Cups** cho offer đã lên Supabase Storage và gán vào offer (27/10). Đổi ảnh: BOS → App offers → Upload a new photo.
- ✅ **Tài khoản demo** cho Apple review trên production: `eightytwenty-review@gmail.com` (6 dấu, $25 gift card). Mật khẩu đã bị đổi trong BOS — điền mật khẩu mới vào cuối `backend/.env.production`.
- ✅ **QA**: `make qa` (backend + app, bundle iOS/Android), `make api-smoke`, GitHub CI chạy backend (Postgres 17) và app.
- ✅ **Đang ẩn** khi chạy API thật (mock của khách vẫn hiện): gửi eGift, "Add a card" (lưu thẻ). Bật bằng `EXPO_PUBLIC_GIFT_CARD_SALES` / `EXPO_PUBLIC_SAVED_CARDS` = `true` khi server làm xong.

## ⛔ Còn chặn bản công khai

1. **Thanh toán Square** — code xong; **đang thử Sandbox** (28/10/2026) bằng tài khoản Square developer cá nhân của chủ dự án: SDK Square đã gắn lại vào bản iOS (sửa lỗi build 6 bằng `plugins/withSquareSetupAfterEmbed.js`), `eas.json` có Application ID sandbox, Render bật `PAYMENT_PROVIDER=square` + sandbox. Chạy ổn thì xin tài khoản Square + Apple Pay của khách và thay mã (`docs/SQUARE_SETUP.md`). Expo Go không chạy Square — thử bằng TestFlight. Đặt món mà không trả được tiền dễ bị Apple từ chối (guideline 2.1).
2. **Mật khẩu tài khoản demo** để điền App Review Information.
3. **Ảnh chụp màn hình** iPhone 6.9" (1320×2868 hoặc 1290×2796), 3–10 ảnh, chụp từ bản TestFlight bằng tài khoản demo (thứ tự gợi ý trong `APP_STORE_LISTING.md`).
4. **Điền App Store Connect** theo `APP_STORE_LISTING.md` (mô tả, từ khoá, App Privacy, ghi chú cho người duyệt).

## ⚠️ Nên làm

- ✅ **Job định kỳ** (nhắc lịch đặt bàn, gửi lại email lỗi, dọn dẹp) chạy trong server mỗi 15 phút từ 27/10 (`docs/OPERATIONS.md`).
- Báo các quán có thể có đơn thử từ Apple trong lúc duyệt.
- **Android / Google Play**: chưa làm (push Android cần FCM).

## Lên TestFlight

**Bản mới nhất: 1.0.0 (12), 31/10/2026** (`b8b4c82`: logo thống nhất, ẩn Akuna, bản vá Expo). **Đã lên TestFlight lần đầu (27/10/2026):** build EAS `7cbedcef-…` (1.0.0, build 6) tải lên App Store Connect, app `EightyTwenty` (Apple ID app `6827542559`, SKU `eightytwenty-ios`, en-AU, ghi trong `eas.json` → `submit.production.ios.ascAppId`). Cách đã làm, không dùng Apple ID (đăng nhập Google, không nhớ mật khẩu):

- **Khóa App Store Connect API** (team key, quyền Admin, tạo trên web): file `D:\Data\secrets\eightytwenty-secrets\apple\AuthKey_3VFQP9H8X5.p8` (ngoài repo); Key ID / Issuer ID / team ghi trong `mobile/.env.asc` (gitignored, máy này). Máy khác: tạo lại file này cùng các biến `EXPO_ASC_API_KEY_PATH`, `EXPO_ASC_KEY_ID`, `EXPO_ASC_ISSUER_ID`, `EXPO_APPLE_TEAM_ID=988D5VKVXS`, `EXPO_APPLE_TEAM_TYPE=INDIVIDUAL`.
- **Credentials trên EAS** (project `@my-an-lien/eightytwenty`): bundle id `au.com.eightytwentyfood.app` (EAS đăng ký); quyền **Push Notifications** bật bằng API của Apple (bước "Syncing capabilities" của EAS lỗi với API key → build dùng `EXPO_NO_CAPABILITY_SYNC=1`); **Distribution Certificate dùng chung với dự án Soya** (cùng team, hết hạn 16/10/2027); provisioning profile mới; **push key dùng chung của team** `QUYKTFXZ95` (tạo push key mới bắt buộc đăng nhập Apple ID).
- **App trên App Store Connect phải tạo tay trên web** (API của Apple không cho tạo app).

**Bản build mới sau này** (từ thư mục gốc, không hỏi gì):

```bash
make ios-build
```

```bash
make ios-submit
```

`ios-build` gửi lên EAS rồi thoát (`--no-wait`, ~15 phút, xem trên expo.dev); xong thì `ios-submit` tải bản mới nhất lên (khóa API được ghi tạm vào `eas.json` trong lúc chạy rồi trả lại). Số build tự tăng. Apple xử lý 5–30 phút, có email.

**Người test:** App Store Connect → app → **TestFlight** → **Internal Testing** → nhóm → thêm người test (phải là thành viên team trên App Store Connect) → họ cài app **TestFlight** trên iPhone và nhận lời mời. Test nội bộ không cần Apple duyệt; test ngoài (External, bất kỳ email) cần một lần Beta Review. Thử nhanh không cần build: `make mobile-prod` (Expo Go, API production).
