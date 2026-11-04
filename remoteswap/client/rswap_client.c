#include <linux/module.h>
#include <linux/inet.h>
#include <linux/frontswap.h>

#include "rswap_ops.h"
#include "constants.h"

MODULE_AUTHOR("Chenxi Wang, Yifan Qiao");
MODULE_DESCRIPTION("Hermit dual-server remote memory pool (Patch 1 connection tests)");
MODULE_LICENSE("Dual BSD/GPL");
MODULE_VERSION("1.1");

static char server_ip[INET_ADDRSTRLEN];
static char server_ip2[INET_ADDRSTRLEN];
static int server_port = 9400;
static int server_port2 = 9400;
static int remote_mem_size = 16;
static unsigned int connect_timeout_ms = 30000;

module_param_string(sip, server_ip, sizeof(server_ip), 0444);
module_param_string(sip2, server_ip2, sizeof(server_ip2), 0444);
module_param_named(sport, server_port, int, 0444);
module_param_named(sport2, server_port2, int, 0444);
module_param_named(rmsize, remote_mem_size, int, 0444);
module_param(connect_timeout_ms, uint, 0444);
MODULE_PARM_DESC(sip, "Server 0 IPv4 address");
MODULE_PARM_DESC(sip2, "Server 1 IPv4 address");
MODULE_PARM_DESC(sport, "Server 0 port");
MODULE_PARM_DESC(sport2, "Server 1 port");
MODULE_PARM_DESC(rmsize, "Total remote capacity in GiB; two equal multiples of 8 GiB");
MODULE_PARM_DESC(connect_timeout_ms, "Per-session setup deadline in milliseconds (100..300000)");

static int __init rswap_cpu_init(void)
{
	struct rswap_client_config config;
	u8 addr[4];
	int ret;

	if (remote_mem_size <= 0 || remote_mem_size % (2 * REGION_SIZE_GB) ||
	    remote_mem_size / (2 * REGION_SIZE_GB) > MAX_REGION_NUM ||
	    server_port <= 0 || server_port > 65535 ||
	    server_port2 <= 0 || server_port2 > 65535 ||
	    connect_timeout_ms < 100 || connect_timeout_ms > 300000 ||
	    !in4_pton(server_ip, -1, addr, '\0', NULL) ||
	    !in4_pton(server_ip2, -1, addr, '\0', NULL))
		return -EINVAL;
	if (!strcmp(server_ip, server_ip2) && server_port == server_port2)
		return -EINVAL;

	config.servers[0].ip = server_ip;
	config.servers[0].port = server_port;
	config.servers[1].ip = server_ip2;
	config.servers[1].port = server_port2;
	config.size_gb = remote_mem_size;
	config.timeout_ms = connect_timeout_ms;
	ret = rswap_client_init(&config);
	if (ret)
		return ret;

	/* Publish only after every session and descriptor has been validated. */
	ret = rswap_register_frontswap();
	if (ret) {
		rswap_client_exit();
		return ret;
	}
	pr_info("rswap: Patch 1 ready; swapon is disabled until Patch 2/3\n");
	return 0;
}

static void __exit rswap_cpu_exit(void)
{
	/* Unlink and wait for synchronous backend calls before freeing RDMA. */
	rswap_deregister_frontswap();
	rswap_client_exit();
	pr_info("rswap: client unloaded\n");
}

module_init(rswap_cpu_init);
module_exit(rswap_cpu_exit);
