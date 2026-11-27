#!/usr/bin/env bash
# Operator installation of verified stage-1 artifacts. No swap changes/reboot.
set -euo pipefail

repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
kernel="$repo/linux-5.14-rc5"
build_dir=/tmp/rswap-stage1-work
expected_release=5.14.0-rc5-hermit-syscall-v2

die() { echo "STOP: $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run with sudo bash $repo/remoteswap/tests/stage1_install_kernel.sh"
[[ -f "$build_dir/READY" && -f "$build_dir/artifacts.sha256" ]] || die 'Build/test handoff is not ready.'
[[ $(uname -r) == "$expected_release" ]] || die 'Run from the currently deployed Hermit kernel.'
[[ $(wc -l < /proc/swaps) -eq 1 ]] || die 'Complete normal swapoff first, keeping its Server online.'
[[ ! -d /sys/module/rswap_client ]] || die 'Unload the inactive Client first.'
release=$(sed -n 's/^#define UTS_RELEASE "\(.*\)"/\1/p' "$kernel/include/generated/utsrelease.h")
[[ $release == "$expected_release" ]] || die "Unexpected built release: $release"
[[ $(modinfo -F version "$repo/remoteswap/client/rswap-client.ko") == 1.17 ]] || die 'Expected Client 1.17.'
grep -qw rswap_identity_scan_mm "$kernel/System.map" || die 'Built kernel lacks the identity interface.'
grep -qw __tracepoint_rswap_swap_bind "$kernel/System.map" || die 'Built kernel lacks the bind notification.'
grep -qw __tracepoint_rswap_swap_free "$kernel/System.map" || die 'Built kernel lacks the free notification.'
sha256sum -c "$build_dir/artifacts.sha256"
for name in vmlinuz initrd.img System.map config; do
    [[ -f "/boot/$name-$release" ]] || die "Missing current /boot/$name-$release; inspect before installation."
done
[[ -d "/lib/modules/$release" ]] || die 'Current module directory is missing.'

# Preserve the existing OFED release layout and retain the complete old tree.
backup_parent="${repo%/*}/hermit-kernel-backups"
mkdir -p -- "$backup_parent"
backup=$(mktemp -d "$backup_parent/stage1-$(date +%Y%m%d-%H%M%S)-XXXXXX")
echo "Backing up current kernel and modules to: $backup"
mkdir "$backup/boot"
for name in vmlinuz initrd.img System.map config; do
    cp -a -- "/boot/$name-$release" "$backup/boot/"
done
cp -a -- "/lib/modules/$release" "$backup/modules"
cp -a -- /etc/default/grub "$backup/grub-default"
cp -a -- /boot/grub/grub.cfg "$backup/grub.cfg"
cp -- "$build_dir/artifacts.sha256" "$backup/new-artifacts.sha256"
uname -a > "$backup/old-uname.txt"

echo 'Backup complete. Installing modules and kernel; OFED extra/updates paths are retained.'
make -C "$kernel" LOCALVERSION= INSTALL_MOD_STRIP=1 modules_install 2>&1 | tee "$backup/modules-install.log"
make -C "$kernel" LOCALVERSION= install 2>&1 | tee "$backup/kernel-install.log"
cmp -- "$kernel/arch/x86/boot/bzImage" "/boot/vmlinuz-$release"
[[ -s "/boot/initrd.img-$release" ]] || die 'Installed initramfs is missing or empty.'
expected_version=$(sed -n 's/^#define UTS_VERSION "\(.*\)"/\1/p' "$kernel/include/generated/compile.h")
printf '\nINSTALL PASS\nBackup: %s\nBoot release: %s\nExpected uname -v: %s\n' \
    "$backup" "$release" "$expected_version"
echo 'Reboot manually into the usual Hermit boot entry, then verify uname -v and the new symbol.'
echo 'Return for stage-1 hardware validation before starting stage 2.'
