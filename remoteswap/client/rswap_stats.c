#include <linux/seq_file.h>

#include "rswap_rdma.h"
#include "rswap_mapping.h"
#include "rswap_placement.h"
#include "rswap_app.h"
#include "rswap_identity.h"
#include "rswap_vpn_heat.h"

static struct dentry *stats_root;
static unsigned int stats_nr_servers;
static u64 stats_instance_ns;
static atomic64_t unrouted_error = ATOMIC64_INIT(0);
static const char * const stats_op_names[NUM_QP_TYPE] = {
	"write", "read_sync", "read_async",
};
static const char * const stats_tier_names[RSWAP_NR_TIERS] = {
	"dram", "pmem",
};

void rswap_stats_wc(struct rdma_session_context *session,
		unsigned int tier, enum rdma_queue_type type, int status)
{
	struct rswap_io_stats *stats = &session->io_stats[tier][type];

	if (status)
		atomic64_inc(&stats->wc_error);
	else
		atomic64_inc(&stats->ok);
}

void rswap_stats_submit_error(struct rdma_session_context *session,
		unsigned int tier, enum rdma_queue_type type)
{
	if (!session || tier >= RSWAP_NR_TIERS || (unsigned int)type >= NUM_QP_TYPE)
		atomic64_inc(&unrouted_error);
	else
		atomic64_inc(&session->io_stats[tier][type].submit_error);
}

/* A live snapshot, not an atomic transaction across all counters. */
static int rswap_stats_show(struct seq_file *m, void *unused)
{
	unsigned int id, tier, type;

	seq_printf(m, "version=2 instance_ns=%llu sample_ns=%llu page_size=%lu\n",
		(unsigned long long)stats_instance_ns,
		(unsigned long long)ktime_get_ns(), PAGE_SIZE);
	seq_puts(m, "server tier op ok wc_error submit_error\n");
	for (id = 0; id < stats_nr_servers; id++) {
		for (tier = 0; tier < RSWAP_NR_TIERS; tier++) {
			for (type = 0; type < NUM_QP_TYPE; type++) {
				struct rswap_io_stats *stats = &rdma_sessions[id].io_stats[tier][type];

				seq_printf(m, "%u %s %s %llu %llu %llu\n", id,
					stats_tier_names[tier], stats_op_names[type],
					(unsigned long long)atomic64_read(&stats->ok),
					(unsigned long long)atomic64_read(&stats->wc_error),
					(unsigned long long)atomic64_read(&stats->submit_error));
			}
		}
	}
	seq_printf(m, "unrouted_error=%llu\n",
		(unsigned long long)atomic64_read(&unrouted_error));
	return 0;
}

static int rswap_stats_open(struct inode *inode, struct file *file)
{
	/* The file exists before final Frontswap registration can still fail. */
	if (READ_ONCE(THIS_MODULE->state) != MODULE_STATE_LIVE)
		return -ENODEV;
	return single_open(file, rswap_stats_show, NULL);
}

static const struct file_operations rswap_stats_fops = {
	.owner = THIS_MODULE,
	.open = rswap_stats_open,
	.read = seq_read,
	.llseek = seq_lseek,
	.release = single_release,
};

/* Display binary capacities using the requested KB..GB labels. Choose units
 * before rounding: exactly 1024 stays in the current unit; GB is the ceiling.
 * Integer arithmetic avoids floating point and rounding the input twice.
 */
static void rswap_seq_capacity(struct seq_file *m, u64 bytes)
{
	static const char * const units[] = { "KB", "MB", "GB" };
	unsigned int shift = 10;
	u64 tenths;

	while (shift < 30 && bytes > (1ULL << (shift + 10)))
		shift += 10;
	tenths = (bytes >> shift) * 10 +
		(((bytes & ((1ULL << shift) - 1)) * 10 + (1ULL << (shift - 1))) >> shift);
	seq_printf(m, "%llu.%u%s", (unsigned long long)(tenths / 10),
		(unsigned int)(tenths % 10), units[shift / 10 - 1]);
}

