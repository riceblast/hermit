/* SPDX-License-Identifier: GPL-2.0 */
#ifndef _LINUX_RSWAP_IDENTITY_H
#define _LINUX_RSWAP_IDENTITY_H

#include <linux/tracepoint.h>

struct mm_struct;

/* Synchronous metadata notifications. Probes run under VM locks, must not
 * sleep, and must copy mm/address rather than retain a VMA or PTE pointer.
 * No heat sampling or placement policy belongs in these hooks.
 */
DECLARE_TRACE(rswap_swap_bind,
	TP_PROTO(struct mm_struct *mm, unsigned long address,
		 unsigned int type, unsigned long offset),
	TP_ARGS(mm, address, type, offset));

DECLARE_TRACE(rswap_swap_free,
	TP_PROTO(unsigned int type, unsigned long offset),
	TP_ARGS(type, offset));

/* Caller owns an mm reference. Scan existing base-page swap PTEs without
 * faulting pages in, changing accessed bits, or splitting huge pages.
 */
int rswap_identity_scan_mm(struct mm_struct *mm);

#endif
