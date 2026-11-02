# 两个既有仓库的逻辑差异，与本工具箱的重写依据

本文回答"为什么 bwx 这么写"。三个代码库的关系：

- **`swebench-firecracker`**（`/home/cxd/mpam/code/` 下是它的 fork，上游
  `gitcode.com/duelu/swebench-firecracker`）：完整的交付流水线（convert →
  v1 注入 → v2 交付盘）与 run 重放框架，代码最重。
- **`linearity-research`**（`/home/cxd/linearity-research`）：面向"线性度"研究
  的精简重写，直接 `--no-api` 裸 Firecracker + 运行时任务盘，不修改镜像；
  resctrl 组管理、C 带宽探针、分析脚本都出自这里。
- **`bwx`**（本仓库）：以两者为参照的第三次实现，吸收各自的正确部分，
  修掉各自的已知坑。

## 逐项对比

| 维度 | swebench-firecracker | linearity-research | bwx |
|---|---|---|---|
| VM 启动 | `--api-sock` + config-file | `--no-api` + config-file | `--no-api`（少一个要清理的 socket） |
| 轨迹承载 | v1/v2 两阶段：debugfs 注入镜像副本，结果走第二块 results 盘 | 不改镜像，运行时挂 payload 任务盘，串口正则解析 | payload 任务盘（沿用 linearity）；结果**双通道**：串口 CRC JSON + 结果盘 summary，互相校验 |
| 每轮参数 | 每轮 debugfs 替换镜像副本里的 init（要回读 sha 验证） | 固定，写死在任务盘 | **全部走内核 cmdline**（`bwx_token/超时/wait`），镜像与任务盘模板零每轮差异，无需 debugfs 写验证 |
| 串口 | console=ttyS0 固定，全量落盘，host 100ms 增量读 | `--silent-console` 可关（串口在 16 并发占 ~28% sys 的教训），但关了校验也没了 | 三档 `--console off/markers/full`；动作输出永不进串口（每步日志在 payload 盘），`markers` 档兼顾零成本与完整校验 |
| 成功判定 | 依赖 results 盘 summary + exit code | `check_guest` 扫 panic 标记（linearity 踩过：init 失败 → panic → firecracker exit 0 被当成功） | 显式证据链：BOOT→READY(注入 CRC 对账)→全 STEP→RESULT CRC→与盘上 summary 一致→放置校验；`off` 模式降级为盘校验并标记 disabled |
| 动作执行 | 同一 bash source（保留 cwd/env/函数） | 每步独立 bash 子进程（shell 状态不保留） | source 方式（轨迹是那样录的）+ 每动作 watchdog + `submit()` 函数兜底 |
| 计时 | guest 纳秒逐动作（events.jsonl） | 只有 VMM 墙钟一个数 | guest 纳秒为主（`actions_s`），host 墙钟只做整轮记账 |
| CPU/NUMA | numactl + 运行中逐线程 sched_setaffinity + 扫 /proc 避让他人 VM（TOCTOU 竞态）+ NUMA 内存容量预估 | numactl 整进程 + 事后 Placement 采样，用户显式给 CPU | numactl + 逐线程 vCPU/helper 细绑（含"vCPU 线程绑齐前不动主线程"），**显式分配计划**代替竞态扫描；`guest_ram_numa` 用虚拟尺寸定位 RAM 映射（实测踩坑：按驻留页找早期根本不触发） |
| MPAM | 无 | Python Group 类：`--join` 启动器继承 CLOSID、零填充回读、非空拒删 | 同 `--join` 机制 + 逐运行成员采样证据（稳态判定）+ **组复用**（本机 `num_rmids=1`，快速建删会永久耗尽池子，实测）+ 0 档全线拒绝 |
| 带宽真值 | 无 | C RMW 探针测"逻辑流量" | ddrc uncore PMU 按 node 聚合 + calibrate 用已知流量探针标定 scale + 窗口只积分 CASE_START..DONE |
| 档位集合 | 硬编码 | sweep 脚本硬编码 | 由目标机 `bandwidth_gran` 推导 + 吸附表去重（gran=9 的机器 1..8 自动坍缩成一个档） |
| 工具缺失 | v2 阶段往镜像里补 python（chroot + 私有 loader） | 不补（缺 python 的步骤 127 失败） | 共享只读工具盘（vdc）：一次构建全体 VM 复用；conda 树 + ELF 闭包 + 私有 loader（踩坑：ldd 要对源运行时跑，扩展模块的 `../../lib` RPATH 搬家后解析不了 libssl/libbz2）；不遮蔽镜像自带 python |
| config | setup.json 绝对路径（换机器就死，踩过：console 全空 exit 0） | 命令行 + 硬编码默认 | config.toml(gitignored) < env < CLI，doctor 启动前校验二进制路径 |

## 从两个仓库各拿了什么、修了什么

**拿自 swebench-firecracker**：`mkfs.ext4 -d` 免挂载制盘、`du -sb` 表观尺寸估
容量、`-O ^metadata_csum_seed`、交付盘 sha 对账、`reboot=k panic=1` + sysrq-b
兜底退出、"vCPU 绑齐前不收窄主线程"、action 以 source 执行、`submit()` 语义、
每步日志与 events 事件流、boot/case 双阶段预算。

**拿自 linearity-research**：`--join` 启动器（写自身 PID 进 tasks 再 exec，子线程
全部继承）、schemata 零填充数值比较、"新组不得动非自有资源"、C 探针（RMW+
barrier+prefault，逻辑流量已知）、`--silent-console` 的动机（串口是被测干扰源）、
plan/execute 分离与失败继续、描述性统计不做显著性声明。

**bwx 新修的坑（全部实测）**：guest init 在挂 /proc 前读 cmdline 拿不到 token；
`<(...)` 进程替换需要 /dev/fd（devtmpfs 没有）；mawk 的 `%d` 在 2^31 截断纳秒
时间戳（换 `%.0f`）；镜像 `image-env.sh` 会整段覆盖 PATH（工具盘要先 source 再
前置）；`mount /dev/vdc` 前要先 `mkdir -p /opt/bwx-tools`；perf 的 CSV 在
stderr；绑线程在 numactl exec 之前会把 numactl 自己钉死导致其拒绝
`--physcpubind` 退出 1；按驻留页找 guest RAM 映射永远找不到（要用虚拟尺寸）；
membership 采样有固有竞态（快照先行 + 稳态判定）；resctrl 组建删循环耗尽
RMID 池不恢复（按档位复用组）。
