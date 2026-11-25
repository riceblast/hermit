# Remoteswap

**Development stage: Client 1.13 Adaptive unknown-page DRAM default (single/dual-Server BFS observations completed; dedicated functional validation pending).**
The new [capacity-mean policy](tests/capacity-mean-placement.md) uses an estimated
remote-candidate mean and separates
observed means from placement thresholds. Local upload ABI v2 requires matching
Client/sampler builds; Server protocol v2 is unchanged.
In 1.13, Adaptive Stores without a snapshot/threshold or with a PFN miss prefer
DRAM, sharing its Server round-robin with known-hot Stores. Zero heat keeps the
original interleave preference. See [the change and validation plan](tests/unknown-dram-placement.md).
The [single dual-Server BFS report](tests/unknown-dram-bfs-result.md) records balanced
Interleave traffic and an Adaptive DRAM/PMem total bandwidth ratio of 2.64; no follow-up trials were run.
The separately requested [single-Server BFS](tests/unknown-dram-single-bfs-result.md)
used a 16/32-GiB pool: DRAM filled, capacity fallback reversed the traffic balance,
and Adaptive DRAM/PMem bandwidth was 217.20/332.04 MiB/s. Neither run establishes a general performance guarantee.
The later requested [32/32-GiB single-Server run](tests/unknown-dram-single32-bfs-result.md)
had balanced Interleave traffic and Adaptive bandwidth of 536.08/224.57 MiB/s, with no capacity fallback.
Its short decision trace lost events; the cumulative WC bandwidth results remain available, while complete trace validation is uncertified.
The Server now requires the named arguments in the [Patch 5 Server guide](tests/patch5-server.md).
Client 1.13 discovers each Server's DRAM/PMem topology. Its Inactive/Warmup preferred pool
interleaves first across Servers, then across the selected Server's Tiers.
Actual Slots are dynamically allocated and retained until invalidation and the
last in-flight reference. Each interleave level stripes its common capacity and
appends the larger side's tail. See the [Patch 6 guide](tests/patch6-client.md)
for capacity limits, local checks and the minimal hardware acceptance plan.
Use matching protocol v2 builds; the Patch 1–4 Client cannot connect to this Server.
The historical Server command lines below and in Patch 1–4 test guides no longer apply.

The Client discovers each selected Server's capacity and checks the configured sum.
Single-server pools still interleave DRAM/PMem. Multi-session polling, request retirement and
managed Frontswap publication now permit one swap area no larger than the pool.
Use 4-KiB pages; disable THP for validation workloads. Whole-THP remote I/O is rejected.
Statistics now use format v2, with Server/Tier counters and successful-payload
bandwidth. The optional `rswap:placement` event is disabled by default; see the
[Patch 7 guide](tests/patch7-client.md) for the interface and core acceptance plan.
Use the updated Python tools; old stats v1 snapshots are not supported.
Current configuration expects two 48-GiB Servers (96 GiB total).

`mapping` now displays capacity/used/free with one decimal and KB/MB/GB suffixes
(binary units, switching only above 1024, capped at GB). Used combines the internal
live and retired counts. `mapping --raw`, or debugfs
`mapping_raw`, preserves the exact version-1 page/byte counts. Updated pool
collectors use the raw interface and retain compatibility with older Clients.

After connecting, `bash client/manage_rswap_client.sh address-map` (from this
directory), or `sudo cat /sys/kernel/debug/rswap/address_map`, lists the registered
remote virtual address ranges grouped by DRAM/PMem and Server. Each MR retains its
original Chunk number and reports its `[start,end)` range and byte length. This
read-only view is available before swapon; see the [pool guide](operation_doc/create_pool.md)
for interpretation and a minimal connection-only check.

