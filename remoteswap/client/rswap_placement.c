// SPDX-License-Identifier: GPL-2.0
#include <linux/capability.h>
#include <linux/debugfs.h>
#include <linux/ktime.h>
#include <linux/mm.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/rcupdate.h>
#include <linux/seq_file.h>
#include <linux/slab.h>
#include <linux/uaccess.h>
#include <linux/vmalloc.h>

#include "rswap_placement.h"

struct rswap_heat_snapshot {
	u64 generation, publish_ns, count, known;
	int mean;
	struct rswap_heat_record records[];
};

struct rswap_policy_run {
	u64 id, start_ns, warmup_ns;
	bool manual, forced;
	atomic64_t rr[RSWAP_NR_TIERS];
	struct rswap_heat_snapshot __rcu *snapshot;
};

struct rswap_uploader {
	u64 run_id, received;
	struct rswap_heat_snapshot *staging;
};

/* All writers, including fd release and area teardown, are serialized here.
 * Readers hold RCU only while copying a decision, never across mapping or I/O.
 * Retired arrays are freed in the writer's process context after a grace period.
 */
static DEFINE_MUTEX(policy_mutex);
static struct rswap_policy_run __rcu *policy_run;
static struct rswap_uploader *policy_owner;
static struct rswap_pool_layout policy_layout;
static u64 policy_next_id;
static bool policy_area;

/* Module-lifetime counters, deliberately independent of run/snapshot storage.
 * Stop may retire a run before an old Store WC arrives. Never retain its pointer.
 * Readers see independently atomic counters; reconcile only after I/O drains.
 */
static u64 policy_instance_ns;
static struct {
	atomic64_t decisions, post_ok, post_error, wc_ok, wc_error;
} policy_counts[RSWAP_REASON_COUNT];
static atomic64_t policy_preferred[RSWAP_MAX_SERVERS][RSWAP_NR_TIERS];
static atomic64_t policy_allocated[RSWAP_MAX_SERVERS][RSWAP_NR_TIERS];
static atomic64_t policy_fallback[4], policy_age[4], policy_alloc_error;

void rswap_policy_stats_init(u64 instance_ns)
{
	unsigned int r, s, t;

	policy_instance_ns = instance_ns;
	for (r = 0; r < RSWAP_REASON_COUNT; r++) {
		atomic64_set(&policy_counts[r].decisions, 0);
		atomic64_set(&policy_counts[r].post_ok, 0);
		atomic64_set(&policy_counts[r].post_error, 0);
		atomic64_set(&policy_counts[r].wc_ok, 0);
		atomic64_set(&policy_counts[r].wc_error, 0);
	}
	for (s = 0; s < RSWAP_MAX_SERVERS; s++)
		for (t = 0; t < RSWAP_NR_TIERS; t++) {
			atomic64_set(&policy_preferred[s][t], 0);
			atomic64_set(&policy_allocated[s][t], 0);
		}
	for (r = 0; r < 4; r++) {
		atomic64_set(&policy_fallback[r], 0);
		atomic64_set(&policy_age[r], 0);
	}
	atomic64_set(&policy_alloc_error, 0);
}

void rswap_policy_record_alloc(const struct rswap_decision *d,
		const struct rswap_location *loc, int status)
{
	unsigned int bucket;
	u64 age;

	atomic64_inc(&policy_counts[d->reason].decisions);
	atomic64_inc(&policy_preferred[d->server][d->tier]);
	if (d->state == RSWAP_ADAPTIVE && d->generation) {
		age = d->decision_ns - d->publish_ns;
		bucket = age <= NSEC_PER_SEC ? 0 : age <= 5ULL * NSEC_PER_SEC ? 1 :
			age <= 20ULL * NSEC_PER_SEC ? 2 : 3;
		atomic64_inc(&policy_age[bucket]);
	}
	if (status) {
		atomic64_inc(&policy_alloc_error);
		return;
	}
	atomic64_inc(&policy_allocated[loc->server][loc->tier]);
	atomic64_inc(&policy_fallback[(loc->server ^ d->server) + 2 * (loc->tier ^ d->tier)]);
}

void rswap_policy_record_submit(unsigned int reason, int status)
{
	if (status)
		atomic64_inc(&policy_counts[reason].post_error);
	else
		atomic64_inc(&policy_counts[reason].post_ok);
}

