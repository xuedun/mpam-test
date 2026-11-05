# bwx -- Firecracker 沙箱轨迹重放工具

一个自包含的实验工具：把 SWE-bench podman 镜像转成 ext4、用 Firecracker 并发重放
轨迹、用 ddrc uncore PMU 测量真实内存带宽。支持多 case 并发、显式 CPU 绑核、
以及保持机器满负载的填充模式。标准库 Python 3.9+，无第三方依赖。

> 历史：本仓库最初围绕 MPAM(resctrl) 带宽限制实验构建，MPAM 层已按需求移除
> （git 历史里保留完整实现：mpam.py / sweep / alloc / calibrate / analyze）。

## 快速上手

```bash
cp config.example.toml config.toml     # 改 dataset / firecracker / kernel 路径
sudo ./bwx doctor                      # 体检：工具/KVM/ddrc PMU/数据集
sudo ./bwx convert --all               # 镜像 -> cases/<id>/image.ext4（幂等）
sudo ./bwx replay --case tikv__pd-5480 # 单 case 重放
sudo ./bwx replay --case-file cases.txt --concurrency 8 --fill   # 并发 + 填充模式
```

优先级：命令行 > 环境变量（`BWX_*`）> config.toml > 内置默认。
碰 KVM/podman 的命令都要 root（`sudo ./bwx ...`，或先 `sudo -v` 缓存凭据）。

```
doctor     机器体检（工具/KVM/ddrc PMU/数据集）
convert    podman 镜像 -> ext4 交付镜像（幂等）
replay     并发多 VM 轨迹重放（支持填充模式）
list       已转换 case 一览
cleanup    清理遗留 scratch 目录
```

## replay：四个输入轴

### 1. case 选择（三种入口，可混用）

| 参数 | 形式 |
|---|---|
| `--case ID` | 单个 case，可重复 |
| `--cases a,b,c` | 逗号分隔列表 |
| `--case-file FILE` | 文件：`.txt`（一行一个，`#` 注释）或 `.json`（`["id",...]` / `{"cases":[...]}`，结构化 mix 会被展平为 id 列表） |

模板见 `cases.example.txt` / `cases.example.json`。三入口合并去重保序。

### 2. concurrency 展开

`--concurrency N`（默认 = case 数）：

- N **小于** case 数 → **截断取前 N 个**
- N **大于** case 数 → **轮转复制到恰好 N 个 VM**（同 case 的多个 VM 各自独立 payload/rootfs，CPU 仍不相交）

```bash
sudo ./bwx replay --case-file cases.txt --concurrency 2   # 5 个 case 只跑前 2 个
sudo ./bwx replay --case-file cases.txt --concurrency 8   # 3 个 case 复制到 8 个 VM
```

### 3. CPU 绑核（四种方式，互斥）

| 参数 | 语义 |
|---|---|
| `--cpu-list "38,40,42-48"` | 显式 CPU 池（支持 a-b 区间），按 vcpus 逐 VM 顺序切片 |
| `--cpu-list-file FILE` | 同上，从文件读（文本：逗号/空格/换行/区间；或 JSON 数组 / `{"cpus":[...]}`） |
| `--cpu-start 38 --cpu-step 1` | 自动生成 `start + i*step` 序列，再按 vcpus 切片 |
| 不传 | 从 `--numa-node` 自动分配（SMT 感知、互不相交） |

**每 VM 恰好 `vcpus` 个 CPU**，逐 vCPU 线程独占绑核；Firecracker 控制面线程
（主线程/API/串口）不占专属 CPU，与 vCPU 共享 VM 的亲和掩码。

**跨 NUMA node 合法**：池可以跨 node（如从 38 连续用到 181），VM 的
`numactl --membind` 绑到它自己的 CPU 所在 node 集合，内存仍在本地域内。
所有放置校验：跨 VM CPU 互不相交、CPU 在线；`node` 字段保留主 node（首个
CPU）用于报表与带宽归因。

**SMT 密度由 step 表达**（本机兄弟对是相邻编号，38/39 同物理核）：

- `--cpu-step 1`：紧凑打包，VM 的 2 个 vCPU 落在同一物理核（半核/VM 密度）
- `--cpu-step 2`：每个 vCPU 独占一个物理核

`--numa-node` 仅在自动分配路径使用。

### 4. 填充模式（`--fill`）

常规模式下每个 VM 跑完即结束，先完成的 VM 会让机器负载掉下来。填充模式让负载
持续到批次结束：