static int rswap_mapping_show(struct seq_file *m, void *unused)
{
	struct rswap_mapping_stats stats;
	unsigned int s, t;

	rswap_mapping_snapshot(&stats);
	seq_puts(m, "version=2 capacity=");
	rswap_seq_capacity(m, stats.pages << PAGE_SHIFT);
	seq_puts(m, " metadata=");
	rswap_seq_capacity(m, stats.metadata_bytes);
	seq_printf(m, " sequence=%llu\n", (unsigned long long)stats.sequence);
	seq_puts(m, "server tier capacity used free\n");
	for (s = 0; s < stats.nr_servers; s++) {
		for (t = 0; t < RSWAP_NR_TIERS; t++) {
			const struct rswap_mapping_pool_stats *p = &stats.pools[s][t];

			seq_printf(m, "%u %s ", s, stats_tier_names[t]);
			rswap_seq_capacity(m, p->capacity << PAGE_SHIFT);
			seq_puts(m, " ");
			rswap_seq_capacity(m, (p->live + p->retired) << PAGE_SHIFT);
			seq_puts(m, " ");
			rswap_seq_capacity(m, p->free << PAGE_SHIFT);
			seq_puts(m, "\n");
		}
	}
	return 0;
}

/* Preserve exact page/byte counts for collectors and archived-log parsers. */
static int rswap_mapping_raw_show(struct seq_file *m, void *unused)
{
	struct rswap_mapping_stats stats;
	unsigned int s, t;

	rswap_mapping_snapshot(&stats);
	seq_printf(m, "version=1 pages=%llu metadata_bytes=%llu sequence=%llu\n",
		(unsigned long long)stats.pages,
		(unsigned long long)stats.metadata_bytes,
		(unsigned long long)stats.sequence);
	seq_puts(m, "server tier capacity live retired free\n");
	for (s = 0; s < stats.nr_servers; s++) {
		for (t = 0; t < RSWAP_NR_TIERS; t++) {
			struct rswap_mapping_pool_stats *p = &stats.pools[s][t];

			seq_printf(m, "%u %s %llu %llu %llu %llu\n", s,
				stats_tier_names[t], (unsigned long long)p->capacity,
				(unsigned long long)p->live, (unsigned long long)p->retired,
				(unsigned long long)p->free);
		}
	}
	return 0;
}

static int rswap_mapping_open(struct inode *inode, struct file *file)
{
	if (READ_ONCE(THIS_MODULE->state) != MODULE_STATE_LIVE)
		return -ENODEV;
	return single_open(file, rswap_mapping_show, NULL);
}

static const struct file_operations rswap_mapping_fops = {
	.owner = THIS_MODULE,
	.open = rswap_mapping_open,
	.read = seq_read,
	.llseek = seq_lseek,
	.release = single_release,
};

static int rswap_mapping_raw_open(struct inode *inode, struct file *file)
{
	if (READ_ONCE(THIS_MODULE->state) != MODULE_STATE_LIVE)
		return -ENODEV;
	return single_open(file, rswap_mapping_raw_show, NULL);
}

static const struct file_operations rswap_mapping_raw_fops = {
	.owner = THIS_MODULE,
	.open = rswap_mapping_raw_open,
	.read = seq_read,
	.llseek = seq_lseek,
	.release = single_release,
};

/* Validated MR descriptors are immutable for the Session lifetime. The open
 * file pins the module; stats_exit removes this interface before Session
 * storage is released. These are Server virtual addresses, not local pointers.
 */