The independent `tools/rswap_hotness` sampler observes a PID or launches a command,
tracks activity over a configurable window, and exports per-page heat and means.
The disabled-by-default `rswap:store_sample` event and offline checker correlate
prior heat snapshots with Store PFNs. With `--placement`, the sampler starts the
Client policy before launching a workload and publishes complete heat generations.
After warmup, known positive pages at or above the configured threshold prefer DRAM, lower positive pages PMem, with per-Tier
Server round-robin. Unknown pages prefer DRAM; zero-heat pages keep interleave.
All choices use capacity fallback.
Stop, uploader exit and swapoff clear policy without changing existing mappings.
See the [Patch 10 design and interfaces](tests/patch10-placement.md) and
[core acceptance plan](tests/patch10-core-smoke.md). No active migration is performed.
Client 1.11 adds module-lifetime `policy_stats`, Store post/WC correlation, and
`pool-status` / `pool-monitor` / `pool-report` commands. See the
[Patch 11 guide](tests/patch11-monitoring.md) and
[single/dual-node acceptance plan and results](tests/patch11-core-smoke.md).
For step-by-step commands to initialize both Servers and the Client, run BFS with
hotness tracking, observe placement/bandwidth and clean up, see the
[manual BFS guide](tests/bfs-end-to-end-guide.md).
The dense mapping and free lists use 12 bytes per pool page (288 MiB for 96 GiB,
10.5 GiB for 3.5 TiB), plus allocation/page-table overhead. They are allocated before
Frontswap registration even when the swap area is smaller than the pool.
See the [Patch 9 guide](tests/patch9-mapping.md) for design, local validation,
the `mapping` command, dynamic trace replay and hardware acceptance plan.
See the [Patch 8 guide](tests/patch8-monitor.md) for hotness semantics and prior hardware results.
The Patch 8/9/10 core instructions target older Client versions; use the Patch 11
plan for this version.

Use the matching Hermit kernel exporting `frontswap_register_managed_ops_v2`.
The Patch 9 review fix also changes swap-fault error/PTE validation in the kernel:
its fixed kernel must be deployed before hardware acceptance. The current Client
already runs the verified #8 build; Patch 10 does not change the kernel.
See the [minimal core acceptance and kernel handoff](tests/patch9-core-smoke.md).
The kernel release string is unchanged, so `uname -r` alone does not identify this build.
See the [Patch 3 summary](tests/patch3_summary.md) for changes and verification limits.
Use the [Patch 3 hardware test guide](tests/patch3-smoke.md) for commands and expected results.
See [Patch 4 statistics](tests/patch4_summary.md) for counter definitions and `stats` / `monitor` commands.
Use the [Patch 4 BFS test guide](tests/patch4-bfs-smoke.md) for the single-server 48-GiB and dual-server 80-GiB runs.
Patch 1/2 smoke guides are historical and must not be used as Patch 3 swap instructions.
The management script connects/disconnects and exposes monitoring/policy controls;
it does not create, format, activate or deactivate swap. Edit `client/rswap_client.conf` before install;
environment overrides are not used. The historical deployment instructions below
predate this prototype and its managed Frontswap lifecycle.

