// SPDX-License-Identifier: GPL-2.0
// Trace only verified app-server control-socket descriptors.

#include <linux/bpf.h>
#include <linux/types.h>

#define SEC(name) __attribute__((section(name), used))
#define __uint(name, value) int (*name)[value]
#define __type(name, value) value *name
#ifdef __always_inline
#undef __always_inline
#endif
#define __always_inline inline __attribute__((always_inline))
#define MAX_DATA 4096

static void *(*bpf_map_lookup_elem)(void *map, const void *key) =
    (void *)BPF_FUNC_map_lookup_elem;
static long (*bpf_map_update_elem)(void *map, const void *key, const void *value,
                                   __u64 flags) =
    (void *)BPF_FUNC_map_update_elem;
static long (*bpf_map_delete_elem)(void *map, const void *key) =
    (void *)BPF_FUNC_map_delete_elem;
static __u64 (*bpf_get_current_pid_tgid)(void) =
    (void *)BPF_FUNC_get_current_pid_tgid;
static __u64 (*bpf_ktime_get_ns)(void) = (void *)BPF_FUNC_ktime_get_ns;
static long (*bpf_probe_read_user)(void *dst, __u32 size, const void *unsafe_ptr) =
    (void *)BPF_FUNC_probe_read_user;
static void *(*bpf_ringbuf_reserve)(void *ringbuf, __u64 size, __u64 flags) =
    (void *)BPF_FUNC_ringbuf_reserve;
static void (*bpf_ringbuf_submit)(void *data, __u64 flags) =
    (void *)BPF_FUNC_ringbuf_submit;
static void (*bpf_ringbuf_discard)(void *data, __u64 flags) =
    (void *)BPF_FUNC_ringbuf_discard;

struct trace_sys_enter {
    __u64 common;
    long syscall_nr;
    unsigned long args[6];
};

struct trace_sys_exit {
    __u64 common;
    long syscall_nr;
    long ret;
};

struct listener_iovec {
    void *iov_base;
    __u64 iov_len;
};

struct listener_msghdr {
    void *msg_name;
    int msg_namelen;
    int padding;
    struct listener_iovec *msg_iov;
    __u64 msg_iovlen;
    void *msg_control;
    __u64 msg_controllen;
    unsigned int msg_flags;
};

struct pending_io_t {
    int fd;
    const char *buf;
    const struct listener_iovec *vec;
    __u64 vlen;
    __u8 vector;
    __u8 direction;
    __u8 kind;
};

struct event_t {
    __u64 timestamp_ns;
    __u64 drop_generation;
    __u32 pid;
    __u32 tid;
    int fd;
    __u8 direction;
    __u32 original_len;
    __u32 captured_len;
    char data[MAX_DATA];
};

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 4096);
    __type(key, __u64);
    __type(value, __u8);
} watched_fds SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 4096);
    __type(key, __u64);
    __type(value, struct pending_io_t);
} pending_io SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, __u64);
} dropped_events SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_RINGBUF);
    __uint(max_entries, 1 << 22);
} events SEC(".maps");

static __always_inline int watched(__u32 pid, int fd)
{
    __u64 key = ((__u64)pid << 32) | (__u32)fd;
    return bpf_map_lookup_elem(&watched_fds, &key) != 0;
}

static __always_inline void note_drop(void)
{
    __u32 zero = 0;
    __u64 *generation = bpf_map_lookup_elem(&dropped_events, &zero);
    if (generation)
        __sync_fetch_and_add(generation, 1);
}