- 首批启动的 N 个 VM 是**真实 VM**（其记录是测量样本）
- 某个真实 VM 完成、且还有真实 VM 在跑 → **立即在它的 CPU 槽位启动同 case 的填充 VM**（填充 VM 自己跑完了也会重启，保持槽位不空转）
- **最后一个真实 VM 结束 → kill 全部存活的填充 VM**（SIGKILL 进程组）
- 日志分离：真实 VM 写 `real/`、填充 VM 写 `filler/`，记录带 `kind: real|filler`
- 填充 VM 的记录标 `status: cancelled`（`verification.ok = None`，明确"是主动撤掉的负载，不是失败"，也永远不进测量样本）

```bash
sudo ./bwx replay --case-file cases.txt --concurrency 4 --fill --cpu-start 38 --cpu-step 2
```

### 其余参数

`--vcpus/--mem-mib`（VM 规格）、`--numa-node`（自动分配路径的 node）、
`--case-timeout/--boot-timeout/--action-timeout`（三级预算）、`--wait on/off`
（轨迹延迟重放）、`--console off/markers/full`（串口档位）、`--keep-disks`（保留
scratch 盘）、`--out DIR`、`--dry-run`（只打印计划）。

**资源采集**（两个采集器共用 `--sample-interval`，默认 1s；各自
`--bandwidth on|off` / `--cpu-stats on|off` 控制，默认看 config 的
`bandwidth_enabled` / `cpu_stats_enabled`）：

- **内存带宽**（`perf` DDRC uncore PMU）：per-node 读写 GB/s 序列
  （`bandwidth-series.json`），并按每 VM 的 CASE_START→CASE_DONE 窗口把
  peak/mean 贴进 record 的 `bandwidth` 字段
- **CPU 利用率**（`/proc/stat` 差分，纯用户态）：`cpu-series.json` 含
  `pool`（本组绑核 CPU 并集的 busy%——VM 池压力，不受干扰）、每 node 全量
  （node 比 pool 高 = 未绑核 CPU 上有别人的负载，哨兵）、`total`；record 里
  贴 `cpu_util.pool_pct`（窗口统计）
- **per-VM CPU**：record 的 `host_cpu_s` / `host_vcpu_util`（VMM 进程
  utime+stime 差分，并发不互相污染；旧的 host_user_s 在并发下不可比）
- 顺带读 `/proc/pressure/{cpu,memory}` PSI（本机内核未开 PSI 时优雅跳过）

退出码：全部真实 VM completed/budget_exhausted → 0，否则 1。

## linearity：并发线性度摸底（`bwx linearity`）

先从 case 产物目录提取可重放清单（`--strict` 会顺带校验 dataset 轨迹可加载）：

```bash
sudo ./bwx case-list --strict --out my-cases.txt   # 或 bwx case-list > my-cases.txt
```

`case-list` 的 stdout 是干净的 id 清单（一行一个，可重定向），跳过项和摘要走
stderr；`--json` 给机器可读输出（含 steps / delay_sum_s / 跳过原因）。

然后按并发档位批量驱动 `bwx replay --fill`，测量每个 case 的耗时随并发的增长，
汇总成 CSV。两层循环全部由入参驱动，脚本不设并发上限（CPU 预算是调用者的
责任，见 preflight）：

```
for level in levels:                      # --levels 1,2,4,8,...
    shuffle case 清单（seed 由 run id + level 派生，落盘可复现）
    按 level 切成连续的组，组间串行
    每组: bwx replay --cases <组> --concurrency level --fill ...
```

- **尾组**：最后一组 case 数不足 level 时，由 replay 的轮转复制契约补满到
  恰好 level 个真实 VM——每组都是满压测量；尾组 case 会在该档产生多条样本，
  CSV 取均值
- **耗时口径**：`case_wall_s`（guest 时钟 CASE_START→CASE_DONE，不含
  boot/teardown，串口噪声免疫）；filler 永不进样本
- **preflight**：先 dry-run 最大档（CPU 池不足 / 离线 CPU 立即报错，不浪费
  几小时的排期），并校验全部 case 可加载
- **断点续跑**：每个组处理完（无论成败）打 `.done` 标记；重跑同一 `--out`
  跳过已标记组；Ctrl-C 只中断当前组（SIGINT 转发给 replay，VM 正常 teardown），
  该组不打标记、下次重跑
- **失败策略**：验证失败 / 超时的样本不入均值，CSV 该格留空，批次继续
- **shuffle 开关**：默认每档 shuffle 一次（seed 落盘可复现）；`--no-shuffle`
  按传入顺序分组（组构成稳定，便于复现/排查）
