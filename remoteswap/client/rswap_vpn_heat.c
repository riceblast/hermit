// SPDX-License-Identifier: GPL-2.0
#include <linux/capability.h>
#include <linux/debugfs.h>
#include <linux/ktime.h>
#include <linux/module.h>
#include <linux/mm.h>
#include <linux/mutex.h>
#include <linux/rcupdate.h>
#include <linux/slab.h>
#include <linux/seq_file.h>
#include <linux/uaccess.h>
#include <linux/vmalloc.h>

#include "rswap_identity.h"
#include "rswap_vpn_heat.h"
#include "rswap_placement.h"

struct vpn_snapshot {
	u64 window, publish_ns, count, pages, sum_heat;
	struct rswap_vpn_heat_record records[];
};

struct vpn_uploader {
	u64 binding_id, received, received_pages, received_sum, query_vpn;
	bool used, queried;
	bool placement;
	u64 start_ns;
	atomic64_t rr[RSWAP_NR_TIERS];
	struct vpn_snapshot __rcu *snapshot;
	struct vpn_snapshot *staging;
};

static DEFINE_MUTEX(vpn_heat_mutex);
static struct vpn_uploader __rcu *vpn_heat_owner;
static bool vpn_heat_active;

/* Writer mutex held. Retire before waiting; readers only copy scalar values. */
static void vpn_retire(void)
{
	struct vpn_uploader *u = rcu_dereference_protected(vpn_heat_owner, 1);
	struct vpn_snapshot *s;

	if (!u)
		return;
	if (u->placement) {
		WRITE_ONCE(u->placement, false);
		rswap_policy_release_vpn(u->binding_id);
	}
	rcu_assign_pointer(vpn_heat_owner, NULL);
	synchronize_rcu();
	s = rcu_dereference_protected(u->snapshot, 1);
	RCU_INIT_POINTER(u->snapshot, NULL);
	kvfree(s);
	kvfree(u->staging);
	u->staging = NULL;
}

static u64 vpn_heat_at(const struct vpn_snapshot *s, u64 vpn)
{
	u64 lo = 0, hi = s ? s->count : 0;

	while (lo < hi) {
		u64 mid = lo + (hi - lo) / 2;
		const struct rswap_vpn_heat_record *r = &s->records[mid];

		if (r->vpn < vpn)
			lo = mid + 1;
		else if (r->vpn > vpn)
			hi = mid;
		else
			return r->heat;
	}
	return 0;
}

struct vpn_reduction {
	const struct vpn_snapshot *snapshot;
	u64 heat, vpn;
	bool seen;
};

/* Identity entry lock held. Only binary searches in one immutable snapshot. */
static void vpn_max_alias(void *arg, u64 vpn)
{
	struct vpn_reduction *r = arg;
	u64 heat = vpn_heat_at(r->snapshot, vpn);

	if (!r->seen || heat > r->heat || (heat == r->heat && vpn < r->vpn)) {
		r->seen = true;
		r->heat = heat;
		r->vpn = vpn;
	}
}

int rswap_vpn_placement_choose(unsigned int type, __u64 offset,
		const struct rswap_pool_layout *layout, struct rswap_decision *d)
{
	struct vpn_uploader *u;
	struct vpn_snapshot *s;
	struct rswap_identity_result id;
	struct vpn_reduction reduction = {};
	u64 ignored;
	int ret = 0;

	rcu_read_lock();
	u = rcu_dereference(vpn_heat_owner);
	if (!u || !READ_ONCE(u->placement))
		goto out;
	memset(d, 0, sizeof(*d));
	d->vpn_policy = true;
	d->decision_ns = ktime_get_ns();
	ret = rswap_map_page(layout, offset, &d->server, &d->tier, &ignored);
	if (ret)
		goto out;
	ret = 1;
	/* Unmatched/invalid bindings keep normal Interleave. They are not cold
	 * target pages and must never fall through to legacy PFN unknown rules. */
	if (!rswap_identity_binding_valid(u->binding_id))
		goto out;
	s = rcu_dereference(u->snapshot);
	reduction.snapshot = s;
	if (rswap_identity_visit(type, offset, u->binding_id, vpn_max_alias, &reduction, &id))
		goto out;
	d->run_id = d->binding_id = id.binding_id;
	d->vpn = reduction.vpn;
	d->aliases = id.aliases;
	d->heat_q32 = reduction.heat;
	d->generation = s ? s->window : 0;
	d->publish_ns = s ? s->publish_ns : 0;
	d->decision_ns = ktime_get_ns();
	d->pages = s ? s->pages : 0;
	d->sum_heat = s ? s->sum_heat : 0;
	d->state = s && s->window >= 5 ? RSWAP_ADAPTIVE : RSWAP_WARMUP;
	d->reason = RSWAP_REASON_WARMUP;
	d->tier = RSWAP_TIER_PMEM;
	if (d->state == RSWAP_ADAPTIVE) {
		/* BEGIN bounds pages to U32_MAX, so this Q32 product fits u64.
		 * Empty population has mean=0, consistent with summary.tsv. */
		bool hot = d->heat_q32 * d->pages >= d->sum_heat;

		d->reason = hot ? RSWAP_REASON_HOT : RSWAP_REASON_COLD;
		d->tier = hot ? RSWAP_TIER_DRAM : RSWAP_TIER_PMEM;
	}
	d->rr = (u64)atomic64_inc_return(&u->rr[d->tier]) - 1;
	d->server = d->rr % layout->nr_servers;
out:
	rcu_read_unlock();
	return ret;
}

