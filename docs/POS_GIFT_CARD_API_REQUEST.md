# POS gift-card API request for Việt

Status: proposed contract, not implemented by this patch. App Square/Apple Pay code is unchanged.

The current backend has app/customer gift-card balances and history, but the POS snapshot still returns `giftCards: []`. `GET /api/v1/app/me/gift-card` is a customer endpoint, not a balance lookup for the signed-in till. Existing POS `method: gift` payment records do not debit GiftCard.balance_cents. The frontend's `/gift-cards/v1/.../adjustments` routes are placeholders absent from this backend.

## 1. Read the selected customer's gift card

Proposed: `GET /api/v1/pos/customers/{customerId}/gift-card?cafe=braddon`.

Authentication: existing device/account credentials, `X-Terminal-Id`, `X-Pos-Staff`. Check staff assignment to this café and gift-redemption permission (`gift.redeem` or its parent `gift`).

Example response:

```json
{
  "id": "gift-card-uuid",
  "customerId": "customer-uuid",
  "last4": "1234",
  "balanceCents": 5000,
  "currency": "AUD"
}
```

Return 404 for an unknown/unavailable customer/card. Confirm the identification flow with the frontend: this first version attaches a known customer, then reads that customer's card. Four trailing digits are not a globally unique card identifier and must not authorize redemption by themselves. If anonymous physical cards/scanned passes are required, agree on a full unique card identifier or verified scan token first.

## 2. Redeem atomically inside the existing payment endpoint

Extend `POST /api/v1/pos/orders/{orderId}/payments`, rather than having the POS send a separate balance adjustment.

Example request, amount in cents:

```json
{
  "method": "gift",
  "giftCardId": "gift-card-uuid",
  "giftLast4": "1234",
  "amount": 1200,
  "tip": 0,
  "surcharge": 0,
  "ref": "stable-payment-reference"
}
```

Required behavior:

- Validate the verified PIN staff and café/terminal access, then lock the order and the identified gift card inside one database transaction.
- Validate the card belongs to the selected customer; look up authoritative balance, last four digits and status on the server. Do not trust cached balance or the client's staff name.
- Reject spending beyond the remaining order amount or card balance; disallow gift tips/surcharges unless a separate policy is agreed.
- Create the payment, debit the balance, and append GiftCardActivity together. A failure must roll back all three.
- A successful replay with the same payment `ref` / Idempotency-Key must not debit twice. Changed details under an existing reference return 409.
- Save an authoritative card reference on the payment, so refunds can credit the original card rather than matching last four digits.
- For a full or partial refund, credit the original card and append its activity in the same transaction as the order adjustment; replay must not credit twice.
- Keep split payments: the customer can spend the available gift balance and pay the remainder with another method. Two tills spending the same balance concurrently must not overspend it.
- Keep app checkout and till checkout using the same shared card balance and compatible row-lock ordering.

Suggested errors: 403 invalid staff/permission, 404 card not found, 409 insufficient/changed/stale payment state, 422 invalid amount or malformed request. Use the existing POS error envelope.

Until these APIs exist, the live POS in this patch does not record gift payments or apply cached gift balances. Demo gift cards still work. Issuing/top-ups are separate scope; no new POS issuance endpoint is assumed.

## Acceptance checks for the backend implementation

Read permissions and café scoping; balance 5000 minus payment 1200 equals 3800; exact replay leaves 3800; changed replay returns 409; insufficient balance changes neither payment nor history; split payment; concurrent redemption; partial/full refund and replay; app/till see the same new balance.
