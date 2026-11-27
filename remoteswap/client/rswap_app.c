// SPDX-License-Identifier: GPL-2.0
#include <linux/capability.h>
#include <linux/cgroup.h>
#include <linux/debugfs.h>
#include <linux/list.h>
#include <linux/memcontrol.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/pid.h>
#include <linux/rculist.h>
#include <linux/sched/task.h>
#include <linux/seq_file.h>
#include <linux/slab.h>
#include <linux/uaccess.h>

#include "rswap_app.h"

struct rswap_app_binding {
	struct list_head link;
	struct mem_cgroup *memcg;
	u64 id;
	atomic64_t rr;
	unsigned int pid, policy;
	bool used, registered;
};

static LIST_HEAD(app_bindings);
static DEFINE_MUTEX(app_mutex);
static unsigned int app_servers;
static bool app_active;
static u64 app_next_id;

/* Writers hold app_mutex. Keep the removed list links and memcg reference
 * intact until every Store reader has copied its decision.
 */
static void rswap_app_remove(struct rswap_app_binding *b)
{
	if (!b->registered)
		return;
	list_del_rcu(&b->link);
	b->registered = false;
	synchronize_rcu();
	css_put(&b->memcg->css);
	b->memcg = NULL;
}

void rswap_app_init(unsigned int nr_servers)
{
	app_servers = nr_servers;
}

void rswap_app_area(bool active)
{
	struct rswap_app_binding *b, *next;

	mutex_lock(&app_mutex);
	app_active = active;
	if (!active)
		list_for_each_entry_safe(b, next, &app_bindings, link)
			rswap_app_remove(b);
	mutex_unlock(&app_mutex);
}

bool rswap_app_choose(struct page *page, struct rswap_app_decision *d)
{
	struct rswap_app_binding *b;
	struct mem_cgroup *memcg;
	bool found = false;

	/* Store owns the page lock. Unmapping its PTEs did not remove this
	 * charge; no dependence on the broken cgroup Swap counters or IDs.
	 */
	memcg = page_memcg(page);
	if (!memcg)
		return false;
	rcu_read_lock();
	list_for_each_entry_rcu(b, &app_bindings, link) {
		if (b->memcg != memcg)
			continue;
		d->binding_id = b->id;
		d->policy = b->policy;
		d->tier = b->policy == RSWAP_APP_DRAM ? RSWAP_TIER_DRAM : RSWAP_TIER_PMEM;
		d->rr = (u64)atomic64_inc_return(&b->rr) - 1;
		d->server = d->rr % app_servers;
		found = true;
		break;
	}
	rcu_read_unlock();
	return found;
}

static int rswap_app_open(struct inode *inode, struct file *file)
{
	if (!capable(CAP_SYS_ADMIN))
		return -EPERM;
	if (READ_ONCE(THIS_MODULE->state) != MODULE_STATE_LIVE)
		return -ENODEV;
	file->private_data = kzalloc(sizeof(struct rswap_app_binding), GFP_KERNEL);
	return file->private_data ? 0 : -ENOMEM;
}

static int rswap_app_release(struct inode *inode, struct file *file)
{
	struct rswap_app_binding *b = file->private_data;

	mutex_lock(&app_mutex);
	rswap_app_remove(b);
	mutex_unlock(&app_mutex);
	kfree(b);
	return 0;
}