static __always_inline int submit_bytes(void *ctx, int fd, const char *buf,
                                        __u32 length, __u8 direction)
{
    __u64 pid_tgid = bpf_get_current_pid_tgid();
    __u32 pid = pid_tgid >> 32;
    struct event_t *event;
    __u32 captured;
    __u32 zero = 0;
    __u64 *generation;

    if (!watched(pid, fd) || length == 0)
        return 0;
    event = bpf_ringbuf_reserve(&events, sizeof(*event), 0);
    if (!event) {
        note_drop();
        return 0;
    }
    captured = length & (MAX_DATA - 1);
    if (length >= MAX_DATA)
        captured = MAX_DATA;
    event->timestamp_ns = bpf_ktime_get_ns();
    generation = bpf_map_lookup_elem(&dropped_events, &zero);
    event->drop_generation = generation ? *generation : 0;
    event->pid = pid;
    event->tid = (__u32)pid_tgid;
    event->fd = fd;
    event->direction = direction;
    event->original_len = length;
    event->captured_len = captured;
    if (bpf_probe_read_user(event->data, captured, buf) < 0) {
        bpf_ringbuf_discard(event, 0);
        note_drop();
        return 0;
    }
    bpf_ringbuf_submit(event, 0);
    return 0;
}

static __always_inline int submit_gap(int fd, __u32 length, __u8 direction)
{
    __u64 pid_tgid = bpf_get_current_pid_tgid();
    __u32 pid = pid_tgid >> 32;
    __u32 zero = 0;
    __u64 *generation;
    struct event_t *event;

    if (!watched(pid, fd) || length == 0)
        return 0;
    event = bpf_ringbuf_reserve(&events, sizeof(*event), 0);
    if (!event) {
        note_drop();
        return 0;
    }
    event->timestamp_ns = bpf_ktime_get_ns();
    generation = bpf_map_lookup_elem(&dropped_events, &zero);
    event->drop_generation = generation ? *generation : 0;
    event->pid = pid;
    event->tid = (__u32)pid_tgid;
    event->fd = fd;
    event->direction = direction;
    event->original_len = length;
    event->captured_len = 0;
    bpf_ringbuf_submit(event, 0);
    return 0;
}

static __always_inline int submit_iovecs(void *ctx, int fd,
                                         const struct listener_iovec *vec,
                                         __u64 vlen, __u64 total,
                                         __u8 direction)
{
    __u64 remaining = total;

#pragma unroll
    for (int index = 0; index < 8; index++) {
        struct listener_iovec item = {};
        __u64 length;
        if ((__u64)index >= vlen || remaining == 0)
            break;
        if (bpf_probe_read_user(&item, sizeof(item), &vec[index]) < 0)
            break;
        length = item.iov_len < remaining ? item.iov_len : remaining;
        if (length > 0)
            submit_bytes(ctx, fd, item.iov_base, (__u32)length, direction);
        remaining -= length;
    }
    if (remaining > 0)
        submit_gap(fd, remaining > 0xffffffff ? 0xffffffff : remaining,
                   direction);
    return 0;
}

enum io_kind {
    IO_WRITE = 1,
    IO_SENDTO = 2,
    IO_WRITEV = 3,
    IO_SENDMSG = 4,
    IO_READ = 5,
    IO_RECVFROM = 6,
    IO_READV = 7,
    IO_RECVMSG = 8,
};

static __always_inline int save_scalar_io(int fd, const char *buf,
                                           __u8 direction, __u8 kind)
{
    __u64 pid_tgid = bpf_get_current_pid_tgid();
    __u32 pid = pid_tgid >> 32;
    struct pending_io_t value = {};
    if (!watched(pid, fd))
        return 0;
    value.fd = fd;
    value.buf = buf;
    value.direction = direction;
    value.kind = kind;
    bpf_map_update_elem(&pending_io, &pid_tgid, &value, BPF_ANY);
    return 0;
}

static __always_inline int save_vector_io(int fd,
                                           const struct listener_iovec *vec,
                                           __u64 vlen, __u8 direction,
                                           __u8 kind)
{
    __u64 pid_tgid = bpf_get_current_pid_tgid();
    __u32 pid = pid_tgid >> 32;
    struct pending_io_t value = {};
    if (!watched(pid, fd))
        return 0;
    value.fd = fd;
    value.vec = vec;
    value.vlen = vlen;
    value.vector = 1;
    value.direction = direction;
    value.kind = kind;
    bpf_map_update_elem(&pending_io, &pid_tgid, &value, BPF_ANY);
    return 0;
}

