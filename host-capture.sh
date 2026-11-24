#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Run this on the HOST PC, not inside the VM.
#
# It:
#   1. temporarily allows your LAN subnet to reach the forwarded ports
#   2. opens SSH local forwards 8080/8081 from the host to the VM
#   3. removes the nftables rule when you stop the script with Ctrl+C
#
# Override if needed:
#   SOURCE_SUBNET=192.168.1.0/24 VM_USER=iriseth VM_ADDR=192.168.122.50 ./host-capture.sh

set -euo pipefail

SOURCE_SUBNET="${SOURCE_SUBNET:-192.168.49.0/24}"
VM_USER="${VM_USER:-iriseth}"
VM_ADDR="${VM_ADDR:-192.168.122.50}"
VM_PROXY_PORT="${VM_PROXY_PORT:-8080}"
VM_WEB_PORT="${VM_WEB_PORT:-8081}"
LOCAL_PROXY_PORT="${LOCAL_PROXY_PORT:-8080}"
LOCAL_WEB_PORT="${LOCAL_WEB_PORT:-8081}"

RULE_COMMENT="hifimagic capture proxy"
NFT_FAMILY=""
NFT_TABLE=""
NFT_CHAIN=""
RULE_HANDLE=""
SSH_PID=""

cleanup() {
  trap - EXIT INT TERM
  if [[ -n "$SSH_PID" ]]; then
    kill "$SSH_PID" 2>/dev/null || true
    wait "$SSH_PID" 2>/dev/null || true
  fi
  if [[ -n "$NFT_FAMILY" && -n "$NFT_TABLE" && -n "$NFT_CHAIN" && -n "$RULE_HANDLE" ]]; then
    echo "Removing temporary nftables rule (handle $RULE_HANDLE)..."
    nft delete rule "$NFT_FAMILY" "$NFT_TABLE" "$NFT_CHAIN" handle "$RULE_HANDLE" 2>/dev/null || true
  fi
}

trap cleanup EXIT INT TERM

if ! command -v nft >/dev/null 2>&1; then
  echo "ERROR: nft not found. Install nftables or run the equivalent firewall command." >&2
  exit 1
fi

echo "Finding nftables input chain..."
for candidate in "inet filter input" "ip filter INPUT" "inet filter INPUT" "ip filter input"; do
  read -r family table chain <<<"$candidate"
  if nft list chain "$family" "$table" "$chain" >/dev/null 2>&1; then
    NFT_FAMILY="$family"
    NFT_TABLE="$table"
    NFT_CHAIN="$chain"
    break
  fi
done

if [[ -z "$NFT_CHAIN" ]]; then
  echo "ERROR: could not find a usual input chain." >&2
  echo "Run 'sudo nft list chains' and set NFT_FAMILY/NFT_TABLE/NFT_CHAIN manually in this script." >&2
  exit 1
fi

echo "Using chain: $NFT_FAMILY $NFT_TABLE $NFT_CHAIN"

if nft -a list chain "$NFT_FAMILY" "$NFT_TABLE" "$NFT_CHAIN" | grep -Fq "$RULE_COMMENT"; then
  echo "Firewall rule already present; leaving it alone."
else
  echo "Allowing $SOURCE_SUBNET -> TCP $LOCAL_PROXY_PORT,$LOCAL_WEB_PORT ..."
  nft insert rule "$NFT_FAMILY" "$NFT_TABLE" "$NFT_CHAIN" index 0 \
    ip saddr "$SOURCE_SUBNET" tcp dport "{ $LOCAL_PROXY_PORT, $LOCAL_WEB_PORT }" ct state new \
    comment "\"$RULE_COMMENT\"" accept
fi

RULE_HANDLE="$(nft -a list chain "$NFT_FAMILY" "$NFT_TABLE" "$NFT_CHAIN" \
  | grep -F "$RULE_COMMENT" \
  | sed -n 's/.*# handle \([0-9][0-9]*\).*/\1/p' \
  | tail -1)"

if [[ -z "$RULE_HANDLE" ]]; then
  echo "ERROR: could not read the inserted rule handle." >&2
  exit 1
fi

HOST_IP="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
[[ -n "$HOST_IP" ]] || HOST_IP="<this-host-LAN-IP>"

echo
echo "Starting SSH forwards to $VM_USER@$VM_ADDR ..."
echo "  phone HTTP proxy: http://$HOST_IP:$LOCAL_PROXY_PORT"
echo "  mitmweb UI:       http://$HOST_IP:$LOCAL_WEB_PORT"
echo "  CA cert:          http://mitm.it while the phone proxy is set"
echo
echo "Leave this running while capturing. Press Ctrl+C to stop and remove the firewall rule."
echo

ssh -N -o GatewayPorts=yes -o ExitOnForwardFailure=yes \
  -L "0.0.0.0:$LOCAL_PROXY_PORT:127.0.0.1:$VM_PROXY_PORT" \
  -L "0.0.0.0:$LOCAL_WEB_PORT:127.0.0.1:$VM_WEB_PORT" \
  "$VM_USER@$VM_ADDR" &
SSH_PID=$!

wait "$SSH_PID"
