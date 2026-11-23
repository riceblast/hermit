/* SPDX-License-Identifier: GPL-2.0 */
#ifndef RSWAP_PLACEMENT_UAPI_H
#define RSWAP_PLACEMENT_UAPI_H

#include <linux/types.h>

#define RSWAP_PLACEMENT_VERSION 2
#define RSWAP_HEAT_SCALE 1000000
#define RSWAP_UPLOAD_BATCH 4096
enum rswap_upload_op { RSWAP_UPLOAD_START = 1, RSWAP_UPLOAD_BEGIN,
	RSWAP_UPLOAD_DATA, RSWAP_UPLOAD_COMMIT };

enum rswap_threshold_policy { RSWAP_THRESHOLD_GLOBAL, RSWAP_THRESHOLD_CAPACITY };
enum rswap_threshold_source { RSWAP_THRESHOLD_MEAN, RSWAP_THRESHOLD_TRIMMED,
	RSWAP_THRESHOLD_RETAINED, RSWAP_THRESHOLD_FALLBACK };
enum rswap_threshold_status { RSWAP_THRESHOLD_OK, RSWAP_THRESHOLD_LOW_COVERAGE,
	RSWAP_THRESHOLD_NO_PRESSURE, RSWAP_THRESHOLD_SMALL_SAMPLE };

/* ABI v2: BEGIN is followed by this metadata, DATA by heat records.
 * generation identifies the original threshold calculation, not its last reuse.
 * Population/sample/candidate are de-duplicated threshold-model page counts.
 */
struct rswap_threshold_metadata {
	__u32 policy, source;
	__s32 threshold_ppm;
	__u32 status;
	__u64 generation, population_pages, sample_pages, candidate_pages, local_pages;
	__u64 min_pages;
	__u32 min_coverage_ppm, reserved;
};

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
