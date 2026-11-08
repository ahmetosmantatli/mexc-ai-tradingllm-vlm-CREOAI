# Attached market entry protection compatibility

`stopLossPrice` is a trigger level. `stopLossType` describes market (0) versus
limit (1) execution after triggering; it is not a contract-specific price.

MEXC documents that field but its current-order example omits it:
https://www.mexc.com/api-docs/futures/account-and-trading-endpoints/get-current-take-profitstop-loss-order-list
The standard create endpoint accepts attached SL/TP levels and trigger types,
without exposing a stop execution-type selection:
https://www.mexc.com/api-docs/futures/account-and-trading-endpoints/place-order

Compatibility inference, not an API guarantee: a missing `stopLossType` can
match the standard attached-market entry schema only when the local journal
records this submission schema, the reconciled exchange entry type is 5, and
the protection references that exact nonzero exchange order ID. It also must
have the correct position side and zero error code, no nonzero limit SL price,
and satisfy all existing active-state, reverse flag, trigger-price type,
quantity and exact SL/TP level checks. Explicit null or limit type is rejected.
This exception does not normalize the exchange field to 0 or apply to external
or standalone protection orders. The accepted evidence is journaled as
`protection_verification=attached_market_entry_schema`.

Order create requests use `type`, but query responses use `orderType`:
https://www.mexc.com/api-docs/futures/account-and-trading-endpoints/get-order-information-by-order-id
The response type is read from `orderType` (or a consistent legacy `type`).
Missing, null or conflicting values stay unknown, never default to the local
`entry_submitted_type`. Local submitted and exchange observed types are separate.

Mocks cover this response shape and conflicting variants, but cannot prove
actual future exchange stop execution. Emergency protection and persistent
entry halts remain enabled.

After reviewing and updating a stopped account, local acknowledgement is:

```
python -m trading.control.live_rearm --account friend --acknowledge-reviewed-emergency
```

This performs only exchange reads, then clears only the specific reviewed,
fully closed emergency halt if there are no exchange positions, entry orders,
protection orders or unresolved local intents. It never starts trading. A
different halt requires separate investigation. Use Telegram to start after
successful acknowledgement; no automatic release follows emergency closes.
