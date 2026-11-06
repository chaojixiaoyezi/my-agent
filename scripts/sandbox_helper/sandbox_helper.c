/* One native pre-exec step: Landlock -> resources -> exec the existing bwrap argv. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/resource.h>
#include <sys/syscall.h>
#include <unistd.h>
#if !defined(__linux__) || (!defined(__x86_64__) && !defined(__aarch64__))
#error "sandbox helper requires Linux x86_64 or aarch64"
#endif
#define NR_CREATE 444
#define NR_ADD_RULE 445
#define NR_RESTRICT 446
#define LL_CREATE_RULESET_VERSION 1U
#define LL_ACCESS_NET_CONNECT_TCP (1ULL << 1)
#define LL_RULE_NET_PORT 2
#define EXIT_BAD_ARGV 96
#define EXIT_APPLY_FAILED 97
#define EXIT_RESOURCE_FAILED 98
struct ll_ruleset_attr { uint64_t handled_access_fs, handled_access_net; };
struct ll_net_port_attr { uint64_t allowed_access, port; };

// LLM: Fail-closed marker is consumed by landlock_launcher.is_apply_failed; never exec on error.
// 函数用途: 输出网络隔离失败的结构化原因，返回97。
static int landlock_fail(const char *reason, long a, long b) {
  if (a < 0) fprintf(stderr, "{\"error_code\":\"GATEWAY_ISOLATION_APPLY_FAILED\",\"reason\":\"%s\"}\n", reason);
  else if (b < 0) fprintf(stderr, "{\"error_code\":\"GATEWAY_ISOLATION_APPLY_FAILED\",\"reason\":\"%s:%ld\"}\n", reason, a);
  else fprintf(stderr, "{\"error_code\":\"GATEWAY_ISOLATION_APPLY_FAILED\",\"reason\":\"%s:%ld:%ld\"}\n", reason, a, b);
  return EXIT_APPLY_FAILED;
}

// LLM: Hard resource or exec errors match the Python resource launcher, without a fallback exec.
// 函数用途: 输出资源守卫失败标记，返回98。
static int resource_fail(void) {
  fputs("{\"error_code\":\"SANDBOX_RESOURCE_GUARD_FAILED\",\"error_type\":\"OSError\"}\n", stderr);
  return EXIT_RESOURCE_FAILED;
}

// LLM: Numeric arguments follow ResourceLimits bounds; reject overflow and trailing garbage.
// 函数用途: 严格解析范围内的十进制整数。
static int parse_long(const char *text, long lo, long hi, long *out) {
  char *end = NULL;
  errno = 0;
  long value = strtol(text, &end, 10);
  if (errno || !end || *end || end == text || value < lo || value > hi) return -1;
  *out = value;
  return 0;
}

// LLM: An 8KB bitmap covers every TCP port, including 0; duplicates cannot relax the deny set.
// 函数用途: 解析任意数量的端口并去重，不保留16端口上限。
static int parse_ports(char *text, uint8_t *blocked) {
  int count = 0;
  for (char *save = NULL, *tok = strtok_r(text, ",", &save); tok; tok = strtok_r(NULL, ",", &save)) {
    long port;
    if (parse_long(tok, 0, 65535, &port)) return -1;
    blocked[port / 8] |= (uint8_t)(1U << (port % 8));
    count++;
  }
  return count ? 0 : -1;
}

// LLM: Create CONNECT_TCP allow rules for the complement only; close unpublished fd on failure.
// 函数用途: 自包含deny模式构造与Python相同的端口ruleset。
static int build_ruleset(const uint8_t *blocked, int *fd_out) {
  long abi = syscall(NR_CREATE, NULL, 0, LL_CREATE_RULESET_VERSION);
  if (abi < 4) return landlock_fail("LANDLOCK_NET_UNSUPPORTED", -1, -1);
  struct ll_ruleset_attr attr = {0, LL_ACCESS_NET_CONNECT_TCP};
  int fd = (int)syscall(NR_CREATE, &attr, sizeof(attr), 0U);
  if (fd < 0) {
    if (errno == ENOSYS || errno == EOPNOTSUPP) return landlock_fail("LANDLOCK_NOT_SUPPORTED", -1, -1);
    return landlock_fail("CREATE_RULESET_FAILED", errno, -1);
  }
  struct ll_net_port_attr rule = {LL_ACCESS_NET_CONNECT_TCP, 0};
  for (long port = 0; port <= 65535; port++) {
    if (blocked[port / 8] & (1U << (port % 8))) continue;
    rule.port = (uint64_t)port;
    if (syscall(NR_ADD_RULE, fd, LL_RULE_NET_PORT, &rule, 0U) != 0) {
      int rc = landlock_fail("ADD_RULE_FAILED", port, errno);
      close(fd);
      return rc;
    }
  }
  *fd_out = fd;
  return 0;
}

// LLM: Apply on this sole thread; close the inherited ruleset before any bwrap or untrusted exec.
// 函数用途: 设置NNP、收紧网络domain并确保共享fd不会泄漏给命令。
static int restrict_self(int fd) {
  int rc = 0;
  if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0) rc = landlock_fail("NO_NEW_PRIVS_FAILED", -1, -1);
  else if (syscall(NR_RESTRICT, fd, 0U) != 0) rc = landlock_fail("RESTRICT_SELF_FAILED", -1, -1);
  if (close(fd) != 0 && !rc) rc = landlock_fail("CLOSE_RULESET_FAILED", -1, -1);
  return rc;
}

// LLM: Tighten both inherited limits, never raise either; unsupported errno is soft like Python.
// 函数用途: 应用可选AS/NPROC硬上限，真实错误拒绝执行。
static int tighten(int resource, long requested) {
  struct rlimit cur;
  if (getrlimit(resource, &cur) != 0) return errno;
  rlim_t ceiling = (rlim_t)requested;
  if (cur.rlim_cur != RLIM_INFINITY && cur.rlim_cur < ceiling) ceiling = cur.rlim_cur;
  if (cur.rlim_max != RLIM_INFINITY && cur.rlim_max < ceiling) ceiling = cur.rlim_max;
  struct rlimit next = {ceiling, ceiling};
  if (setrlimit(resource, &next) == 0) return 0;
  return (errno == ENOSYS || errno == EOPNOTSUPP || errno == EINVAL) ? 0 : errno;
}

// LLM: OOM is best effort and monotonic; never lower the inherited score or leak this proc fd.
// 函数用途: 只增加当前进程的OOM偏置，失败不阻断普通命令。
static void raise_oom_score_adj(long wanted) {
  int fd = open("/proc/self/oom_score_adj", O_RDWR | O_CLOEXEC);
  if (fd < 0) return;
  char buf[32] = {0};
  ssize_t n = read(fd, buf, sizeof(buf) - 1);
  long current = n > 0 ? strtol(buf, NULL, 10) : 0;
  if (n > 0 && wanted > current) {
    int len = snprintf(buf, sizeof(buf), "%ld", wanted);
    if (lseek(fd, 0, SEEK_SET) == 0 && write(fd, buf, (size_t)len) < 0) { /* soft guard */ }
  }
  close(fd);
}

