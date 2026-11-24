#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Create the single variable-amount EUR Payment Link used by HifiMagic's
# optional footer Support link. Reads STRIPE_SECRET_KEY from the environment
# or from a local .env file; never prints the key.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$HERE/.env"

MODE="test"
if [[ "${1:-}" == "--live" ]]; then
  MODE="live"
elif [[ -n "${1:-}" ]]; then
  echo "Usage: $0 [--live]" >&2
  exit 2
fi

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

STRIPE_BIN="${STRIPE_BIN:-}"
if [[ -z "$STRIPE_BIN" ]]; then
  if command -v stripe >/dev/null 2>&1; then
    STRIPE_BIN="$(command -v stripe)"
  elif [[ -x "$HOME/.stripe/bin/stripe" ]]; then
    STRIPE_BIN="$HOME/.stripe/bin/stripe"
  else
    echo "Stripe CLI not found. Install it first (see README)." >&2
    exit 1
  fi
fi

KEY="${STRIPE_SECRET_KEY:-${STRIPE_API_KEY:-}}"
if [[ -z "$KEY" ]]; then
  echo "Set STRIPE_SECRET_KEY in $ENV_FILE or in the environment." >&2
  exit 1
fi

if [[ "$MODE" == "live" && "$KEY" != sk_live_* && "$KEY" != rk_live_* ]]; then
  echo "Refusing to create a live link with a non-live key." >&2
  exit 1
fi
if [[ "$MODE" == "test" && "$KEY" != sk_test_* && "$KEY" != rk_test_* ]]; then
  echo "Refusing to create a test link with a non-test key." >&2
  exit 1
fi

export STRIPE_API_KEY="$KEY"
LIVE_FLAG=()
KEY_SUFFIX=""
if [[ "$MODE" == "live" ]]; then
  LIVE_FLAG=(--live)
  KEY_SUFFIX="-live"
fi

product_json=$("$STRIPE_BIN" post /v1/products "${LIVE_FLAG[@]}" -c \
  -i "hifimagic-support-product-v1$KEY_SUFFIX" \
  -d 'name=HifiMagic support' \
  -d 'description=Optional one-time support for HifiMagic, an open-source local-first CXN100 controller.')
product_id=$(printf '%s' "$product_json" | python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])')

price_json=$("$STRIPE_BIN" post /v1/prices "${LIVE_FLAG[@]}" -c \
  -i "hifimagic-support-price-v1$KEY_SUFFIX" \
  -d currency=eur \
  -d "product=$product_id" \
  -d 'custom_unit_amount[enabled]=true' \
  -d 'custom_unit_amount[preset]=500' \
  -d 'custom_unit_amount[minimum]=100' \
  -d 'custom_unit_amount[maximum]=5000')
price_id=$(printf '%s' "$price_json" | python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])')

link_json=$("$STRIPE_BIN" post /v1/payment_links "${LIVE_FLAG[@]}" -c \
  -i "hifimagic-support-link-v3$KEY_SUFFIX" \
  -d "line_items[0][price]=$price_id" \
  -d 'line_items[0][quantity]=1' \
  -d 'managed_payments[enabled]=false')

printf '%s' "$link_json" | PRODUCT_ID="$product_id" PRICE_ID="$price_id" python3 -c '
import os, sys, json
d = json.load(sys.stdin)
if "url" not in d:
    raise SystemExit(d.get("error", d))
print("mode:", "live" if d.get("livemode") else "test")
print("product:", os.environ["PRODUCT_ID"])
print("price:", os.environ["PRICE_ID"])
print("payment link:", d["id"])
print("url:", d["url"])
'