void rswap_vpn_placement_stop(void)
{
	struct vpn_uploader *u;

	mutex_lock(&vpn_heat_mutex);
	u = rcu_dereference_protected(vpn_heat_owner, 1);
	if (u && u->placement) {
		WRITE_ONCE(u->placement, false);
		rswap_policy_release_vpn(u->binding_id);
	}
	mutex_unlock(&vpn_heat_mutex);
}

bool rswap_vpn_placement_show(struct seq_file *m, bool raw)
{
	struct vpn_uploader *u;
	struct vpn_snapshot *s;
	u64 now, pages, sum, mean;
	bool shown = false;

	mutex_lock(&vpn_heat_mutex);
	u = rcu_dereference_protected(vpn_heat_owner, 1);
	if (!u || !u->placement || !rswap_identity_binding_valid(u->binding_id))
		goto out;
	s = rcu_dereference_protected(u->snapshot, 1);
	now = ktime_get_ns();
	pages = s ? s->pages : 0;
	sum = s ? s->sum_heat : 0;
	/* This human/status ppm value is never used in a Store comparison. */
	mean = pages ? (sum / pages) * 1000000 / RSWAP_VPN_HEAT_SCALE : 0;
	seq_printf(m, "version=%u state=%s area_active=1 sample_ns=%llu placement_rule=2 source=vpn\n",
		raw ? 1 : 2, s && s->window >= 5 ? "adaptive" : "warmup", now);
	seq_printf(m, "run_id=%llu binding_id=%llu start_ns=%llu warmup_windows=5 completed_windows=%llu\n",
		u->binding_id, u->binding_id, u->start_ns, s ? s->window : 0);
	seq_printf(m, "generation=%llu publish_ns=%llu age_ns=%llu records=%llu known=%llu pages=%llu sum_heat=%llu ",
		s ? s->window : 0, s ? s->publish_ns : 0, s ? now - s->publish_ns : 0,
		s ? s->count : 0, pages, pages, sum);
	if (raw)
		seq_printf(m, "mean_ppm=%llu\n", mean);
	else {
		u64 hundredths = pages ? ((sum / pages) * 100 + RSWAP_VPN_HEAT_SCALE / 2) / RSWAP_VPN_HEAT_SCALE : 0;

		seq_printf(m, "mean=%llu.%02llu\n", hundredths / 100, hundredths % 100);
	}
	shown = true;
out:
	mutex_unlock(&vpn_heat_mutex);
	return shown;
}

void rswap_vpn_heat_area(bool active)
{
	mutex_lock(&vpn_heat_mutex);
	vpn_heat_active = active;
	if (!active)
		vpn_retire();
	mutex_unlock(&vpn_heat_mutex);
}

int rswap_vpn_heat_lookup(__u64 binding_id, __u64 vpn,
		struct rswap_vpn_heat_ack *result)
{
	struct vpn_uploader *u;
	struct vpn_snapshot *s;
	u64 lo = 0, hi;
	int ret = -ESTALE;

