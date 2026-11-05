#!/usr/bin/env bash
set -euo pipefail

# Connect/disconnect only: swap activation is an explicit operator action.
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
config_file="${script_dir}/rswap_client.conf"

die() {
    echo "Error: $*" >&2
    exit 1
}

run_as_root() {
    if [[ ${EUID} -eq 0 ]]; then
        "$@"
    else
        sudo -- "$@"
    fi
}

require_no_swap() {
    if [[ $(wc -l < /proc/swaps) -ne 1 ]]; then
        cat /proc/swaps >&2
        die 'Active swap exists. Explicitly stop the intended swap area first.'
    fi
}

require_uint() {
    local name="$1"
    local value="${!name-}"
    [[ ${value} =~ ^[1-9][0-9]{0,9}$ ]] || die "${name} must be a positive decimal integer."
    (( value >= $2 && value <= $3 )) || die "${name} must be in $2..$3."
}

case "${1:-}" in
    install|connect)
        [[ -r ${config_file} ]] || die "Cannot read ${config_file}."
        # Missing keys must not silently fall back to inherited environment values.
        unset NR_SERVERS POOL_GIB CONNECT_TIMEOUT_MS SERVER0_IP SERVER0_PORT SERVER1_IP SERVER1_PORT
        source "${config_file}"
        require_uint NR_SERVERS 1 2
        # rmsize is a signed-int module parameter; MR limits are checked there.
        require_uint POOL_GIB "$((NR_SERVERS * 8))" 2147483647
        (( POOL_GIB % 8 == 0 )) || die 'POOL_GIB must be a multiple of 8 GiB.'
        require_uint CONNECT_TIMEOUT_MS 100 300000
        [[ -n ${SERVER0_IP:-} ]] || die 'SERVER0_IP is required.'
        require_uint SERVER0_PORT 1 65535
        if (( NR_SERVERS == 2 )); then
            [[ -n ${SERVER1_IP:-} ]] || die 'SERVER1_IP is required for two nodes.'
            require_uint SERVER1_PORT 1 65535
        fi

        printf 'Config: %s\nNodes: %s\nExpected total: %s GiB\nSetup deadline per session: %s ms\n' \
            "${config_file}" "${NR_SERVERS}" "${POOL_GIB}" "${CONNECT_TIMEOUT_MS}"
        printf 'Server 0: %s:%s\n' "${SERVER0_IP}" "${SERVER0_PORT}"
        if (( NR_SERVERS == 2 )); then
            printf 'Server 1: %s:%s\n' "${SERVER1_IP}" "${SERVER1_PORT}"
        fi
        echo 'Mode: 4-KiB remote swap ready; activate the intended swap area explicitly.'

        require_no_swap
        [[ ! -d /sys/module/rswap_client ]] || die 'rswap_client is already loaded.'
        module_args=("nr_servers=${NR_SERVERS}" "rmsize=${POOL_GIB}" \
            "connect_timeout_ms=${CONNECT_TIMEOUT_MS}" \
            "sip=${SERVER0_IP}" "sport=${SERVER0_PORT}")
        if (( NR_SERVERS == 2 )); then
            module_args+=("sip2=${SERVER1_IP}" "sport2=${SERVER1_PORT}")
        fi
        run_as_root insmod "${script_dir}/rswap-client.ko" "${module_args[@]}"
        echo 'Client initialized. No swap area was activated by this script.'
        ;;
    uninstall|disconnect)
        # A missing or broken configuration must never prevent normal unloading.
        require_no_swap
        run_as_root rmmod rswap_client
        echo 'Restart the participating Server processes before reconnecting.'
        ;;
    status)
        if [[ -d /sys/module/rswap_client ]]; then
            echo 'rswap_client: loaded (actual module parameters below)'
            for name in nr_servers rmsize connect_timeout_ms sip sport sip2 sport2; do
                param_file="/sys/module/rswap_client/parameters/${name}"
                if [[ -r ${param_file} ]]; then
                    printf '%s=%s\n' "${name}" "$(cat "${param_file}")"
                fi
            done
        else
            echo 'rswap_client: not loaded'
        fi
        cat /proc/swaps
        ;;
    *)
        echo "Usage: $0 {install|uninstall|status}" >&2
        echo "Edit ${config_file} before install; environment overrides are not used." >&2
        exit 2
        ;;
esac
