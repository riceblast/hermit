// SPDX-License-Identifier: GPL-2.0
#include <linux/capability.h>
#include <linux/debugfs.h>
#include <linux/ktime.h>
#include <linux/module.h>
#include <linux/mm.h>
#include <linux/mutex.h>
#include <linux/rcupdate.h>
#include <linux/slab.h>
#include <linux/uaccess.h>
#include <linux/vmalloc.h>

#include "rswap_identity.h"
#include "rswap_vpn_heat.h"

struct vpn_snapshot {
	u64 window, publish_ns, count, pages, sum_heat;
	struct rswap_vpn_heat_record records[];
};

struct vpn_uploader {
	u64 binding_id, received, received_pages, received_sum, query_vpn;
	bool used, queried;
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
	rcu_assign_pointer(vpn_heat_owner, NULL);
	synchronize_rcu();
	s = rcu_dereference_protected(u->snapshot, 1);
	RCU_INIT_POINTER(u->snapshot, NULL);
	kvfree(s);
	kvfree(u->staging);
	u->staging = NULL;
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
	if (cmd->op == RSWAP_VPN_HEAT_BIND) {
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
	    cmd.op > RSWAP_VPN_HEAT_QUERY)
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