void rswap_policy_record_wc(unsigned int reason, int status)
{
	if (status)
		atomic64_inc(&policy_counts[reason].wc_error);
	else
		atomic64_inc(&policy_counts[reason].wc_ok);
}

static unsigned int rswap_policy_phase(const struct rswap_policy_run *run, u64 now)
{
	if (!run)
		return RSWAP_INACTIVE;
	if (now - run->start_ns < run->warmup_ns ||
	    (run->manual && !READ_ONCE(run->forced)))
		return RSWAP_WARMUP;
	return RSWAP_ADAPTIVE;
}

/* policy_mutex held. Existing mappings and RDMA requests are untouched. */
static void rswap_policy_stop(void)
{
	struct rswap_policy_run *run = rcu_dereference_protected(policy_run, 1);

	rcu_assign_pointer(policy_run, NULL);
	if (policy_owner) {
		kvfree(policy_owner->staging);
		policy_owner->staging = NULL;
		policy_owner = NULL;
	}
	if (run) {
		synchronize_rcu();
		kvfree(rcu_dereference_protected(run->snapshot, 1));
		kfree(run);
	}
}

void rswap_placement_init(const struct rswap_pool_layout *layout)
{
	policy_layout = *layout;
}

void rswap_placement_area(bool active)
{
	mutex_lock(&policy_mutex);
	policy_area = active;
	if (!active)
		rswap_policy_stop();
	mutex_unlock(&policy_mutex);
}

void rswap_placement_exit(void)
{
	rswap_placement_area(false);
	memset(&policy_layout, 0, sizeof(policy_layout));
}

int rswap_placement_choose(u64 offset, u64 pfn, struct rswap_decision *d)
{
	struct rswap_policy_run *run;
	struct rswap_heat_snapshot *snap;
	u64 ignored, lo, hi;
	int ret;

	memset(d, 0, sizeof(*d));
	d->heat = d->mean = -1;
	ret = rswap_map_page(&policy_layout, offset, &d->server, &d->tier, &ignored);
	if (ret)
		return ret;
	rcu_read_lock();
	run = rcu_dereference(policy_run);
	snap = run ? rcu_dereference(run->snapshot) : NULL;
	/* Capture time after acquiring the view: a newly started run cannot have
	 * a start_ns later than this decision's timestamp.
	 */
	d->decision_ns = ktime_get_ns();
	d->state = rswap_policy_phase(run, d->decision_ns);
	if (!run)
		goto out;
	d->run_id = run->id;
	d->reason = RSWAP_REASON_WARMUP;
	if (snap) {
		d->generation = snap->generation;
		d->publish_ns = snap->publish_ns;
		d->mean = snap->mean;
	}
	if (d->state == RSWAP_WARMUP)
		goto out;
	d->reason = RSWAP_REASON_NO_SNAPSHOT;
	if (!snap)
		goto out;
	d->reason = RSWAP_REASON_NO_MEAN;
	if (snap->mean < 0)
		goto out;
	d->reason = RSWAP_REASON_UNKNOWN;
	lo = 0;
	hi = snap->count;
	while (lo < hi) {
		u64 mid = lo + (hi - lo) / 2;
		const struct rswap_heat_record *record = &snap->records[mid];

		if (record->pfn < pfn)
			lo = mid + 1;
		else if (record->pfn > pfn)
			hi = mid;
		else {
			d->heat = record->heat_ppm;
			d->reason = d->heat > d->mean ? RSWAP_REASON_HOT : RSWAP_REASON_COLD;
			d->tier = d->heat > d->mean ? RSWAP_TIER_DRAM : RSWAP_TIER_PMEM;
			d->rr = (u64)atomic64_inc_return(&run->rr[d->tier]) - 1;
			d->server = d->rr % policy_layout.nr_servers;
			break;
		}
	}
out:
	rcu_read_unlock();
	return 0;
}

static int rswap_upload_open(struct inode *inode, struct file *file)
{
	if (!capable(CAP_SYS_ADMIN))
		return -EPERM;
	if (READ_ONCE(THIS_MODULE->state) != MODULE_STATE_LIVE)
		return -ENODEV;
	file->private_data = kzalloc(sizeof(struct rswap_uploader), GFP_KERNEL);
	return file->private_data ? 0 : -ENOMEM;
}

