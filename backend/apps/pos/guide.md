The REST API behind the **EightyTwenty till** (repo `EightyTwenty-POS-Frontend-KDS`: front counter POS and
kitchen display). It implements the till's own seam, `src/services/contracts.ts` (`PosBackend`) and the
shapes in `src/domain/types.ts`. The endpoint paths in the till's `src/services/http/httpBackend.ts` were
proposals; **the paths on this page replace them** (section 2 maps every method to its path).

**How to read this page:** the guide below covers set-up and the rules shared by every call. Every endpoint
underneath has its own description, request/response schema, examples and errors. Use **Try it out**: run
`POST /api/v1/auth/login` with a café team account, copy `access`, press **Authorize** and paste it.

Other formats: [readable version](/api/docs/pos/redoc/) · [OpenAPI schema (YAML)](/api/docs/pos/schema/)

## 1. Connect the till

```bash
VITE_DATA_PROVIDER=http
VITE_API_BASE_URL=https://eightytwenty-api.onrender.com
VITE_TERMINAL_ID=BRADDON-TILL-1
```

- Every path starts with `/api/v1/pos` (plus `/api/v1/auth/login` and `/api/v1/auth/refresh`). No trailing
  slash. Local backend: `http://127.0.0.1:8000`.
- Send `X-Terminal-Id` on every request (the till already does). It identifies the till's cash shift and is
  stored on every order, payment and record.
- CORS is open to every origin (`Access-Control-Allow-Origin: *`) and any host name is accepted, so the
  till can call the API from `localhost`, a LAN IP or any preview domain without asking first.

### Two sign-ins: the device, then the person

1. **The device** signs in once with a café team account (email + password of a café owner or staff
   member, or a chain-wide account), exactly like the till's `src/services/http/authSession.ts`:
   `POST /api/v1/auth/login {email, password}` → `{access, refresh}`. Send `Authorization: Bearer <access>`
   on every call. The account decides **which cafés** the till can trade for (`GET /api/v1/pos/locations`).
2. **The person at the till** signs in with a 4-digit PIN: `POST /api/v1/pos/staff/verify-pin {pin}` →
   `{staff, permissions, staffToken, staffTokenExpiresIn}`, or `204` when the PIN is not recognised. PINs
   are set in the BOS workspace (People → Users → Till PIN), unique across the chain.

**Tokens:** `access` lives 15 minutes, `refresh` 30 days and is single use — every
`POST /api/v1/auth/refresh {refresh}` returns a new pair. **Refresh before the access token expires, and on
any `401` refresh and resend the same command with the same Idempotency-Key.** The outbox treats `401` as a
rejection, so without this a command queued over an expired token would be moved to `rejected`. Keep the
refresh token in device storage so a reload does not ask for the password again.

**Server-side permissions:** the server checks the device account (role and cafés) on every call. Money and
card commands also need the person: send `X-POS-Staff: <staffToken>` (valid 12 hours, for the same device
account and `X-Terminal-Id`; a changed PIN ends it). Without a valid token they answer `403`
`STAFF_SESSION_REQUIRED` / `STAFF_SESSION_MISMATCH`, or `FORBIDDEN` when the person lacks the permission:
payments and orders created with payments (`pos`), refunds/voids (`orders.refund`), opening a shift (`pos`),
cash moves and closing a shift (`cash.out`), editing a customer (`customers`), stamp/redeem
(`loyalty.stamp`). Other commands record who did what from the names in the payload (`by`, `approver`,
`staff`).

## 2. `PosBackend` → endpoints