// LLM: No cgroup or shell mode; validate all argv before Landlock, resources and absolute execv.
// 函数用途: 原生启动器入口，严格保持网络限制先于资源限制，失败时绝不执行后续命令。
int main(int argc, char **argv) {
  long nice_inc = 0, oom = 0, mem_mb = 0, nproc = 0, ruleset_fd = -1;
  uint8_t blocked_bitmap[8192] = {0};
  int has_ports = 0, i = 1;
  for (; i < argc && strcmp(argv[i], "--") != 0; i += 2) {
    if (i + 1 >= argc) return EXIT_BAD_ARGV;
    const char *key = argv[i], *value = argv[i + 1];
    int bad = 0;
    if (!strcmp(key, "--nice")) bad = parse_long(value, 0, 19, &nice_inc);
    else if (!strcmp(key, "--oom-score-adj")) bad = parse_long(value, 0, 1000, &oom);
    else if (!strcmp(key, "--max-memory-mb")) bad = parse_long(value, 0, LONG_MAX / (1024L * 1024L), &mem_mb);
    else if (!strcmp(key, "--max-processes")) bad = parse_long(value, 0, INT_MAX, &nproc);
    else if (!strcmp(key, "--landlock-fd")) bad = parse_long(value, 3, INT_MAX, &ruleset_fd);
    else if (!strcmp(key, "--deny")) { bad = parse_ports(argv[i + 1], blocked_bitmap); has_ports = 1; }
    else bad = -1;
    if (bad) return EXIT_BAD_ARGV;
  }
  if (i >= argc - 1 || argv[i + 1][0] != '/' || (has_ports && ruleset_fd >= 0)) return EXIT_BAD_ARGV;
  if (has_ports) {
    int fd = -1, rc = build_ruleset(blocked_bitmap, &fd);
    if (rc) return rc;
    ruleset_fd = fd;
  }
  if (ruleset_fd >= 0) {
    int rc = restrict_self((int)ruleset_fd);
    if (rc) return rc;
  }
  if (nice_inc) { errno = 0; if (nice((int)nice_inc) == -1 && errno) { /* soft guard */ } }
  if (oom) raise_oom_score_adj(oom);
  if (mem_mb && tighten(RLIMIT_AS, mem_mb * 1024L * 1024L)) return resource_fail();
  if (nproc && tighten(RLIMIT_NPROC, nproc)) return resource_fail();
  execv(argv[i + 1], &argv[i + 1]);
  return resource_fail();
}