- **串口档位**：`--console off|markers|full` 逐组透传（默认沿用 config 的
  `markers`）。markers/full 时协议行（BOOT/READY/STEP/CASE_DONE）留在
  `n<level>/g<idx>/real/vm-*/console-*.log`，可直接目检执行是否成功；
  `--console off` 丢弃串口

```bash
sudo ./bwx linearity --case-file cases.txt --levels 1,2,4:8,24:48:8 \
    --cpu-start 38 --cpu-step 1 --out logs/linearity/run1 --console markers
```

`--levels` 每个逗号段可以是整数或**含端点**切片 `start:stop[:step]`（bash
`{1..6}` 风格）：`1,2,4:8,24:48:8` = 1,2,4,5,6,7,8,24,32,40,48；跨段自动去重
升序；空区间（`5:3`）、step<1、非整数段直接报错。

输出（都在 `--out` 下）：

| 文件 | 内容 |
|---|---|
| `linearity.csv` | 行 = case，列 = 并发档，值 = mean(case_wall_s) 秒 |
| `linearity_ratio.csv` | 同形状，相对该 case 最低有数据档的倍数（n1 = 1.000） |
| `linearity_detail.csv` | 长表：每条真实 VM 记录（level/group/slot/status/verified/…） |
| `n<level>/g<idx>/` | 单组 replay 输出（result.json、real/、filler/、带宽、bwx.log） |
| `n<level>/seed.txt`、`plan.txt` | 该档的 shuffle seed 与分组计划（可复现） |

只重算 CSV 不跑 VM：`sudo ./bwx linearity --out <dir> --summarize-only`。

### 结果切片（`bwx slice`）

从 sweep 的结果矩阵中切子集，`--levels` 用与 sweep 相同的含端点切片语法选
**列**（不存在的档警告并跳过），`--cases`/`--top N` 选**行**（top = 最高档值
最大的 N 个 case）：

```bash
sudo ./bwx slice --sweep logs/linearity/run1 --levels 1,8:48:8 --top 10     # 最慢 10 个
sudo ./bwx slice --sweep logs/linearity/run1 --levels 4:16:4 --cases a,b --metric ratio
sudo ./bwx slice --sweep logs/linearity/run1 --levels 8:48:8 --save sub.csv # 子矩阵落盘
sudo ./bwx slice --sweep sub.csv --levels 8:48:8                            # 再切已存的子矩阵
```

`--sweep` 是**输入**：sweep 目录（按 `--metric` 读其中的 linearity*.csv）或
直接一个 CSV 文件（比如 `--save` 的产物可以再切）。`--metric
seconds|ratio` 选耗时矩阵或倍数矩阵；默认打印对齐表格，`--save` 写的 CSV
与 `linearity.csv` 同构。
透传参数：`--cpu-start/--cpu-step`、`--vcpus/--mem-mib/--numa-node`、
`--case-timeout/--boot-timeout/--action-timeout/--bandwidth-interval`、
`--no-tools-disk`、`--discard-work`、`--group-timeout`（组级兜底超时，默认
2400s，SIGINT→SIGKILL）。

退出码：全部组成功 → 0，存在 vm-failed/timeout/error 组 → 1。

CPU 预算提示：每 VM 恰好 `vcpus` 个 CPU（无 helper 附加），池可以跨 NUMA
node。本机 node2 = 38-107、node3 = 108-181：`--cpu-start 38 --cpu-step 1`
（vcpus=2）可连续供 72 个 VM，48 并发用 38..133 即可。池不够时 preflight
会 fail-fast。

## 转换是如何执行的（`bwx convert`）

**输入**：dataset 的 `manifest.csv` + root podman 存储里的镜像 + 轨迹 JSON。
每个 case 依次：

```
1  定位镜像     manifest 查 image tag → canonical 名 → root podman 查镜像 ID
2  读镜像配置   podman image inspect → Env / WorkingDir / 架构（不符拒绝）
3  登记轨迹     校验 replay JSON（action 非空、delay 有限非负），只登记不注入
4  空间预检     剩余 ≥ 镜像Size×1.3 + 8GiB
5  导出 rootfs  podman create --entrypoint /bin/true → export 流式写 tar → 解包
6  注入 /bwx    init.sh（stub PID1）+ image-env.sh（镜像环境）；预建挂载点
7  估尺寸制盘   du -sb×1.3+2GiB（下限 4GiB）；mkfs.ext4 -q -F -m 0
                -O ^metadata_csum_seed -d rootfs（免挂载制盘）→ e2fsck -fn
8  原子落位     rename 成 image.ext4 → sha256 → base-image.json 登记
```

