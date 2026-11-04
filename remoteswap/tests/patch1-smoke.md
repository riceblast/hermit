# Patch 1: dual-session connection and lifecycle smoke tests

Scope: Client 10.0.0.11, Server 0 (server02) 10.0.0.20, Server 1
(node04) 10.0.0.13. Each Server provides 8 GiB; the Client expects 16 GiB.
No workload or swap activation is needed for these tests.

Patch 1 deliberately registers a managed backend with `max_pages=0`.
`swapon` is rejected with EOPNOTSUPP until Patch 2/3 implement striping and
multi-session completion/polling. Do not use the old single-session I/O paths
for a 16 GiB swap area. No runtime enable switch bypasses this restriction.

## Build and kernel prerequisites

From the repository root:

```bash
make -C linux-5.14-rc5 -j12 bzImage modules
make -C remoteswap/client -j12
make -C remoteswap/server
```

The new kernel must be installed and booted by the operator before loading
the Client. Building it does not update the running kernel. The release name
is unchanged to retain the existing OFED build configuration; `uname -r`
alone therefore does not establish that the new kernel is running. Check:

```bash
sudo grep -w -E 'frontswap_(register|unregister)_managed_ops' /proc/kallsyms
```

Both function symbols must exist. Keep a working kernel boot entry.
The Client build uses the repository's matching kernel build/Module.symvers
and the existing OFED headers/symbols; do not override vermagic or force load.

Review `/proc/swaps`. The operator must explicitly stop the existing swap
areas before managed registration. Do not use `swapoff -a` in automation.
The management script reports active swap and exits without changing it.

## T0: SSH and RDMA connectivity (operator)

Use account `yxr` on all nodes. If SSH fails, stop and report the failure.
Before cross-node tests, run these pairs, one pair at a time:

On server02:

```bash
bash /home/yxr/scripts/r_server.sh 20
```

On the Client:

```bash
bash /home/yxr/scripts/r_client.sh 20 11
```

Then on node04:

```bash
bash /home/yxr/scripts/r_server.sh 13
```

On the Client:

```bash
bash /home/yxr/scripts/r_client.sh 13 11
```

The scripts take IP suffixes, not full IP addresses. Expect 10 successful
rping exchanges and a successful Client exit. If RDMA fails, stop and report;
do not continue with module loading. rping and remote-swap use separate
services; the remote-swap port is 9400 on each Server.

## T1: successful connection, unload and reconnect (operator)

1. On the Client, obtain the online CPU count with
   `getconf _NPROCESSORS_ONLN`. Use this count on **both** Servers. Keep the
   online CPU set unchanged during testing.
2. On each Server, from `/home/yxr/projects/hermit/remoteswap/server`, build
   the existing Server and start it in its own terminal:

   ```text
   server02: ./rswap-server 10.0.0.20 9400 8 <Client-online-CPU-count>
   node04:   ./rswap-server 10.0.0.13 9400 8 <Client-online-CPU-count>
   ```

   Use the normal RDMA/memlock privileges for that machine. No Server source
   changes are required. Wait for the Server memory allocation to complete.
3. With no active swap on the Client:

   ```bash
   cd /mnt/sdb3/data/yxr/projects/hermit/remoteswap/client
   ./manage_rswap_client.sh install
   ./manage_rswap_client.sh status
   sudo dmesg | tail -100
   ```

   Expect `server=0 ready ... capacity=8 GiB regions=1`, the equivalent
   Server 1 message, and `pool ready servers=2 total=16 GiB`.
   The module is loaded; swap remains inactive.
4. Unload:

   ```bash
   ./manage_rswap_client.sh uninstall
   ./manage_rswap_client.sh status
   sudo dmesg | tail -100
   ```

   Expect both `resources released` messages, `client unloaded`, and no
   loaded Client module. There should be no Oops/BUG/unexpected WARN.
5. Restart both Server processes and repeat this successful cycle once.

## T2: Server 1 unavailable, rollback and retry (operator)

1. Ensure the Client module is unloaded and swap remains inactive.
2. Restart Server 0 with 8 GiB. Leave the Server 1 remote-swap process stopped.
3. Run `./manage_rswap_client.sh install` on the Client and record its exit
   code immediately. Do not run `rmmod` to repair a failed insertion.
4. Expect a nonzero exit, an initialization-error/rollback log, and release
   of Server 0 and any created Server 1 resources. `status` must show the
   module is not loaded. A protocol timeout is bounded by the configured
   per-session deadline (default 30000 ms); driver teardown is not a hard
   real-time operation.
5. Restart both Server processes. Repeat T1's install/uninstall once. A
   successful retry is part of this test, not an inference from log output.

Save the command exit codes and the relevant new Client/Server log excerpts.
Do not clear the global kernel log. Do not run BFS or another workload yet.

## Remaining validation

Compilation is not runtime validation. QP flush/CM synchronization and
resource reclamation must be confirmed with the actual NIC/OFED stack.
Driver hangs/hot-unplug are outside the basic connection-failure guarantee;
a timeout never authorizes freeing DMA-visible memory early.

After T0/T1/T2 pass, retain only the necessary deeper checks: one silent
handshake timeout/flush case, active-swap unload protection and concurrent
activation/unload, then real page-content verification after Patch 2/3.
The BFS script is recorded at
`/mnt/sdb2/yxr/K_R_benchmarks/ligra/scripts/cgroup_run_bfs.sh`; it is not run
in Patch 1. The swapfile path will be supplied by the operator later.

## Review boundaries

Kernel changes are limited to `include/linux/frontswap.h`, `mm/frontswap.c`
and `mm/swapfile.c`. Managed registration is exclusive and occurs with no
SWP_USED swap area; a binding holds the owning module reference until area
invalidation completes. SRCU separately protects synchronous backend calls,
including poll/peek, from ops removal. Asynchronous RDMA lifetime is handled
by the Client's stop/drain/destroy path, not by SRCU.

Review the bitmap correctness changes separately from lifecycle changes:
`__frontswap_set/clear` count bit transitions; duplicate store invalidates
old backend data before attempting replacement; load tests the existing
Frontswap bitmap before consulting a backend. This uses the existing bitmap,
not a new page-location table. None of these changes implements recovery of
remote-only data after a running Server fails.