static int rswap_upload_release(struct inode *inode, struct file *file)
{
	struct rswap_uploader *u = file->private_data;

	mutex_lock(&policy_mutex);
	if (policy_owner == u)
		rswap_policy_stop();
	kvfree(u->staging);
	mutex_unlock(&policy_mutex);
	kfree(u);
	return 0;
}

static ssize_t rswap_upload_read(struct file *file, char __user *buf,
		size_t len, loff_t *pos)
{
	struct rswap_uploader *u = file->private_data;
	struct rswap_upload_ack ack = {};
	struct rswap_policy_run *run;
	struct rswap_heat_snapshot *snap;

	mutex_lock(&policy_mutex);
	run = rcu_dereference_protected(policy_run, 1);
	if (policy_owner != u || !run) {
		mutex_unlock(&policy_mutex);
		return -ESTALE;
	}
	ack.run_id = run->id;
	ack.start_ns = run->start_ns;
	snap = rcu_dereference_protected(run->snapshot, 1);
	if (snap) {
		ack.generation = snap->generation;
		ack.publish_ns = snap->publish_ns;
	}
	mutex_unlock(&policy_mutex);
	return simple_read_from_buffer(buf, len, pos, &ack, sizeof(ack));
}

static int rswap_upload_start(struct rswap_uploader *u,
		const struct rswap_upload_command *cmd)
{
	struct rswap_policy_run *run;
	unsigned int t;

	if (cmd->generation || cmd->count > 86400ULL * NSEC_PER_SEC ||
	    cmd->value > 1 || cmd->mean_ppm)
		return -EINVAL;
	if (!policy_area)
		return -ENODEV;
	if (rcu_access_pointer(policy_run))
		return -EBUSY;
	/* One run per fd: delayed commands on an old descriptor can never join a
	 * later Start, even if another thread retained that descriptor.
	 */
	if (u->run_id)
		return -ESTALE;
	run = kzalloc(sizeof(*run), GFP_KERNEL);
	if (!run)
		return -ENOMEM;
	run->id = ++policy_next_id;
	run->start_ns = ktime_get_ns();
	run->warmup_ns = cmd->count;
	run->manual = cmd->value;
	for (t = 0; t < RSWAP_NR_TIERS; t++)
		atomic64_set(&run->rr[t], 0);
	u->run_id = run->id;
	policy_owner = u;
	rcu_assign_pointer(policy_run, run);
	return 0;
}

static int rswap_upload_command(struct rswap_uploader *u,
		const struct rswap_upload_command *cmd, const char __user *data)
{
	struct rswap_policy_run *run = rcu_dereference_protected(policy_run, 1);
	struct rswap_heat_snapshot *snap, *old;
	u64 i;

	if (cmd->op == RSWAP_UPLOAD_START)
		return rswap_upload_start(u, cmd);
	if (!run || policy_owner != u || u->run_id != run->id)
		return -ESTALE;
	old = rcu_dereference_protected(run->snapshot, 1);
	if (!cmd->generation || (old && cmd->generation <= old->generation))
		return -ESTALE;
	if (cmd->op == RSWAP_UPLOAD_BEGIN) {
		if (u->staging)
			return -EBUSY;
		if (cmd->count > totalram_pages() || cmd->value < cmd->count ||
		    (cmd->value ? cmd->mean_ppm < 0 || cmd->mean_ppm > RSWAP_HEAT_SCALE :
			cmd->mean_ppm != -1) ||
		    cmd->count > (SIZE_MAX - sizeof(*snap)) / sizeof(snap->records[0]))
			return -EINVAL;
		snap = kvzalloc(sizeof(*snap) + cmd->count * sizeof(snap->records[0]), GFP_KERNEL);
		if (!snap)
			return -ENOMEM;
		snap->count = cmd->count;
		snap->known = cmd->value;
		snap->mean = cmd->mean_ppm;
		snap->generation = cmd->generation;
		u->staging = snap;
		u->received = 0;
		return 0;
	}
	snap = u->staging;
	if (!snap || snap->generation != cmd->generation || cmd->value || cmd->mean_ppm)
		return -EINVAL;
	if (cmd->op == RSWAP_UPLOAD_DATA) {
		if (cmd->count > snap->count - u->received)
			return -EINVAL;
		if (copy_from_user(snap->records + u->received, data,
				cmd->count * sizeof(snap->records[0])))
			return -EFAULT;
		for (i = u->received; i < u->received + cmd->count; i++) {
			const struct rswap_heat_record *r = &snap->records[i];

			if (!r->pfn || r->reserved || r->heat_ppm > RSWAP_HEAT_SCALE ||
			    (i && r->pfn <= snap->records[i - 1].pfn))
				return -EINVAL;
		}
		u->received += cmd->count;
		return 0;
	}
	if (cmd->op != RSWAP_UPLOAD_COMMIT || cmd->count || u->received != snap->count)
		return -EINVAL;
	snap->publish_ns = ktime_get_ns();
	rcu_assign_pointer(run->snapshot, snap);
	u->staging = NULL;
	synchronize_rcu();
	kvfree(old);
	return 0;
}