| Till method | Endpoint | Status |
|---|---|---|
| `loadSnapshot()` | `GET /api/v1/pos/snapshot?cafe={cafe}` | ✅ (send the till's saved café) |
| `orders.listChanged(cafe, since)` | `GET /api/v1/pos/orders?cafe={cafe}&since={cursor}` | ✅ app orders included |
| `orders.create(order)` | `POST /api/v1/pos/orders` | ✅ |
| `orders.setStatus(id, status, note)` | `POST /api/v1/pos/orders/{id}/status` | ✅ |
| `orders.accept(id)` | `POST /api/v1/pos/orders/{id}/accept` | ✅ |
| `orders.setPrep(id, change)` | `PUT /api/v1/pos/orders/{id}/preparation` | ✅ |
| `orders.adjust(id, change)` | `POST /api/v1/pos/orders/{id}/adjustments` | ✅ |
| `orders.setTables(id, tables)` | `PUT /api/v1/pos/orders/{id}/tables` | ✅ |
| `payments.record(id, payment)` | `POST /api/v1/pos/orders/{id}/payments` | ✅ |
| `records.openShift(shift)` | `POST /api/v1/pos/shifts` | ✅ |
| `records.recordCashMove(shiftId, move)` | `POST /api/v1/pos/shifts/{shiftId}/moves` | ✅ |
| `records.closeShift(shift)` | `PUT /api/v1/pos/shifts/{shiftId}` | ✅ |
| `records.submit(record)` | `POST /api/v1/pos/transactions` | ✅ |
| `records.appendAudit(entry)` | `POST /api/v1/pos/audit` | ✅ |
| `staffAuth.verifyPin(pin)` | `POST /api/v1/pos/staff/verify-pin` | ✅ `204` = not recognised → `null` |
| `#backend-check`: locations, categories, menu | `GET /api/v1/pos/locations`, `…/locations/{slug}/categories`, `…/locations/{slug}/menu` | ✅ same shapes as `posCatalog.ts` / `posMapping.ts` |
| `customers.update(id, patch)` | `PUT /api/v1/pos/customers/{id}` | ✅ app customer accounts |
| (read the card now) | `GET /api/v1/pos/customers/{id}/loyalty` | ✅ `{id, stamps, rewards}` (needs `X-POS-Staff`) |
| `loyalty.setBalance(id, balance, reason)` | `PUT /api/v1/pos/customers/{id}/loyalty` | ✅ same card as the app (section 5) |
| `catalog.setSoldOut(itemId, soldOut)` | `PUT /api/v1/pos/items/{itemId}/sold-out` | ✅ every café of this account |
| `catalog.setBranchAvailability(cafe, itemId, available)` | `PUT /api/v1/pos/locations/{cafe}/items/{itemId}/availability` | ✅ |
| `reservations.upsert(r)` | `PUT /api/v1/pos/reservations/{id}` | ✅ same bookings as website/app/BOS |
| `catering.upsert(e)` | `PUT /api/v1/pos/catering/{id}` | ✅ same enquiries as website/app/BOS |
| `catalog.setPrice(itemId, price)` | `PUT /api/v1/pos/locations/{cafe}/items/{itemId}/price` | ✅ chain owner/admin only; this café's price |
| `catalog.setModifierAvailability(label, available)` | `PUT /api/v1/pos/locations/{cafe}/modifiers/availability` | ✅ manager+; hidden from the app's menu at this café |
| `catalog.setCategoryOrder(ids)` | `PUT /api/v1/pos/locations/{cafe}/categories/order` | ✅ manager+; till only |
| `settings.update(patch)` | `PUT /api/v1/pos/locations/{cafe}/settings/toggles` | ✅ chain owner/admin only |
| `loyalty.applyAction(id, cafe, action)` | `POST /api/v1/pos/customers/{id}/loyalty/actions` | ✅ `{cafe, action: "stamp"\|"redeem"}` |
| `adjustStock` | — | ⏳ `404`: no stock yet |
| `loyalty.updateSettings` | — | ⏳ `404`: the card rule (9 stamps, 10th free) is fixed, shared with the app |
| (read a customer's gift card) | `GET /api/v1/pos/customers/{id}/gift-card` | ✅ `{id, customerId, last4, balance}` (cents, needs `X-POS-Staff`) |
| `payments.record` with `method: gift` | `POST /api/v1/pos/orders/{id}/payments` | ✅ send `giftCardId`; the server takes it off the balance (section 5) |
| `giftCards.issue`, `giftCards.adjustBalance` | — | ⏳ selling cards needs the gift card provider (Square) — `404`; the chain owner tops up in BOS |
| `staffAuth.createStaff`, `setRole`, `setRoleMatrix` | — | ⏳ `404`: people and PINs are managed in BOS |
| `records.requestTaxInvoice` | — | ❌ no invoicing service yet (`404`) |

A command without a route answers `404 NOT_FOUND`, which the outbox moves to `rejected`. The adapter should
**not send** the ⏳ commands (resolve them locally) so the rejected list stays meaningful.

## 3. Conventions

- **Ids:** cafés, menu items, categories and modifier groups use the website slugs the till already knows
  (`braddon`, `mighty`, `drink`). Staff ids are UUIDs. Orders are identified by their number (`id`).
- **Order numbers must be unique across all tills and the app.** The till currently issues
  `#8020-<n>` from a per-device counter, so two tills (or a till and an app order, which use `#8020-NNNN`)
  can collide: the second `POST /orders` with an existing id answers `409 CONFLICT`. Prefix the number with
  the terminal, e.g. `BRADDON-TILL-1-0042` (≤ 40 characters). URL-encode the id in paths
  (`encodeURIComponent`, `#` → `%23`).
- **Money:** as in `types.ts` — orders, lines, discounts and catering prices in **AUD dollars** (numbers);
  payments, refunds, shifts, cash moves and transaction records in **integer cents**. Order `total` excludes
  the card surcharge, which travels on the payment.
- **Idempotency:** every write takes an `Idempotency-Key` header (the outbox entry id). A retry with the same
  key and body returns the first answer without doing the work again; the same key with a different body →
  `409 CONFLICT`. Payments are also idempotent on `ref`, adjustments on `key`, cash moves on `id`, records on
  café + kind + reference, audit entries on `id`.
- **Times:** ISO timestamps with the café offset (`2026-10-16T09:41:12+11:00`). Human labels the till makes
  (`at: "16 Oct, 9:41 am"`, `openedAt`) are stored as given.
- **Responses:** writes on an order answer `200`/`201` with the order as stored (same shape as the feed);
  shift writes answer the shift; records and audit answer `{"ok": true}`. The till may ignore the bodies.

## 4. Errors

Same envelope as the rest of the API: `{"error": {"code", "message", "fields"}}`. `message` is written for
staff and can be shown as is. The HTTP status drives the till's `ApiError.kind`:

| HTTP | `code` | Meaning | Outbox |
|---|---|---|---|
| 401 | `UNAUTHORIZED` | Access token missing/expired | refresh, then resend (section 1) |
| 403 | `FORBIDDEN` | Not a café team account / no café assigned | rejected |
| 404 | `NOT_FOUND` | Café not on this account, unknown order/shift, command not built yet | rejected |
| 409 | `CONFLICT` | Number already used, order already finished, key reused | rejected |
| 422 | `VALIDATION` | Wrong shape or numbers that do not add up (`fields` names the field) | rejected |
| 429 | `TOO_MANY_REQUESTS` | Too many PIN checks (the only limited call) | wait (`Retry-After`) |
| 5xx | `SERVER` | Server problem | retried with the same key |

## 5. Orders: what the server checks and does

- **Till orders are recorded even when prices changed.** The till trades on the menu it loaded at start-up;
  when a price changed in BOS since then, or an item was sold out, the sale has still happened. The server
  checks that the order adds up (Σ price × qty − discounts + delivery = `total`, to the cent) and compares every
  line with the café's current menu; differences are listed on the order for a manager (BOS → Orders). Load the
  snapshot again at the start of each day so the till trades on current prices.
- **App orders show on the till** with `channel: "app"`, `accepted: false` until the café accepts them,
  `paid: true` (paid in the app; the till sees one card/Apple Pay payment and, if used, one gift card
  payment), contact and delivery address in `note`, the free coffee as a discount. Accepting, kitchen
  progress, ready and completed are sent to the customer's app feed. Cancelling or fully refunding an app
  order through `/adjustments` (as the till sends it: `amount` = what was paid, `total` unchanged) returns
  its payment, stamps and gift card spend; the app order keeps its own total. App orders come with
  `stamped: false` so the till leaves their coffee card alone (the server adds and returns their stamps).
- **Status map:** the server keeps one status per order; the till sees `new` (and `accepted`) for orders that
  are confirmed or waiting, then `preparing`, `ready`, `completed`, `cancelled`, `refunded`, `failed`.
- **Finished orders** (completed, cancelled, refunded) accept no more status or kitchen changes (`409`); a
  completed order can still be refunded.
- **Customers** are the app's customer accounts (one list for the chain, `customers` in the snapshot, newest
  2,000). `customerId` on an order, reservation or enquiry links it when it is a customer's id. The till cannot
  create customers; people sign up in the app.
- **Coffee card = the app's card.** One stamp per coffee; every 9 stamps is a free coffee and free coffees add
  up until the customer uses them (one per order). The till sees `stamps` 0–8 (towards the next one) and
  `rewards` (free coffees ready, 0 or more). `GET /customers/{id}/loyalty` reads the card as it is now;
  `PUT /customers/{id}/loyalty` sets it (`rewards × 9 + stamps`). Orders sent with `serverLoyalty: true` get
  their stamps (and the free coffee they use) from the server once fully paid, and back on a refund — the
  till must not also send `loyalty.balance` for them. Without it, the till's own `loyalty.balance` command
  after payment does it.
- **Gift card = the app's gift card** (one per customer, topped up in BOS, same balance in the app).
  `GET /customers/{id}/gift-card` gives its `id` and `balance` (cents). A payment with `method: gift` and
  `giftCardId` takes `amount` off that card in the same transaction (a repeated `ref` is not taken twice;
  more than the balance → `409`, nothing taken). A refund, void or cancellation allocated to that payment
  (`alloc` row with its `ref`) puts the money back on the same card once per adjustment `key`; a full refund
  without `alloc` returns what is left of every gift card payment, a partial one needs `alloc` (`422`).
- **Sold out:** the snapshot's `availability.soldOut` lists items sold out at every café of this account,
  `branchOff[cafe]` those sold out only here. Changes reach the website, the app and BOS at once.

- **Tables and promo codes** come from BOS: café owners set their floor plan (BOS → Locations → Tables) and
  the chain owner the promo codes (BOS → Daily operations → Promo codes; a percentage or a dollar amount, for every café or
  one, with optional dates). The snapshot returns this café's active tables and the codes live today. The code
  is checked on the till; the order carries the discount as usual (`discounts`), ideally with the code in the
  optional `code` field (otherwise the server looks for a known code in `label`). The server checks it again
  when the order is created: an unknown, switched-off, expired or other-café code, or more off than the code
  allows, is **noted on the order for a manager** (BOS → Orders → Check prices) — the order is never refused.

## 6. Cash shifts and records

- One open shift per till (`X-Terminal-Id`). The snapshot returns this till's open shift as `currentShift`,
  so a till that reloads mid-shift carries on. A shift is closed and moved only from the till that opened it.
- Cash payments and refunds are tied to the till's open shift. Closing checks `expected` = float + cash
  payments − cash refunds of this shift + cash in − cash out and `variance` = counted − expected.
- `POST /transactions` keeps the till's `TransactionRecord` (append-only); `POST /audit` keeps its
  `AuditEntry` in the BOS activity log. The snapshot returns the last 100 audit entries for the café.

## 7. Not connected (by design, for now)

Card terminal, printers and cash drawer stay on the device (`DeviceHub`); the server never moves money. Gift
cards (Square), stock, Apple Wallet and tax invoices are not built; the snapshot returns empty lists for them
(`giftCards`, `stock`, `stockLog`).

With `unconnectedDevices` the till refuses card and wallet tenders. Until a terminal SDK is integrated, a café
using a standalone EFTPOS machine needs a "card taken on the EFTPOS machine" tender in the till that records
`method: "card"` with the approved amount; the server accepts it like any other payment.
