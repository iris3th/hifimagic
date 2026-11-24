#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Run the HifiMagic AppImage with the Stripe test support link configured.
# Downloads the release AppImage if it is not already next to this script.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION="${VERSION:-v$(cat "$SCRIPT_DIR/VERSION" 2>/dev/null || echo 0.0.0)}"
RELEASE_URL="https://github.com/iris3th/hifimagic/releases/download/${VERSION}"
ASSET="HifiMagic-${VERSION#v}-x86_64.AppImage"
APPIMAGE="${APPIMAGE:-${SCRIPT_DIR}/${ASSET}}"

PLAYER_HOST="${PLAYER_HOST:-192.168.49.156}"
PORT="${PORT:-8080}"
SUPPORT_URL="${SUPPORT_URL:-https://buy.stripe.com/test_6oU4gtaH1grI4FJbVv1B600}"

if [[ ! -x "$APPIMAGE" ]]; then
  echo "Downloading ${ASSET} from ${VERSION}..."
  TMP="$(mktemp "${TMPDIR:-/tmp}/hifimagic-appimage.XXXXXX")"
  SUM_FILE="${TMP}.sha256"
  trap 'rm -f "$TMP" "$SUM_FILE"' EXIT
  curl -fsSL "${RELEASE_URL}/${ASSET}" -o "$TMP"
  curl -fsSL "${RELEASE_URL}/SHA256SUMS-${VERSION}.txt" -o "$SUM_FILE"

  EXPECTED="$(awk -v asset="$ASSET" 'NF == 2 && $2 == asset { print $1 }' "$SUM_FILE")"
  ACTUAL="$(sha256sum "$TMP" | awk '{ print $1 }')"
  if [[ -z "$EXPECTED" || "$EXPECTED" != "$ACTUAL" ]]; then
    echo "Checksum mismatch for ${ASSET}" >&2
    exit 1
  fi
  mv "$TMP" "$APPIMAGE"
  trap - EXIT
fi
chmod +x "$APPIMAGE"

cat <<EOF
Starting HifiMagic ${VERSION}
  AppImage:    ${APPIMAGE}
  Player:      ${PLAYER_HOST}
  UI:          http://127.0.0.1:${PORT}
  Support URL: ${SUPPORT_URL}   (Stripe TEST mode)

In the browser, click the subtle "Support" link in the footer.
Use Stripe test card 4242 4242 4242 4242, any future expiry, any CVC.
No real money is charged. Press Ctrl+C to stop.
EOF

exec env \
  HIFIMAGIC_SUPPORT_URL="$SUPPORT_URL" \
  "$APPIMAGE" \
  --host "$PLAYER_HOST" \
  --port "$PORT" \
  "$@"
