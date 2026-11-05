#include <linux/module.h>
#include <linux/inet.h>
#include <linux/frontswap.h>

#include "rswap_ops.h"
#include "constants.h"

MODULE_AUTHOR("Chenxi Wang, Yifan Qiao");
MODULE_DESCRIPTION("Hermit single/dual-server remote pool (Patch 2 static mapping)");
MODULE_LICENSE("Dual BSD/GPL");
MODULE_VERSION("1.2");

static char server_ip[INET_ADDRSTRLEN];
static char server_ip2[INET_ADDRSTRLEN];
static int server_port = 9400;
static int server_port2 = 9400;
static int remote_mem_size = 16;
static unsigned int nr_servers = 2;
static unsigned int connect_timeout_ms = 30000;

module_param_string(sip, server_ip, sizeof(server_ip), 0444);
module_param_string(sip2, server_ip2, sizeof(server_ip2), 0444);
module_param_named(sport, server_port, int, 0444);
module_param_named(sport2, server_port2, int, 0444);
module_param_named(rmsize, remote_mem_size, int, 0444);
module_param(nr_servers, uint, 0444);
module_param(connect_timeout_ms, uint, 0444);
MODULE_PARM_DESC(sip, "Server 0 IPv4 address");
MODULE_PARM_DESC(sip2, "Server 1 IPv4 address");
MODULE_PARM_DESC(sport, "Server 0 port");
MODULE_PARM_DESC(sport2, "Server 1 port");
MODULE_PARM_DESC(nr_servers, "Number of memory servers (1 or 2)");
MODULE_PARM_DESC(rmsize, "Expected sum of selected servers' capacities in GiB (multiple of 8)");
MODULE_PARM_DESC(connect_timeout_ms, "Per-session setup deadline in milliseconds (100..300000)");

static int __init rswap_cpu_init(void)
{
	struct rswap_client_config config = {};
	const char *ips[] = { server_ip, server_ip2 };
	int ports[] = { server_port, server_port2 };
	u8 addrs[RSWAP_MAX_SERVERS][4];
	unsigned int id;
	int ret;

	if (!nr_servers || nr_servers > RSWAP_MAX_SERVERS ||
	    remote_mem_size <= 0 || remote_mem_size % REGION_SIZE_GB ||
	    remote_mem_size < nr_servers * REGION_SIZE_GB ||
	    remote_mem_size > nr_servers * MAX_REGION_NUM * REGION_SIZE_GB ||
	    connect_timeout_ms < 100 || connect_timeout_ms > 300000)
		return -EINVAL;
	for (id = 0; id < nr_servers; id++) {
		if (ports[id] <= 0 || ports[id] > 65535 ||
		    !in4_pton(ips[id], -1, addrs[id], '\0', NULL))
			return -EINVAL;
		config.servers[id].ip = ips[id];
		config.servers[id].port = ports[id];
	}
	if (nr_servers == 2 && ports[0] == ports[1] &&
	    !memcmp(addrs[0], addrs[1], sizeof(addrs[0])))
		return -EINVAL;

	config.nr_servers = nr_servers;
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
	pr_info("rswap: client ready; swapon remains disabled pending Patch 3\n");
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
