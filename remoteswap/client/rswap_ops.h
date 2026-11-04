#ifndef __RSWAP_OPS_H
#define __RSWAP_OPS_H

#include <linux/types.h>

#define RSWAP_NUM_SERVERS 2

struct rswap_endpoint {
	const char *ip;
	u16 port;
};

struct rswap_client_config {
	struct rswap_endpoint servers[RSWAP_NUM_SERVERS];
	unsigned int size_gb; /* Total capacity, not per-server capacity. */
	unsigned int timeout_ms; /* Deadline for each complete session setup. */
};

int rswap_client_init(const struct rswap_client_config *config);
void rswap_client_exit(void);
int rswap_register_frontswap(void);
void rswap_deregister_frontswap(void);

#endif