static ssize_t rswap_upload_write(struct file *file, const char __user *buf,
		size_t len, loff_t *pos)
{
	struct rswap_upload_command cmd;
	struct rswap_uploader *u = file->private_data;
	size_t records = 0;
	int ret = -EINVAL;

	if (len < sizeof(cmd))
		goto discard;
	if (copy_from_user(&cmd, buf, sizeof(cmd))) {
		ret = -EFAULT;
		goto discard;
	}
	if (cmd.version != RSWAP_PLACEMENT_VERSION || cmd.reserved ||
	    cmd.op < RSWAP_UPLOAD_START || cmd.op > RSWAP_UPLOAD_COMMIT)
		goto discard;
	if (cmd.op == RSWAP_UPLOAD_DATA) {
		if (!cmd.count || cmd.count > RSWAP_UPLOAD_BATCH)
			goto discard;
		records = cmd.count;
	}
	if (len != sizeof(cmd) + records * sizeof(struct rswap_heat_record))
		goto discard;
	mutex_lock(&policy_mutex);
	ret = rswap_upload_command(u, &cmd, buf + sizeof(cmd));
	/* Invalid/incomplete transactions never change the published generation.
	 * Discard staging after any error so a caller can begin a fresh transaction.
	 */
	if (ret) {
		kvfree(u->staging);
		u->staging = NULL;
	}
	mutex_unlock(&policy_mutex);
	return ret ? (ssize_t)ret : (ssize_t)len;
discard:
	mutex_lock(&policy_mutex);
	kvfree(u->staging);
	u->staging = NULL;
	mutex_unlock(&policy_mutex);
	return ret;
}

static const struct file_operations rswap_upload_fops = {
	.owner = THIS_MODULE, .open = rswap_upload_open,
	.read = rswap_upload_read, .write = rswap_upload_write,
	.llseek = default_llseek, .release = rswap_upload_release,
};

static ssize_t rswap_control_write(struct file *file, const char __user *buf,
		size_t len, loff_t *pos)
{
	struct rswap_policy_run *run;
	char command[16];
	int ret = 0;

	if (!capable(CAP_SYS_ADMIN))
		return -EPERM;
	if (!len || len >= sizeof(command))
		return -EINVAL;
	if (copy_from_user(command, buf, len))
		return -EFAULT;
	command[len] = 0;
	mutex_lock(&policy_mutex);
	run = rcu_dereference_protected(policy_run, 1);
	if (sysfs_streq(command, "stop")) {
		rswap_policy_stop();
	} else if (!sysfs_streq(command, "adaptive")) {
		ret = -EINVAL;
	} else if (!run) {
		ret = -ENODEV;
	} else if (ktime_get_ns() - run->start_ns < run->warmup_ns) {
		ret = -EAGAIN;
	} else {
		WRITE_ONCE(run->forced, true);
	}
	mutex_unlock(&policy_mutex);
	return ret ? (ssize_t)ret : (ssize_t)len;
}

static const struct file_operations rswap_control_fops = {
	.owner = THIS_MODULE, .write = rswap_control_write, .llseek = no_llseek,
};