	memset(result, 0, sizeof(*result));
	rcu_read_lock();
	u = rcu_dereference(vpn_heat_owner);
	if (!u || u->binding_id != binding_id ||
	    !rswap_identity_binding_valid(binding_id))
		goto out;
	result->binding_id = binding_id;
	ret = 0;
	s = rcu_dereference(u->snapshot);
	if (!s)
		goto out;
	result->window = s->window;
	result->publish_ns = s->publish_ns;
	result->records = s->count;
	result->pages = s->pages;
	result->sum_heat = s->sum_heat;
	result->has_snapshot = 1;
	hi = s->count;
	while (lo < hi) {
		u64 mid = lo + (hi - lo) / 2;
		const struct rswap_vpn_heat_record *r = &s->records[mid];

		if (r->vpn < vpn)
			lo = mid + 1;
		else if (r->vpn > vpn)
			hi = mid;
		else {
			result->found = 1;
			result->heat = r->heat;
			break;
		}
	}
out:
	rcu_read_unlock();
	return ret;
}

static int vpn_open(struct inode *inode, struct file *file)
{
	struct vpn_uploader *u;

	if (!capable(CAP_SYS_ADMIN))
		return -EPERM;
	if (READ_ONCE(THIS_MODULE->state) != MODULE_STATE_LIVE)
		return -ENODEV;
	u = kzalloc(sizeof(*u), GFP_KERNEL);
	if (!u)
		return -ENOMEM;
	file->private_data = u;
	return 0;
}

static int vpn_release(struct inode *inode, struct file *file)
{
	struct vpn_uploader *u = file->private_data;

	mutex_lock(&vpn_heat_mutex);
	if (rcu_access_pointer(vpn_heat_owner) == u)
		vpn_retire();
	kvfree(u->staging);
	mutex_unlock(&vpn_heat_mutex);
	kfree(u);
	return 0;
}

static int vpn_command(struct vpn_uploader *u,
		const struct rswap_vpn_heat_command *cmd, const char __user *data)
{
	struct vpn_uploader *owner = rcu_dereference_protected(vpn_heat_owner, 1);
	struct vpn_snapshot *s, *old;
	u64 i;

	if (!vpn_heat_active)
		return -ENODEV;
	if (!cmd->binding_id || !rswap_identity_binding_valid(cmd->binding_id))
		return -ESTALE;
	if (cmd->op == RSWAP_VPN_HEAT_BIND || cmd->op == RSWAP_VPN_HEAT_BIND_POLICY) {
		if (cmd->window || cmd->count || cmd->pages || cmd->sum_heat || cmd->vpn)
			return -EINVAL;
		if (u->used)
			return -ESTALE;
		if (owner && !rswap_identity_binding_valid(owner->binding_id)) {
			vpn_retire();
			owner = NULL;
		}
		if (owner)
			return -EBUSY;
		if (cmd->op == RSWAP_VPN_HEAT_BIND_POLICY) {
			int ret = rswap_policy_claim_vpn(cmd->binding_id);

			if (ret)
				return ret;
			u->placement = true;
			u->start_ns = ktime_get_ns();
			atomic64_set(&u->rr[0], 0);
			atomic64_set(&u->rr[1], 0);
		}
		u->binding_id = cmd->binding_id;
		u->used = true;
		rcu_assign_pointer(vpn_heat_owner, u);
		return 0;
	}
	if (owner != u || u->binding_id != cmd->binding_id)
		return -ESTALE;
	if (cmd->op == RSWAP_VPN_HEAT_QUERY) {
		if (cmd->window || cmd->count || cmd->pages || cmd->sum_heat)
			return -EINVAL;
		u->query_vpn = cmd->vpn;
		u->queried = true;
		return 0;
	}
	if (cmd->vpn)
		return -EINVAL;
	old = rcu_dereference_protected(u->snapshot, 1);
	if (old && cmd->window <= old->window)
		return -ESTALE;
	if (cmd->op == RSWAP_VPN_HEAT_BEGIN) {
		if (u->staging)
			return -EBUSY;
		if (u->placement && cmd->window != (old ? old->window + 1 : 0))
			return -EINVAL;
		/* Q32 heat sums fit u64 for at most U32_MAX current pages. */
		if (cmd->pages > U32_MAX || cmd->pages > cmd->count ||
		    cmd->count > (SIZE_MAX - sizeof(*s)) / sizeof(s->records[0]) ||
		    cmd->sum_heat > cmd->pages * RSWAP_VPN_HEAT_SCALE)
			return -EINVAL;
		s = kvzalloc(sizeof(*s) + cmd->count * sizeof(s->records[0]), GFP_KERNEL);
		if (!s)
			return -ENOMEM;
		s->window = cmd->window;
		s->count = cmd->count;
		s->pages = cmd->pages;
		s->sum_heat = cmd->sum_heat;
		u->staging = s;
		u->received = u->received_pages = u->received_sum = 0;
		return 0;
	}
	if (cmd->pages || cmd->sum_heat)
		return -EINVAL;
	s = u->staging;
	if (!s || s->window != cmd->window)
		return -EINVAL;
	if (cmd->op == RSWAP_VPN_HEAT_DATA) {
		if (cmd->count > s->count - u->received)
			return -EINVAL;
		if (copy_from_user(s->records + u->received, data,
				   cmd->count * sizeof(s->records[0])))
			return -EFAULT;
		for (i = u->received; i < u->received + cmd->count; i++) {
			const struct rswap_vpn_heat_record *r = &s->records[i];

			if (r->reserved || r->in_scope > 1 || r->heat > RSWAP_VPN_HEAT_SCALE ||
			    (i && r->vpn <= s->records[i - 1].vpn))
				return -EINVAL;
			if (r->in_scope) {
				if (++u->received_pages > s->pages || r->heat > s->sum_heat - u->received_sum)
					return -EINVAL;
				u->received_sum += r->heat;
			}
		}
		u->received += cmd->count;
		return 0;
	}
	if (cmd->op != RSWAP_VPN_HEAT_COMMIT || cmd->count || u->received != s->count ||
	    u->received_pages != s->pages || u->received_sum != s->sum_heat)
		return -EINVAL;
	s->publish_ns = ktime_get_ns();
	rcu_assign_pointer(u->snapshot, s);
	u->staging = NULL;
	synchronize_rcu();
	kvfree(old);
	return 0;
}