- **幂等**：已转换且 sha 对得上 → 跳过
- **镜像极薄**：ext4 只有 rootfs + 2 个文件；轨迹/框架/python 都不进镜像
  （分别来自 payload 盘和共享工具盘）
- 产物生成后**只读**，replay 用 reflink 副本启动，前后 sha 对账

## 重放是如何执行的

一次 VM = 三块盘：`/dev/vda` rootfs（交付镜像 reflink 副本）、`/dev/vdb` payload
（每次现做：框架+actions+steps+result 区）、`/dev/vdc` tools（共享只读，python3 +
loopback，缺 python 的镜像按需补、不遮蔽自带解释器）。

- **每轮参数走内核 cmdline**（`bwx_token/超时/wait`），镜像与 payload 模板零差异
- guest 链：stub init → payload init（loopback、PATH）→ supervise（双阶段预算）→
  run.sh 逐动作 **source 执行**（shell 状态跨步保留；输出写每步日志文件，不进串口）
- 结果双通道：串口 `BWX_RESULT:<crc>:<json>` + 盘上 summary.json，互验
- 验证：证据链 BOOT→READY（注入 CRC 对账）→全 STEP→CRC→盘一致→放置校验；
  Firecracker 在 `panic=1 reboot=k` 下 init 失败也退出码 0，所以退出码 0 不算成功

## 输出布局（logs/replay/&lt;stamp&gt;/）

```
result.json              汇总：mode/concurrency/real[]/filler[]/统计
environment.json         运行环境快照 + 完整 VM 计划
real/vm-<case>-slot<k>/  真实 VM：result/config/console/guest/result...
filler/vm-<case>-slot<k>-f<n>/   填充 VM（同样结构）
bandwidth-*.csv/json     ddrc 采集（按 node GB/s 序列）
```

单 VM 目录 ~1 MiB：rootfs/payload scratch 盘在结果抽取后自动删除
（`--keep-disks` 可保留）。计时主指标 `actions_s` 来自 guest 纳秒计数，
不受 host 串口转发延迟影响；带宽只积分 [CASE_START, CASE_DONE] 窗口。

## 串口三档（`--console`）

| 模式 | host 开销 | 验证 |
|---|---|---|
| `off` | 零（无 console=，stdout→/dev/null） | 盘校验，标记 `serial_verification=disabled` |
| `markers`（默认） | 几乎零（只有协议行；失败保留原始流） | 完整 CRC + 双通道互验 |
| `full` | 高（含内核日志） | 同 markers，可看启动问题 |

## 目录布局

```
cases/<instance_id>/image.ext4     交付镜像（convert 产物，只读）
runtime/python/                    仓库内置 python 运行时（工具盘的源）
work/tools/                        共享只读工具盘 + loopback 编译缓存
logs/replay/<UTC时间戳>/           每次重放的输出
src/bwx/                           库与命令；src/bwx/guest/ 是 guest 框架
tests/                             离线单元测试（不需要 root/KVM）
probe/                             C 探针（loopback）
```

## 平台事实（本机实测）

- ddrc uncore PMU（`hisi_sccl<N>_ddrc<M>/flux_rd|wr/`，32 B/count）是带宽真值
  来源；`perf stat -x,` 的 CSV 输出在 **stderr**
- sccl↔node 映射由 doctor 实测（node0=sccl3, node1=sccl1, node2=sccl11, node3=sccl9）
- 串口是被测干扰源（前人实测 16 并发占 host ~28% sys 时间）：动作输出永不进串口
- perf 只能按 node 聚合带宽，并发场景无法给单 VM 归因（已知局限）
- 曾经的 MPAM/resctrl 层（域映射、档位吸附、组复用、RMID 耗尽问题）见 git
  历史与 docs/REPOSITORIES.md、EXPERIMENT-PLAN.md

## 测试

```
PYTHONPATH=src python3 -m unittest discover -s tests -v    # 71 个离线测试
```

覆盖：cksum 一致性、串口协议（分帧/粘连/CRC/panic 判失败/budget 截断）、
case 文件解析（txt/json 三形态）、concurrency 截断/复制契约、CPU 池解析与
start+step 生成、placement 不相交/SMT 安全、payload steps 契约。
