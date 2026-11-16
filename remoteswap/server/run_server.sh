#!/usr/bin/env bash
set -euo pipefail

# Edit these settings before starting the Server.
SERVER_IP="10.0.0.20"
SERVER_PORT=9400
CLIENT_CORES=48
DRAM_NODE=0
DRAM_GIB=16
PMEM_NODE="auto"  # Find the unique CPU-less memory node, or specify its ID.
PMEM_GIB=32

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec "${script_dir}/rswap-server" \
    --ip "${SERVER_IP}" --port "${SERVER_PORT}" \
    --client-cores "${CLIENT_CORES}" \
    --dram-node "${DRAM_NODE}" --dram-gib "${DRAM_GIB}" \
    --pmem-node "${PMEM_NODE}" --pmem-gib "${PMEM_GIB}"
