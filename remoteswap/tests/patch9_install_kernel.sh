#!/usr/bin/env bash
# Operator runs this with sudo. Builds/tests are performed beforehand by Codex.
# Installs the existing Hermit release, preserving the OFED release/ABI path.
# Does not change swap configuration, GRUB defaults or reboot the machine.
set -euo pipefail

repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
kernel="$repo/linux-5.14-rc5"
build_dir=/tmp/hermit-p9-fix
expected_release=5.14.0-rc5-hermit-syscall-v2

die() { echo "STOP: $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run with sudo bash $repo/remoteswap/tests/patch9_install_kernel.sh"
[[ -f "$build_dir/READY" && -f "$build_dir/artifacts.sha256" ]] || die 'Build/test handoff is not ready.'
[[ $(uname -r) == "$expected_release" ]] || die 'Boot the currently deployed Hermit kernel before replacing it.'
[[ $(wc -l < /proc/swaps) -eq 1 ]] || die 'An active swap area exists; keep its Server online and complete normal swapoff first.'
[[ ! -d /sys/module/rswap_client ]] || die 'Unload the inactive Client first.'
release=$(sed -n 's/^#define UTS_RELEASE "\(.*\)"/\1/p' "$kernel/include/generated/utsrelease.h")
[[ $release == "$expected_release" ]] || die "Unexpected built release: $release"
grep -qw frontswap_register_managed_ops_v2 "$kernel/System.map" || die 'Built kernel lacks managed Frontswap v2.'
sha256sum -c "$build_dir/artifacts.sha256"
for name in vmlinuz initrd.img System.map config; do
    [[ -f "/boot/$name-$release" ]] || die "Missing current /boot/$name-$release; inspect before installation."
done
[[ -d "/lib/modules/$release" ]] || die 'Current module directory is missing.'

# Use the data disk for the complete old module tree, not the root filesystem.
backup_parent="${repo%/*}/hermit-kernel-backups"
mkdir -p -- "$backup_parent"
backup=$(mktemp -d "$backup_parent/p9fix-$(date +%Y%m%d-%H%M%S)-XXXXXX")
echo "Backing up the current kernel and module tree to: $backup"
mkdir "$backup/boot"
for name in vmlinuz initrd.img System.map config; do
    cp -a -- "/boot/$name-$release" "$backup/boot/"
done
cp -a -- "/lib/modules/$release" "$backup/modules"
cp -a -- /etc/default/grub "$backup/grub-default"
cp -a -- /boot/grub/grub.cfg "$backup/grub.cfg"
cp -- "$build_dir/artifacts.sha256" "$backup/new-artifacts.sha256"
uname -a > "$backup/old-uname.txt"

echo 'Backup complete. Installing modules and kernel; existing OFED extra/updates paths are retained.'
make -C "$kernel" LOCALVERSION= INSTALL_MOD_STRIP=1 modules_install 2>&1 | tee "$backup/modules-install.log"
make -C "$kernel" LOCALVERSION= install 2>&1 | tee "$backup/kernel-install.log"
cmp -- "$kernel/arch/x86/boot/bzImage" "/boot/vmlinuz-$release"
[[ -s "/boot/initrd.img-$release" ]] || die 'Installed initramfs is missing or empty.'
expected_version=$(sed -n 's/^#define UTS_VERSION "\(.*\)"/\1/p' "$kernel/include/generated/compile.h")
printf '\nINSTALL PASS\nBackup: %s\nBoot release: %s\nExpected uname -v: %s\n' \
    "$backup" "$release" "$expected_version"
echo 'Now reboot manually into this Hermit entry using your usual boot selection.'
echo 'Do not start Servers or enable swap yet; return to Codex after reboot for verification.'