static int rswap_address_map_show(struct seq_file *m, void *unused)
{
	static const char * const tier_names[] = { "DRAM", "PMem" };
	unsigned int tier, id, i;

	seq_printf(m, "version=1 instance_ns=%llu nr_servers=%u address_space=remote_virtual range_format=[start,end)\n",
		(unsigned long long)stats_instance_ns, stats_nr_servers);
	for (tier = 0; tier < RSWAP_NR_TIERS; tier++) {
		seq_printf(m, "\n%s\n", tier_names[tier]);
		for (id = 0; id < stats_nr_servers; id++) {
			const struct rdma_session_context *session = &rdma_sessions[id];
			const struct chunk_list *pool = &session->remote_mem_pool;
			unsigned int first = tier == RSWAP_TIER_DRAM ? 0 : pool->dram_chunks;
			unsigned int count = tier == RSWAP_TIER_DRAM ?
				pool->dram_chunks : pool->pmem_chunks;

			seq_printf(m, "  memory pool %u: server=%u endpoint=%pI4:%u chunks=%u capacity_gib=%llu\n",
				id, session->server_id, session->addr,
				(unsigned int)ntohs(session->port), count,
				(unsigned long long)count * REGION_SIZE_GB);
			for (i = first; i < first + count; i++) {
				const struct remote_chunk *chunk = &pool->chunks[i];

				if (chunk->chunk_state != MAPPED)
					continue;
				/* Discovery checked the complete range for overflow. */
				seq_printf(m, "    chunk=%u start=0x%016llx end=0x%016llx size=%llu GiB bytes=%llu\n",
					i, (unsigned long long)chunk->remote_addr,
					(unsigned long long)(chunk->remote_addr + chunk->mapped_size),
					(unsigned long long)(chunk->mapped_size >> GB_SHIFT),
					(unsigned long long)chunk->mapped_size);
			}
		}
	}
	return 0;
}

static int rswap_address_map_open(struct inode *inode, struct file *file)
{
	if (READ_ONCE(THIS_MODULE->state) != MODULE_STATE_LIVE)
		return -ENODEV;
	return single_open(file, rswap_address_map_show, NULL);
}

static const struct file_operations rswap_address_map_fops = {
	.owner = THIS_MODULE,
	.open = rswap_address_map_open,
	.read = seq_read,
	.llseek = seq_lseek,
	.release = single_release,
};

int rswap_stats_init(unsigned int nr_servers)
{
	struct dentry *file;
	unsigned int id, tier, type;
	int ret;

	/* Called once before Frontswap registration, never while data I/O runs. */
	stats_nr_servers = nr_servers;
	stats_instance_ns = ktime_get_ns();
	rswap_policy_stats_init(stats_instance_ns);
	atomic64_set(&unrouted_error, 0);
	for (id = 0; id < nr_servers; id++) {
		for (tier = 0; tier < RSWAP_NR_TIERS; tier++) {
			for (type = 0; type < NUM_QP_TYPE; type++) {
				struct rswap_io_stats *stats = &rdma_sessions[id].io_stats[tier][type];

				atomic64_set(&stats->ok, 0);
				atomic64_set(&stats->wc_error, 0);
				atomic64_set(&stats->submit_error, 0);
			}
		}
	}
	stats_root = debugfs_create_dir("rswap", NULL);
	if (IS_ERR_OR_NULL(stats_root)) {
		ret = stats_root ? PTR_ERR(stats_root) : -ENOMEM;
		stats_root = NULL;
		return ret;
	}
	file = debugfs_create_file("stats", 0444, stats_root, NULL, &rswap_stats_fops);
	if (IS_ERR_OR_NULL(file)) {
		ret = file ? PTR_ERR(file) : -ENOMEM;
		rswap_stats_exit();
		return ret;
	}
	file = debugfs_create_file("mapping", 0444, stats_root, NULL, &rswap_mapping_fops);
	if (IS_ERR_OR_NULL(file)) {
		ret = file ? PTR_ERR(file) : -ENOMEM;
		rswap_stats_exit();
		return ret;
	}
	file = debugfs_create_file("mapping_raw", 0444, stats_root, NULL, &rswap_mapping_raw_fops);
	if (IS_ERR_OR_NULL(file)) {
		ret = file ? PTR_ERR(file) : -ENOMEM;
		rswap_stats_exit();
		return ret;
	}
	file = debugfs_create_file("address_map", 0400, stats_root, NULL, &rswap_address_map_fops);
	if (IS_ERR_OR_NULL(file)) {
		ret = file ? PTR_ERR(file) : -ENOMEM;
		rswap_stats_exit();
		return ret;
	}
	ret = rswap_placement_debugfs(stats_root);
	if (!ret)
		ret = rswap_app_debugfs(stats_root);
	if (!ret)
		ret = rswap_identity_debugfs(stats_root);
	if (!ret)
		ret = rswap_vpn_heat_debugfs(stats_root);
	if (ret)
		rswap_stats_exit();
	return ret;
}

void rswap_stats_exit(void)
{
	debugfs_remove_recursive(stats_root);
	stats_root = NULL;
}
