#!/usr/bin/env bash
set -euo pipefail

# Patch 1 connects only. It never creates, reformats, enables or disables swap.
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
server0_ip="${SERVER0_IP:-10.0.0.13}"
server1_ip="${SERVER1_IP:-10.0.0.20}"
server0_port="${SERVER0_PORT:-9400}"
server1_port="${SERVER1_PORT:-9400}"
pool_gib="${POOL_GIB:-16}"
connect_timeout_ms="${CONNECT_TIMEOUT_MS:-15000}"

run_as_root() {
    if [[ ${EUID} -eq 0 ]]; then
        "$@"
    else
        sudo -- "$@"
    fi
}

require_no_swap() {
    if [[ $(wc -l < /proc/swaps) -ne 1 ]]; then
        echo 'Active swap exists. Explicitly stop the intended swap area before continuing.' >&2
        cat /proc/swaps >&2
        exit 1
    fi
}

case "${1:-}" in
    install|connect)
        require_no_swap
        if [[ -d /sys/module/rswap_client ]]; then
            echo 'rswap_client is already loaded.' >&2
            exit 1
        fi
        run_as_root insmod "${script_dir}/rswap-client.ko" \
            "sip=${server0_ip}" "sport=${server0_port}" \
            "sip2=${server1_ip}" "sport2=${server1_port}" \
            "rmsize=${pool_gib}" "connect_timeout_ms=${connect_timeout_ms}"
        echo 'Patch 1 connected. Swap activation is disabled until Patch 2/3.'
        ;;
    uninstall|disconnect)
        require_no_swap
        run_as_root rmmod rswap_client
        echo 'Restart both Server processes before the next connection.'
        ;;
    status)
        if [[ -d /sys/module/rswap_client ]]; then
            echo 'rswap_client: loaded'
        else
            echo 'rswap_client: not loaded'
        fi
        cat /proc/swaps
        ;;
    *)
        echo "Usage: $0 {install|uninstall|status}" >&2
        echo 'Overrides: SERVER0_IP SERVER1_IP SERVER0_PORT SERVER1_PORT POOL_GIB CONNECT_TIMEOUT_MS' >&2
        exit 2
        ;;
esac
