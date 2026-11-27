// SPDX-License-Identifier: GPL-2.0
#include <linux/capability.h>
#include <linux/debugfs.h>
#include <linux/mm.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/pid.h>
#include <linux/rcupdate.h>
#include <linux/rswap_identity.h>
#include <linux/sched/mm.h>
#include <linux/sched/task.h>
#include <linux/seq_file.h>
#include <linux/slab.h>
#include <linux/uaccess.h>
#include <linux/xarray.h>

#include "rswap_identity.h"

struct rswap_vpn_link {
	struct rswap_vpn_link *next;
	unsigned long vpn;
};

struct rswap_identity_context {
	struct mm_struct *mm; /* mm_count reference, not a permanent mm_users pin */
	struct pid *task_pid;
	struct xarray entries; /* swap offset -> list of distinct monitored VPNs */
	u64 id, entries_count, aliases, binds, frees, alloc_errors;
	u64 query_offset;
	unsigned int pid, type;
	bool used, queried;
};

static DEFINE_MUTEX(identity_mutex);
static struct rswap_identity_context __rcu *identity_target;
static u64 identity_next_id, identity_max_pages;
static unsigned int identity_type;
static bool identity_active, identity_registered;

/* All alias lists and counters are protected by the XArray's IRQ-safe lock.
 * Readers/probes also hold RCU for the entire use of their target context.
 */
static void rswap_identity_bind_probe(void *unused, struct mm_struct *mm,
		unsigned long address, unsigned int type, unsigned long offset)
{
	struct rswap_identity_context *c;
	struct rswap_vpn_link *link, *head, *p;
	unsigned long flags, vpn = address >> PAGE_SHIFT;
	void *old;

	rcu_read_lock();
	c = rcu_dereference(identity_target);
	if (!c || c->mm != mm || c->type != type || offset >= identity_max_pages)
		goto out;
	link = kmalloc(sizeof(*link), GFP_ATOMIC);
	xa_lock_irqsave(&c->entries, flags);
	c->binds++;
	head = xa_load(&c->entries, offset);
	for (p = head; p; p = p->next) {
		if (p->vpn == vpn)
			goto unlock;
	}
	if (!link) {
		c->alloc_errors++;
		goto unlock;
	}
	link->vpn = vpn;
	link->next = head;
	old = __xa_store(&c->entries, offset, link, GFP_ATOMIC);
	if (xa_is_err(old)) {
		c->alloc_errors++;
		goto unlock;
	}
	if (!head)
		c->entries_count++;
	c->aliases++;
	link = NULL;
unlock:
	xa_unlock_irqrestore(&c->entries, flags);
	kfree(link);
out:
	rcu_read_unlock();
}

static void rswap_identity_free_probe(void *unused, unsigned int type,
		unsigned long offset)
{
	struct rswap_identity_context *c;
	struct rswap_vpn_link *p, *next;
	unsigned long flags;

	rcu_read_lock();
	c = rcu_dereference(identity_target);
	if (!c || c->type != type)
		goto out;
	xa_lock_irqsave(&c->entries, flags);
	p = __xa_erase(&c->entries, offset);
	if (p) {
		c->entries_count--;
		c->frees++;
	}
	while (p) {
		next = p->next;
		c->aliases--;
		kfree(p);
		p = next;
	}
	xa_unlock_irqrestore(&c->entries, flags);
out:
	rcu_read_unlock();
}

int rswap_identity_lookup(unsigned int type, u64 offset,
		struct rswap_identity_result *result)
{
	struct rswap_identity_context *c;
	struct rswap_vpn_link *p;
	unsigned long flags;
	int ret = -ENOENT;

	memset(result, 0, sizeof(*result));
	if (offset >= identity_max_pages)
		return -ERANGE;
	rcu_read_lock();
	c = rcu_dereference(identity_target);
	if (!c || c->type != type)
		goto out;
	xa_lock_irqsave(&c->entries, flags);
	for (p = xa_load(&c->entries, (unsigned long)offset); p; p = p->next) {
		if (!result->aliases || p->vpn < result->vpn)
			result->vpn = p->vpn;
		result->aliases++;
	}
	if (result->aliases) {
		result->binding_id = c->id;
		result->pid = c->pid;
		ret = 0;
	}
	xa_unlock_irqrestore(&c->entries, flags);
out:
	rcu_read_unlock();
	return ret;
}

/* Compare pointers only under RCU: the context pins mm_count, and pid_task is
 * RCU-protected. No mm_users pin, sleeping operation, or task pointer escapes.
 * An exec changes mm even if PID/start time and executable path stay equal.
 */
