/* SPDX-License-Identifier: GPL-2.0 */
#ifndef RSWAP_HOTNESS_H
#define RSWAP_HOTNESS_H

#include <stdint.h>

/* One bit per sampling round, including rounds that cannot be observed.
 * The CLI bounds the window to 1..64. No per-page timer or TTL is needed.
 */
struct heat_window {
	uint64_t active;
	uint64_t valid;
};

static inline void heat_push(struct heat_window *h, int sample, unsigned int n)
{
	uint64_t mask = n == 64 ? UINT64_MAX : (UINT64_C(1) << n) - 1;

	h->active = ((h->active << 1) | (sample == 1)) & mask;
	h->valid = ((h->valid << 1) | (sample >= 0)) & mask;
}

static inline unsigned int heat_valid(const struct heat_window *h)
{
	return __builtin_popcountll(h->valid);
}

static inline unsigned int heat_active(const struct heat_window *h)
{
	return __builtin_popcountll(h->active);
}

/* -1 is unknown; zero is an observed cold page. Fixed point, parts per million. */
static inline int heat_ppm(const struct heat_window *h)
{
	unsigned int n = heat_valid(h);

	return n ? (int)(heat_active(h) * UINT64_C(1000000) / n) : -1;
}

#endif
