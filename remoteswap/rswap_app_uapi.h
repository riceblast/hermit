/* SPDX-License-Identifier: GPL-2.0 */
#ifndef RSWAP_APP_UAPI_H
#define RSWAP_APP_UAPI_H

#include <linux/types.h>

/* Independent of the hotness upload ABI and Server protocol. One registration
 * per open app_policy fd; closing it unregisters only this binding.
 */
#define RSWAP_APP_VERSION 1
enum rswap_app_policy { RSWAP_APP_DRAM = 1, RSWAP_APP_PMEM = 2 };

struct rswap_app_command {
	__u32 version;
	__s32 pid;
	__u32 policy, reserved;
};

struct rswap_app_ack {
	__u64 binding_id;
	__u32 policy, active;
};

#endif