bool rswap_identity_binding_valid(u64 binding_id)
{
	struct rswap_identity_context *c;
	struct task_struct *task;
	bool valid = false;

	rcu_read_lock();
	c = rcu_dereference(identity_target);
	if (c && c->id == binding_id && READ_ONCE(identity_active)) {
		task = pid_task(c->task_pid, PIDTYPE_PID);
		valid = task && READ_ONCE(task->mm) == c->mm;
	}
	rcu_read_unlock();
	return valid;
}

/* identity_mutex held. Stop publishing first; free only after every probe and
 * lookup has copied its result. No mmap_lock is held during the RCU wait.
 */
static void rswap_identity_detach(void)
{
	struct rswap_identity_context *c = rcu_dereference_protected(identity_target, 1);
	struct rswap_vpn_link *p, *next;
	unsigned long index;

	if (!c)
		return;
	rcu_assign_pointer(identity_target, NULL);
	synchronize_rcu();
	xa_for_each(&c->entries, index, p) {
		while (p) {
			next = p->next;
			kfree(p);
			p = next;
		}
		cond_resched();
	}
	xa_destroy(&c->entries);
	c->entries_count = c->aliases = 0;
	mmdrop(c->mm);
	c->mm = NULL;
	put_pid(c->task_pid);
	c->task_pid = NULL;
}

void rswap_identity_area(bool active, unsigned int type)
{
	mutex_lock(&identity_mutex);
	identity_active = active;
	identity_type = type;
	if (!active)
		rswap_identity_detach();
	mutex_unlock(&identity_mutex);
}

int rswap_identity_init(u64 max_pages)
{
	int ret;

	identity_max_pages = max_pages;
	ret = register_trace_rswap_swap_bind(rswap_identity_bind_probe, NULL);
	if (ret)
		return ret;
	ret = register_trace_rswap_swap_free(rswap_identity_free_probe, NULL);
	if (ret) {
		unregister_trace_rswap_swap_bind(rswap_identity_bind_probe, NULL);
		tracepoint_synchronize_unregister();
		return ret;
	}
	identity_registered = true;
	return 0;
}

void rswap_identity_exit(void)
{
	if (identity_registered) {
		unregister_trace_rswap_swap_bind(rswap_identity_bind_probe, NULL);
		unregister_trace_rswap_swap_free(rswap_identity_free_probe, NULL);
		tracepoint_synchronize_unregister();
		identity_registered = false;
	}
	rswap_identity_area(false, 0);
	identity_max_pages = 0;
}

/* Each control fd may bind once; closing it releases only its own binding.
 * A stopped descriptor cannot attach to a later swapon or another process.
 */
static int rswap_identity_open(struct inode *inode, struct file *file)
{
	struct rswap_identity_context *c;

	if (!capable(CAP_SYS_ADMIN))
		return -EPERM;
	if (READ_ONCE(THIS_MODULE->state) != MODULE_STATE_LIVE)
		return -ENODEV;
	c = kzalloc(sizeof(*c), GFP_KERNEL);
	if (!c)
		return -ENOMEM;
	xa_init_flags(&c->entries, XA_FLAGS_LOCK_IRQ);
	file->private_data = c;
	return 0;
}

static int rswap_identity_release(struct inode *inode, struct file *file)
{
	struct rswap_identity_context *c = file->private_data;

	mutex_lock(&identity_mutex);
	if (rcu_access_pointer(identity_target) == c)
		rswap_identity_detach();
	mutex_unlock(&identity_mutex);
	kfree(c);
	return 0;
}

static int rswap_identity_bind_pid(struct rswap_identity_context *c, int nr)
{
	struct task_struct *task;
	struct mm_struct *mm;
	struct pid *pid;
	unsigned long flags;
	int ret;

	if (c->used)
		return -ESTALE;
	if (!identity_active)
		return -ENODEV;
	if (rcu_access_pointer(identity_target))
		return -EBUSY;
	pid = find_get_pid(nr);
	task = get_pid_task(pid, PIDTYPE_PID);
	if (!task) {
		put_pid(pid);
		return -ESRCH;
	}
	mm = get_task_mm(task);
	put_task_struct(task);
	if (!mm) {
		put_pid(pid);
		return -EINVAL;
	}
	/* Keep the mm allocation alive, but allow exit_mmap after process exit. */
	mmgrab(mm);
	c->mm = mm;
	c->task_pid = pid;
	c->pid = nr;
	c->type = identity_type;
	c->id = ++identity_next_id;
	c->used = true;
	rcu_assign_pointer(identity_target, c);
	/* Subscribe before scanning so concurrent reclaim cannot fall in a gap.
	 * PTE locks serialize scan notifications with entry release/reuse.
	 */
	ret = rswap_identity_scan_mm(mm);
	mmput(mm);
	xa_lock_irqsave(&c->entries, flags);
	if (!ret && c->alloc_errors)
		ret = -ENOMEM;
	xa_unlock_irqrestore(&c->entries, flags);
	if (ret)
		rswap_identity_detach();
	return ret;
}

