/* SPDX-License-Identifier: GPL-2.0 */
#ifndef RSWAP_HOTNESS_H
#define RSWAP_HOTNESS_H
#include <stdint.h>
#include "../rswap_vpn_heat_uapi.h"

/* Q32, round to nearest after each nonoverlapping group. No kernel FP. */
static inline uint64_t heat_update(uint64_t old, unsigned int hits, unsigned int rounds)
{
	uint64_t denominator = 5ULL * rounds;
	return (4ULL * old * rounds + (uint64_t)hits * RSWAP_VPN_HEAT_SCALE +
		denominator / 2) / denominator;
}

static inline unsigned int heat_bin(uint64_t heat)
{
	/* Compare to the nearest Q32 representation of each exact fifth.
	 * In particular H=0.2 after an all-active first group belongs to bin 1. */
	unsigned int bin = (heat * 5 + 2) / RSWAP_VPN_HEAT_SCALE;
	return bin > 4 ? 4 : bin;
}
#endif
