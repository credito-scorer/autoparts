# Webhook Routing Contract

This document defines the routing precedence in `app.py` for inbound WhatsApp messages.
When debugging "wrong flow" issues, use this as the source of truth.

## Goals

- Keep **regular users** on the autoparts assistant flow.
- Keep **beta users** isolated to their configured beta flow.
- Avoid ambiguous behavior when a number exists in multiple lists.

## Precedence Order (Text Messages)

Inbound text messages are routed in this effective order:

1. `SELLER` numbers (`is_seller`)
2. `CUSTOMER_BETA_NUMBERS` (`is_customer_beta`)  
   - Live relay style beta (owner-in-the-loop)
3. Owner number (`YOUR_PERSONAL_WHATSAPP`)
4. Registered supplier numbers (`WHATSAPP_SUPPLIERS`)
5. Local store numbers (Sheets stores registry)
6. `BETA_WHITELIST_NUMBERS` (`is_beta_user` in `beta_discovery.py`)  
   - Discovery interview style beta (Claude signal capture)
7. Pending live offer path
8. Active live session path
9. Pending selection / quote / urgency / confirming states
10. Regular customer assistant flow (`process_customer_request`)

## Two Beta Types (Important)

There are currently two independent beta channels:

- **Customer Beta (Live Relay):** `CUSTOMER_BETA_NUMBERS`
  - Customer is put in live relay mode with owner.
- **Discovery Beta (Research Interview):** `BETA_WHITELIST_NUMBERS`
  - Customer is handled by `beta_discovery.py`.

If a number is in **both** lists, current precedence means **Customer Beta (Live Relay) wins**.

## Number Normalization Rules

Both beta checks normalize phone numbers before matching:

- strips `whatsapp:`
- strips leading `+`
- strips spaces and `-`

Both env vars support separators:

- comma `,`
- newline
- semicolon `;`

Examples that match the same number:

- `+50763622248`
- `50763622248`
- `whatsapp:+50763622248`

## Config Contract

- `CUSTOMER_BETA_NUMBERS`: live relay beta
- `BETA_WHITELIST_NUMBERS`: discovery beta
- Keep these lists intentionally separate unless overlap is desired.

## Regular User Guarantee

A user not matching seller/owner/supplier/store/beta routes will always proceed to the regular autoparts workflow.
If a "regular" user gets beta behavior, check env list membership and normalization first.