static ssize_t rswap_identity_write(struct file *file, const char __user *buf,
		size_t len, loff_t *pos)
{
	struct rswap_identity_context *c = file->private_data;
	char command[64], *value;
	int nr, ret = -EINVAL;
	u64 offset;

	if (!capable(CAP_SYS_ADMIN))
		return -EPERM;
	if (!len || len >= sizeof(command))
		return -EINVAL;
	if (copy_from_user(command, buf, len))
		return -EFAULT;
	command[len] = 0;
	value = strim(command);
	mutex_lock(&identity_mutex);
	if (!strncmp(value, "bind ", 5)) {
		ret = kstrtoint(strim(value + 5), 10, &nr);
		if (!ret)
			ret = nr > 0 ? rswap_identity_bind_pid(c, nr) : -EINVAL;
	} else if (!strncmp(value, "lookup ", 7)) {
		ret = kstrtoull(strim(value + 7), 0, &offset);
		if (!ret && (offset >= identity_max_pages))
			ret = -ERANGE;
		if (!ret) {
			c->query_offset = offset;
			c->queried = true;
		}
	}
	mutex_unlock(&identity_mutex);
	if (!ret)
		*pos = 0;
	return ret ? (ssize_t)ret : (ssize_t)len;
}

static ssize_t rswap_identity_read(struct file *file, char __user *buf,
		size_t len, loff_t *pos)
{
	struct rswap_identity_context *c = file->private_data;
	struct rswap_identity_result result;
	char text[512];
	int bytes, found = 0;
	bool attached;

	mutex_lock(&identity_mutex);
	attached = rcu_access_pointer(identity_target) == c;
	if (attached && c->queried)
		found = !rswap_identity_lookup(c->type, c->query_offset, &result);
	bytes = scnprintf(text, sizeof(text),
		"version=1 binding_id=%llu pid=%u attached=%u mm_current=%u queried=%u found=%u type=%u offset=%llu vpn=%llu va=0x%llx aliases=%llu\n",
		c->id, c->pid, attached, attached && rswap_identity_binding_valid(c->id), c->queried, found, c->type,
		c->query_offset, found ? result.vpn : 0,
		found ? result.vpn << PAGE_SHIFT : 0, found ? result.aliases : 0);
	mutex_unlock(&identity_mutex);
	return simple_read_from_buffer(buf, len, pos, text, bytes);
}

static const struct file_operations rswap_identity_fops = {
	.owner = THIS_MODULE, .open = rswap_identity_open,
	.release = rswap_identity_release, .read = rswap_identity_read,
	.write = rswap_identity_write, .llseek = default_llseek,
};

static int rswap_identity_status_show(struct seq_file *m, void *unused)
{
	struct rswap_identity_context *c;
	u64 id = 0, entries = 0, aliases = 0, binds = 0, frees = 0, errors = 0;
	unsigned int pid = 0;
	unsigned long flags;

	mutex_lock(&identity_mutex);
	c = rcu_dereference_protected(identity_target, 1);
	if (c) {
		id = c->id;
		pid = c->pid;
		xa_lock_irqsave(&c->entries, flags);
		entries = c->entries_count;
		aliases = c->aliases;
		binds = c->binds;
		frees = c->frees;
		errors = c->alloc_errors;
		xa_unlock_irqrestore(&c->entries, flags);
	}
	seq_printf(m, "version=1 area_active=%u swap_type=%u bound=%u binding_id=%llu pid=%u entries=%llu aliases=%llu bind_events=%llu free_events=%llu alloc_errors=%llu\n",
		identity_active, identity_type, !!c, id, pid, entries, aliases,
		binds, frees, errors);
	mutex_unlock(&identity_mutex);
	return 0;
}

static int rswap_identity_status_open(struct inode *inode, struct file *file)
{
	if (READ_ONCE(THIS_MODULE->state) != MODULE_STATE_LIVE)
		return -ENODEV;
	return single_open(file, rswap_identity_status_show, NULL);
}

static const struct file_operations rswap_identity_status_fops = {
	.owner = THIS_MODULE, .open = rswap_identity_status_open,
	.read = seq_read, .llseek = seq_lseek, .release = single_release,
};

int rswap_identity_debugfs(struct dentry *root)
{
	struct dentry *file;

	file = debugfs_create_file("identity", 0600, root, NULL, &rswap_identity_fops);
	if (IS_ERR_OR_NULL(file))
		return file ? PTR_ERR(file) : -ENOMEM;
	file = debugfs_create_file("identity_status", 0400, root, NULL,
				  &rswap_identity_status_fops);
	return IS_ERR_OR_NULL(file) ? (file ? PTR_ERR(file) : -ENOMEM) : 0;
}
