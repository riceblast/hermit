/* SPDX-License-Identifier: GPL-2.0 */
#ifndef RSWAP_THRESHOLD_H
#define RSWAP_THRESHOLD_H

#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include "../rswap_placement_uapi.h"

/* Exact fixed-point heat values attainable with a <=64-round window. */
#define RSWAP_THRESHOLD_BINS 2144
struct rswap_heat_bin { uint32_t heat; uint64_t pages; };
struct rswap_threshold_hist {
	uint64_t counts[65][65]; /* valid, active; unknowns are not heat zero */
	uint64_t population, known;
};
struct rswap_threshold_config {
	unsigned int policy, min_coverage_ppm;
	uint64_t min_pages;
};

static int rswap_heat_bin_compare(const void *a, const void *b)
{
	const struct rswap_heat_bin *x = a, *y = b;
	return (x->heat > y->heat) - (x->heat < y->heat);
}

static size_t rswap_threshold_bins(const struct rswap_threshold_hist *hist,
		struct rswap_heat_bin *bins)
{
	size_t n = 0, merged = 0;
	for (unsigned int v = 1; v <= 64; v++)
		for (unsigned int a = 0; a <= v; a++)
			if (hist->counts[v][a])
				bins[n++] = (struct rswap_heat_bin){a * 1000000U / v, hist->counts[v][a]};
	qsort(bins, n, sizeof(*bins), rswap_heat_bin_compare);
	for (size_t i = 0; i < n; i++) {
		if (merged && bins[merged - 1].heat == bins[i].heat)
			bins[merged - 1].pages += bins[i].pages;
		else bins[merged++] = bins[i];
	}
	return merged;
}

/* ceil(K*(M-B)/M): trim a proportion of the known sample, not B known pages.
 * 128-bit intermediate products avoid overflow on large memory pools.
 * The candidate mean is weighted integer heat_ppm, rounded down once.
 */
static struct rswap_threshold_metadata rswap_threshold_select(
		const struct rswap_threshold_config *cfg,
		const struct rswap_threshold_hist *hist,
		const struct rswap_heat_bin *bins, size_t n, uint64_t local_pages,
		uint64_t generation, int mean,
		const struct rswap_threshold_metadata *previous)
{
	struct rswap_threshold_metadata m = {
		.policy = cfg->policy, .source = RSWAP_THRESHOLD_MEAN,
		.threshold_ppm = mean, .generation = mean >= 0 ? generation : 0,
		.population_pages = hist->population, .sample_pages = hist->known,
		.candidate_pages = hist->known, .local_pages = local_pages,
		.min_pages = cfg->min_pages, .min_coverage_ppm = cfg->min_coverage_ppm,
	};
	if (cfg->policy == RSWAP_THRESHOLD_GLOBAL) return m;
	m.source = RSWAP_THRESHOLD_FALLBACK;
	m.candidate_pages = 0;
	if (hist->population <= local_pages) m.status = RSWAP_THRESHOLD_NO_PRESSURE;
	else if (!hist->known || (__uint128_t)hist->known * RSWAP_HEAT_SCALE <
			(__uint128_t)hist->population * cfg->min_coverage_ppm)
		m.status = RSWAP_THRESHOLD_LOW_COVERAGE;
	else {
		uint64_t remain = (uint64_t)(((__uint128_t)hist->known *
			(hist->population - local_pages) + hist->population - 1) / hist->population);
		m.candidate_pages = remain;
		if (hist->known < cfg->min_pages || remain < cfg->min_pages)
			m.status = RSWAP_THRESHOLD_SMALL_SAMPLE;
		else {
			__uint128_t sum = 0;
			for (size_t i = 0; i < n && remain; i++) {
				uint64_t take = bins[i].pages < remain ? bins[i].pages : remain;
				sum += (__uint128_t)take * bins[i].heat;
				remain -= take;
			}
			if (!remain) {
				m.source = RSWAP_THRESHOLD_TRIMMED;
				m.threshold_ppm = (int)(sum / m.candidate_pages);
				m.generation = generation;
				return m;
			}
			m.status = RSWAP_THRESHOLD_SMALL_SAMPLE;
		}
	}
	if (previous && previous->policy == cfg->policy && previous->threshold_ppm >= 0 && previous->generation) {
		m.source = RSWAP_THRESHOLD_RETAINED;
		m.threshold_ppm = previous->threshold_ppm;
		m.generation = previous->generation;
	}
	return m;
}
#endif