A RDMA-based remote swap system for remote memory and disaggregated clusters. The codebase is larged adopted from Fastswap (https://github.com/clusterfarmem/fastswap) and Canvas (https://github.com/uclasystem/canvas), but is enhanced with our optimizations.

## Prerequisites
Remoteswap is developed and tested under the following settings:

Hardware:
* Infiniband: Mellanox ConnectX-3/4 (40Gbps), or Mellanox ConnectX-6 (100Gbps)
* RoCE: Mellanox ConnectX-5 25GbE, or Mellanox ConnectX-5 Ex 100GbE

Software:
* OS: Ubuntu 18.04/20.04
* gcc 7.5.0/9.4.0
* Mellanox OFED driver: 5.4-3.1.0.0-LTS (for ConnectX-5 and 6), or 4.9-4.1.7.0-LTS (for ConnectX-3).

## Build & Install

### Install MLNX_OFED driver
Here is an example on Ubuntu 20.04:
```bash
# Download the MLNX OFED driver for the Ubuntu 20.04
wget https://content.mellanox.com/ofed/MLNX_OFED-5.4-3.1.0.0/MLNX_OFED_LINUX-5.4-3.1.0.0-ubuntu20.04-x86_64.tgz
tar xzf MLNX_OFED_LINUX-5.4-3.1.0.0-ubuntu20.04-x86_64.tgz
cd MLNX_OFED_LINUX-5.4-3.1.0.0-ubuntu20.04-x86_64

# Install the MLNX OFED driver against linux-5.14-rc5
sudo ./mlnxofedinstall --add-kernel-support --force
# restart openibd service as required
sudo systemctl restart openibd
# reboot
sudo reboot
```

### Client
On the host machine,
```bash
cd remoteswap/client
make
```

There should be a kernel module called `rswap-client.ko` under the client/ directory, which indicates a build success.

### Server
On the memory server,
```bash
cd remotswap/server
make
```

We should get a `rswap-server` after the compilation.

### DRAM client backend

Similar to Fastswap, remoteswap supports debugging using local memory as a fake "remote memory pool". In such case, there is no "remoteswap-server", and the "remoteswap-client" should be built in the following way:

```bash
# (under the remoteswap/client dir)
make BACKEND=DRAM
```

## Usage

1. On the memory server, run `rswap-server` first. This process must be alive all the time so either run it inside `tmux` or `screen`, or run it as a system service.

For now, we have to know the online core number of the **host server** first. You can check `/proc/cpuinfo` on the host server or simply get the number via `top` or `htop`.
A wrong core number will crash the kernel module.

```bash
cd remoteswap/server
./rswap-server <memory server ip> <memory server port> <memory pool size in GB> <number of cores on host server>
# an example: ./rswap-server 10.0.0.4 9400 48 32
```

2. On the host server, edit the parameters in `manage_rswap_client.sh` under `remoteswap/client` directory.

Here is an excerpt of the script:

```bash
# The swap file/partition size should be equal to the whole size of remote memory
SWAP_PARTITION_SIZE="48"

server_ip="10.0.0.2"
server_port="9400"
swap_file="/mnt/swapfile"
```

Make sure that "SWAP_PARTITION_SIZE" equals to the remote memory pool size you set when running `rswap-server`, as well as "server_ip" and "server_port" here.

"swap_file" is the path which the script uses to create a special file as a fake swap partition in the size of `SWAP_PARTITION_SIZE`GB. The script will create the file by itself but the path must be valid. The path can be "/mnt/swapfile" here, or somewhere under your home like "${HOME}/aaa/bbb".

3. On the host server, install the `rswap-client` kernel module, which will establish the connection to the server and finalize the setup.

```bash
./manage_rswap_client.sh install
```

The script should have done everything but in case here is the command to install the kernel module manually:

```bash
sudo insmod ./rswap-client.ko sip=${mem_server_ip} sport=${mem_server_port} rmsize=${SWAP_PARTITION_SIZE_GB}
```

It might take a while to allocate and register all memory in the memory pool, and establish the connection. The system should have been fully set up now.

You can optionally check system log via `dmesg`. A success should look like (1 chunk is 4GB so 12 chunks are essentially 48GB remote memory):
```
rswap_request_for_chunk, Got 12 chunks from memory server.
rdma_session_connect,Exit the main() function with built RDMA conenction rdma_session_context:0xffffffffc08f7460.
frontswap module loaded
```

4. To uninstall the kernel module, one can run the following command on the host server
```bash
cd remoteswap/client
sudo ./manage_rswap_client.sh uninstall
```

## Acknowledgement
The codebase is largely adopted from Fastswap (https://github.com/clusterfarmem/fastswap) and Canvas (https://github.com/uclasystem/canvas).

## Contact
Please contact yifanqiao [at] g [dot] ucla [dot] edu for assistance.
