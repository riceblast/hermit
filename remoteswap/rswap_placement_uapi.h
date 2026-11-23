/* SPDX-License-Identifier: GPL-2.0 */
#ifndef RSWAP_PLACEMENT_UAPI_H
#define RSWAP_PLACEMENT_UAPI_H

#include <linux/types.h>

#define RSWAP_PLACEMENT_VERSION 1
#define RSWAP_HEAT_SCALE 1000000
#define RSWAP_UPLOAD_BATCH 4096
enum rswap_upload_op { RSWAP_UPLOAD_START = 1, RSWAP_UPLOAD_BEGIN,
	RSWAP_UPLOAD_DATA, RSWAP_UPLOAD_COMMIT };

/* Native endian, fixed-width local ABI. One command per write, no padding data.
 * START: count=warmup_ns, value=manual (0/1), generation=0.
 * BEGIN: generation>=1, count=resident records, value=mean contributors.
 * DATA: generation, count records immediately follow this header.
 * COMMIT: generation. Other fields zero, except BEGIN's mean_ppm.
 */
struct rswap_upload_command {
	__u32 version, op;
	__u64 generation, count, value;
	__s32 mean_ppm;
	__u32 reserved;
};

struct rswap_heat_record {
	__u64 pfn;
	__u32 heat_ppm, reserved;
};

/* pread(offset=0) after START/COMMIT; generation=0 means no published snapshot. */
struct rswap_upload_ack {
	__u64 run_id, start_ns, generation, publish_ns;
};

#endif