static int rswap_policy_show(struct seq_file *m, void *unused)
{
	struct rswap_policy_run *run;
	struct rswap_heat_snapshot *snap;
	u64 now = ktime_get_ns();
	static const char *const states[] = { "inactive", "warmup", "adaptive" };

	mutex_lock(&policy_mutex);
	run = rcu_dereference_protected(policy_run, 1);
	/* Time must follow acquisition of a newly started run. */
	now = ktime_get_ns();
	seq_printf(m, "version=1 state=%s area_active=%u sample_ns=%llu\n",
		states[rswap_policy_phase(run, now)], policy_area, now);
	if (run) {
		snap = rcu_dereference_protected(run->snapshot, 1);
		seq_printf(m, "run_id=%llu start_ns=%llu warmup_ns=%llu mode=%s forced=%u\n",
			run->id, run->start_ns, run->warmup_ns,
			run->manual ? "manual" : "auto", run->forced);
		seq_printf(m, "generation=%llu publish_ns=%llu age_ns=%llu records=%llu known=%llu mean_ppm=%d metadata_bytes=%llu staging_bytes=%llu\n",
			snap ? snap->generation : 0, snap ? snap->publish_ns : 0,
			snap ? now - snap->publish_ns : 0, snap ? snap->count : 0,
			snap ? snap->known : 0, snap ? snap->mean : -1,
			snap ? sizeof(*snap) + snap->count * sizeof(snap->records[0]) : 0,
			policy_owner && policy_owner->staging ? sizeof(*snap) +
				policy_owner->staging->count * sizeof(snap->records[0]) : 0);
	}
	mutex_unlock(&policy_mutex);
	return 0;
}

static int rswap_policy_open(struct inode *inode, struct file *file)
{
	if (READ_ONCE(THIS_MODULE->state) != MODULE_STATE_LIVE)
		return -ENODEV;
	return single_open(file, rswap_policy_show, NULL);
}

static const struct file_operations rswap_policy_fops = {
	.owner = THIS_MODULE, .open = rswap_policy_open, .read = seq_read,
	.llseek = seq_lseek, .release = single_release,
};

static int rswap_policy_stats_show(struct seq_file *m, void *unused)
{
	unsigned int r, s, t;

	seq_printf(m, "version=1 instance_ns=%llu sample_ns=%llu nr_servers=%u page_size=%lu\n",
		policy_instance_ns, ktime_get_ns(), policy_layout.nr_servers, PAGE_SIZE);
	for (r = 0; r < RSWAP_REASON_COUNT; r++)
		seq_printf(m, "reason %u %lld %lld %lld %lld %lld\n", r,
			atomic64_read(&policy_counts[r].decisions),
			atomic64_read(&policy_counts[r].post_ok),
			atomic64_read(&policy_counts[r].post_error),
			atomic64_read(&policy_counts[r].wc_ok),
			atomic64_read(&policy_counts[r].wc_error));
	for (s = 0; s < policy_layout.nr_servers; s++)
		for (t = 0; t < RSWAP_NR_TIERS; t++)
			seq_printf(m, "pool %u %u %lld %lld\n", s, t,
				atomic64_read(&policy_preferred[s][t]),
				atomic64_read(&policy_allocated[s][t]));
	for (r = 0; r < 4; r++) {
		seq_printf(m, "fallback %u %lld\n", r, atomic64_read(&policy_fallback[r]));
		seq_printf(m, "age %u %lld\n", r, atomic64_read(&policy_age[r]));
	}
	seq_printf(m, "allocation_error %lld\n", atomic64_read(&policy_alloc_error));
	return 0;
}

static int rswap_policy_stats_open(struct inode *inode, struct file *file)
{
	if (READ_ONCE(THIS_MODULE->state) != MODULE_STATE_LIVE)
		return -ENODEV;
	return single_open(file, rswap_policy_stats_show, NULL);
}

static const struct file_operations rswap_policy_stats_fops = {
	.owner = THIS_MODULE, .open = rswap_policy_stats_open, .read = seq_read,
	.llseek = seq_lseek, .release = single_release,
};

int rswap_placement_debugfs(struct dentry *root)
{
	struct dentry *file;

	file = debugfs_create_file("policy_stats", 0400, root, NULL, &rswap_policy_stats_fops);
	if (IS_ERR_OR_NULL(file))
		return file ? PTR_ERR(file) : -ENOMEM;
	file = debugfs_create_file("placement", 0400, root, NULL, &rswap_policy_fops);
	if (IS_ERR_OR_NULL(file))
		return file ? PTR_ERR(file) : -ENOMEM;
	file = debugfs_create_file("hotness", 0600, root, NULL, &rswap_upload_fops);
	if (IS_ERR_OR_NULL(file))
		return file ? PTR_ERR(file) : -ENOMEM;
	file = debugfs_create_file("placement_control", 0200, root, NULL, &rswap_control_fops);
	return IS_ERR_OR_NULL(file) ? (file ? PTR_ERR(file) : -ENOMEM) : 0;
}