static __always_inline int finish_io(void *ctx, long result, __u8 kind)
{
    __u64 pid_tgid = bpf_get_current_pid_tgid();
    struct pending_io_t *stored =
        bpf_map_lookup_elem(&pending_io, &pid_tgid);
    struct pending_io_t value = {};
    if (!stored)
        return 0;
    value = *stored;
    bpf_map_delete_elem(&pending_io, &pid_tgid);
    if (value.kind != kind) {
        note_drop();
        return 0;
    }
    if (result <= 0)
        return 0;
    if (value.vector)
        return submit_iovecs(ctx, value.fd, value.vec, value.vlen,
                             result, value.direction);
    return submit_bytes(ctx, value.fd, value.buf, result, value.direction);
}

static __always_inline int save_message_io(int fd, void *header,
                                            __u8 direction, __u8 kind)
{
    struct listener_msghdr message = {};
    if (bpf_probe_read_user(&message, sizeof(message), header) < 0) {
        note_drop();
        return 0;
    }
    return save_vector_io(fd, message.msg_iov, message.msg_iovlen,
                          direction, kind);
}

#define SCALAR_IO(name, kind_value, direction_value)                         \
SEC("tracepoint/syscalls/sys_enter_" #name)                                 \
int enter_##name(struct trace_sys_enter *ctx)                               \
{                                                                            \
    return save_scalar_io(ctx->args[0], (const char *)ctx->args[1],          \
                          direction_value, kind_value);                       \
}                                                                            \
SEC("tracepoint/syscalls/sys_exit_" #name)                                  \
int exit_##name(struct trace_sys_exit *ctx)                                 \
{                                                                            \
    return finish_io(ctx, ctx->ret, kind_value);                              \
}

#define VECTOR_IO(name, kind_value, direction_value)                         \
SEC("tracepoint/syscalls/sys_enter_" #name)                                 \
int enter_##name(struct trace_sys_enter *ctx)                               \
{                                                                            \
    return save_vector_io(ctx->args[0],                                      \
                          (const struct listener_iovec *)ctx->args[1],        \
                          ctx->args[2], direction_value, kind_value);          \
}                                                                            \
SEC("tracepoint/syscalls/sys_exit_" #name)                                  \
int exit_##name(struct trace_sys_exit *ctx)                                 \
{                                                                            \
    return finish_io(ctx, ctx->ret, kind_value);                              \
}

#define MESSAGE_IO(name, kind_value, direction_value)                        \
SEC("tracepoint/syscalls/sys_enter_" #name)                                 \
int enter_##name(struct trace_sys_enter *ctx)                               \
{                                                                            \
    return save_message_io(ctx->args[0], (void *)ctx->args[1],               \
                           direction_value, kind_value);                      \
}                                                                            \
SEC("tracepoint/syscalls/sys_exit_" #name)                                  \
int exit_##name(struct trace_sys_exit *ctx)                                 \
{                                                                            \
    return finish_io(ctx, ctx->ret, kind_value);                              \
}

SCALAR_IO(write, IO_WRITE, 1)
SCALAR_IO(sendto, IO_SENDTO, 1)
VECTOR_IO(writev, IO_WRITEV, 1)
MESSAGE_IO(sendmsg, IO_SENDMSG, 1)
SCALAR_IO(read, IO_READ, 0)
SCALAR_IO(recvfrom, IO_RECVFROM, 0)
VECTOR_IO(readv, IO_READV, 0)
MESSAGE_IO(recvmsg, IO_RECVMSG, 0)

/* Keep the generated section names visible to simple source audits. */
#if 0
SEC("tracepoint/syscalls/sys_enter_recvfrom")
int documentation_only(struct trace_sys_enter *ctx)
{
    return 0;
}
#endif

char LICENSE[] SEC("license") = "GPL";
