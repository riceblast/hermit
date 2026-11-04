# Patch 1 build verification — 2026-11-04

Compiler: GCC 9.5.0. Kernel configuration/release:
`5.14.0-rc5-hermit-syscall-v2`, using the existing OFED headers/symbols.

| Check | Result |
| --- | --- |
| `make -C linux-5.14-rc5 -j12 bzImage modules` | PASS |
| `make -C remoteswap/client -j12 BACKEND=DRAM` | PASS |
| `make -C remoteswap/client -j12` (final RDMA build) | PASS |
| `make -C remoteswap/server` | PASS |
| `bash -n remoteswap/client/manage_rswap_client.sh` | PASS |
| `git diff --check` | PASS |
| New managed Frontswap exports in kernel `Module.symvers` | Both present |
| Final Client symbols | `rdma_sessions` present; RDMA backend retained |

Artifacts:

- `linux-5.14-rc5/arch/x86/boot/bzImage`
- `remoteswap/client/rswap-client.ko`
- `remoteswap/server/rswap-server`

The old kernel's assembly/link steps emit GNU-stack/RWX segment warnings.
The successful final Client, DRAM and Server build logs contain no compiler
warnings. Detailed logs are `/tmp/hermit-patch1-{kernel,client,dram,server}-build.log`.

No kernel installation, reboot, insmod/rmmod, swap changes, SSH/RDMA connection
or workload was performed. Build success does not establish runtime safety.
Use [the operator smoke-test guide](patch1-smoke.md) after booting the new kernel.
The release name is unchanged; verify the new exported function symbols, not
just `uname -r`, before insertion.

[Bitmap correctness review](frontswap-validity-review.diff) isolates the
accepted validity/overwrite fixes from the lifecycle/SRCU changes. It is a
review artifact against the original file; the changes are already present
in the working tree and must not be applied again.