static ssize_t rswap_app_write(struct file *file, const char __user *buf,
		size_t len, loff_t *pos)
{
	struct rswap_app_binding *b = file->private_data, *other;
	struct rswap_app_command cmd;
	struct task_struct *task;
	struct pid *pid;
	struct mem_cgroup *memcg;
	int ret = 0;

	if (!capable(CAP_SYS_ADMIN))
		return -EPERM;
	if (len != sizeof(cmd))
		return -EINVAL;
	if (copy_from_user(&cmd, buf, sizeof(cmd)))
		return -EFAULT;
	if (cmd.version != RSWAP_APP_VERSION || cmd.reserved || cmd.pid <= 0 ||
	    (cmd.policy != RSWAP_APP_DRAM && cmd.policy != RSWAP_APP_PMEM))
		return -EINVAL;
	if (mem_cgroup_disabled() || cgroup_subsys_on_dfl(memory_cgrp_subsys))
		return -EOPNOTSUPP;
	pid = find_get_pid(cmd.pid);
	task = get_pid_task(pid, PIDTYPE_PID);
	put_pid(pid);
	if (!task)
		return -ESRCH;
	rcu_read_lock();
	memcg = mem_cgroup_from_task(task);
	if (!memcg || !css_tryget_online(&memcg->css))
		memcg = NULL;
	rcu_read_unlock();
	put_task_struct(task);
	if (!memcg)
		return -ENODEV;
	/* Root binding would silently apply to unrelated applications. */
	if (!memcg->css.parent) {
		css_put(&memcg->css);
		return -EINVAL;
	}
	mutex_lock(&app_mutex);
	if (b->used) {
		ret = -ESTALE;
		goto out;
	}
	if (!app_active) {
		ret = -ENODEV;
		goto out;
	}
	list_for_each_entry(other, &app_bindings, link) {
		if (other->memcg == memcg) {
			ret = -EBUSY;
			goto out;
		}
	}
	b->memcg = memcg;
	b->id = ++app_next_id;
	b->pid = cmd.pid;
	b->policy = cmd.policy;
	b->used = b->registered = true;
	atomic64_set(&b->rr, 0);
	list_add_tail_rcu(&b->link, &app_bindings);
out:
	mutex_unlock(&app_mutex);
	if (ret)
		css_put(&memcg->css);
	return ret ? (ssize_t)ret : (ssize_t)len;
}

static ssize_t rswap_app_read(struct file *file, char __user *buf,
		size_t len, loff_t *pos)
{
	struct rswap_app_binding *b = file->private_data;
	struct rswap_app_ack ack;

	mutex_lock(&app_mutex);
	ack.binding_id = b->id;
	ack.policy = b->policy;
	ack.active = b->registered;
	mutex_unlock(&app_mutex);
	return simple_read_from_buffer(buf, len, pos, &ack, sizeof(ack));
}

static const struct file_operations rswap_app_fops = {
	.owner = THIS_MODULE, .open = rswap_app_open, .release = rswap_app_release,
	.read = rswap_app_read, .write = rswap_app_write, .llseek = default_llseek,
};

static int rswap_apps_show(struct seq_file *m, void *unused)
{
	struct rswap_app_binding *b;

	mutex_lock(&app_mutex);
	seq_printf(m, "version=1 area_active=%u\n", app_active);
	seq_puts(m, "binding pid policy\n");
	list_for_each_entry(b, &app_bindings, link)
		seq_printf(m, "%llu %u %s\n", (unsigned long long)b->id, b->pid,
			b->policy == RSWAP_APP_DRAM ? "pureDRAM" : "purePMem");
	mutex_unlock(&app_mutex);
	return 0;
}

static int rswap_apps_open(struct inode *inode, struct file *file)
{
	if (READ_ONCE(THIS_MODULE->state) != MODULE_STATE_LIVE)
		return -ENODEV;
	return single_open(file, rswap_apps_show, NULL);
}

static const struct file_operations rswap_apps_fops = {
	.owner = THIS_MODULE, .open = rswap_apps_open, .read = seq_read,
	.llseek = seq_lseek, .release = single_release,
};

int rswap_app_debugfs(struct dentry *root)
{
	struct dentry *file;

	file = debugfs_create_file("app_policy", 0600, root, NULL, &rswap_app_fops);
	if (IS_ERR_OR_NULL(file))
		return file ? PTR_ERR(file) : -ENOMEM;
	file = debugfs_create_file("app_policies", 0400, root, NULL, &rswap_apps_fops);
	return IS_ERR_OR_NULL(file) ? (file ? PTR_ERR(file) : -ENOMEM) : 0;
}