static ssize_t vpn_write(struct file *file, const char __user *buf,
		size_t len, loff_t *pos)
{
	struct vpn_uploader *u = file->private_data;
	struct rswap_vpn_heat_command cmd;
	size_t bytes = 0;
	int ret;

	if (!capable(CAP_SYS_ADMIN))
		return -EPERM;
	mutex_lock(&vpn_heat_mutex);
	ret = -EINVAL;
	if (len < sizeof(cmd))
		goto out;
	ret = -EFAULT;
	if (copy_from_user(&cmd, buf, sizeof(cmd)))
		goto out;
	ret = -EINVAL;
	if (cmd.version != RSWAP_VPN_HEAT_VERSION || cmd.op < RSWAP_VPN_HEAT_BIND ||
	    cmd.op > RSWAP_VPN_HEAT_BIND_POLICY)
		goto out;
	if (cmd.op == RSWAP_VPN_HEAT_DATA) {
		if (!cmd.count || cmd.count > RSWAP_VPN_HEAT_BATCH)
			goto out;
		bytes = cmd.count * sizeof(struct rswap_vpn_heat_record);
	}
	if (len != sizeof(cmd) + bytes)
		goto out;
	ret = vpn_command(u, &cmd, buf + sizeof(cmd));
out:
	if (ret) {
		kvfree(u->staging);
		u->staging = NULL;
	}
	mutex_unlock(&vpn_heat_mutex);
	return ret ? ret : (ssize_t)len;
}

static ssize_t vpn_read(struct file *file, char __user *buf,
		size_t len, loff_t *pos)
{
	struct vpn_uploader *u = file->private_data;
	struct rswap_vpn_heat_ack result;
	int ret;

	mutex_lock(&vpn_heat_mutex);
	ret = rcu_access_pointer(vpn_heat_owner) == u ?
		rswap_vpn_heat_lookup(u->binding_id, u->queried ? u->query_vpn : 0, &result) : -ESTALE;
	mutex_unlock(&vpn_heat_mutex);
	return ret ? ret : simple_read_from_buffer(buf, len, pos, &result, sizeof(result));
}

static const struct file_operations vpn_fops = {
	.owner = THIS_MODULE, .open = vpn_open, .release = vpn_release,
	.read = vpn_read, .write = vpn_write, .llseek = default_llseek,
};

int rswap_vpn_heat_debugfs(struct dentry *root)
{
	struct dentry *entry = debugfs_create_file("vpn_heat", 0600, root, NULL, &vpn_fops);

	return IS_ERR_OR_NULL(entry) ? (entry ? PTR_ERR(entry) : -ENOMEM) : 0;
}
