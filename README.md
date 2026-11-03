# bwx -- MPAM 内存带宽沙箱实验工具

一个自包含的实验工具：把 SWE-bench podman 镜像转成 ext4、用 Firecracker 重放轨迹、
通过 resctrl(MPAM) 限制内存带宽、用 ddrc uncore PMU 测量真实带宽，最后做敏感度分类
和带宽分配策略对比。标准库 Python 3.9+，无第三方依赖。

## 快速上手

```bash
cp config.example.toml config.toml     # 改 dataset / firecracker / kernel 路径
sudo ./bwx doctor                      # 体检：工具/KVM/resctrl/ddrc PMU/数据集
sudo ./bwx convert --all               # 镜像 -> cases/<id>/image.ext4（幂等）
sudo ./bwx replay --case tikv__pd-5480 # 单 VM 重放
sudo ./bwx replay --case tikv__pd-5480 --bandwidth 54 --numa-node 2   # MPAM 限制下重放
```

优先级：命令行 > 环境变量（`BWX_*`）> config.toml > 内置默认。
碰 KVM/resctrl/podman 的命令都要 root（`sudo ./bwx ...`，或先 `sudo -v` 缓存凭据）。

命令全景：

```
doctor      机器体检（工具/KVM/resctrl/ddrc PMU/数据集/可建组数）
calibrate   Phase 0：ddrc 标定、MB域↔node 映射、档位吸附表、档位上限
convert     podman 镜像 -> ext4 交付镜像（幂等，见下文）
replay      单 VM 重放（可上 MPAM 档位，见下文）
sweep       Phase 1：case × 档位 × 并发 敏感度矩阵
alloc       Phase 3：并发多 case 分配策略对比（none/shared/equal/demand/spread）
analyze     Phase 2/4：退化率/分类/拐点/N*/策略对比
list        已转换 case 一览
cleanup     清理遗留 resctrl 组与 scratch
```

## 使用指南：按实验计划逐步执行

下面按 EXPERIMENT-PLAN.md 的 Phase 0→4 展开每一步的具体命令、可调参数、
以及**修改并发与 MPAM 参数**的方法。

### 第 0 步：环境体检与平台标定

```bash
sudo ./bwx doctor                          # 必须先跑：15+ 项检查
sudo ./bwx calibrate --node 2 --duration 4 # Phase 0 标定
```

`doctor` 检查（任一 FAIL 先修复再实验）：工具链 / sudo / KVM / firecracker 与
kernel 路径（**错路径 = VM 起不来且 console 全空**）/ dataset / resctrl 挂载与
MB 域 / ddrc PMU 可读 / **当前可建 resctrl 组数** / 各 node CPU / 磁盘。

`calibrate` 参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--node N` | 第一个有 CPU 的 node | 标定发生的 NUMA node |
| `--duration S` | 6 | 每个测量点秒数 |
| `--repeats N` | 3 | 每点重复次数 |
| `--threads N` | 8 | RMW 探针线程数（要超过档位上限才测得出 ceiling） |
| `--buffer-mib N` | 256 | 每线程缓冲（必须大于 LLC slice，否则测的是缓存） |
| `--levels LIST` | 由 gran 推导 | 要探测的档位 |
| `--max-groups N` | 8 | ceiling 步骤最多建几个 resctrl 组（防 RMID 耗尽） |
| `--skip-scale/-mapping/-snap/-ceiling` | - | 跳过对应测量 |

产出 `calibration.json`：**level snap 表**（请求档→实际档的吸附映射，决定
后面 sweep 能用的档位集合）、**ddrc scale**（应为 ~32 B/count）、**域↔node
映射**、各档位 ceiling。

### 第 1 步：转换镜像

```bash
sudo ./bwx convert --all                        # podman 里全部可转的 case
sudo ./bwx convert --case tikv__pd-5480         # 指定 case（可重复）
sudo ./bwx convert --case-file my-cases.txt     # 清单文件（# 注释）
```

参数：`--case ID`（可重复）/ `--case-file FILE` / `--all` /
`--image <podman镜像ID>`（绕过 manifest）/ `--min-gib N`（镜像下限尺寸）。
幂等，重跑自动跳过已转换的。转换细节见下文「转换是如何执行的」。

### 第 2 步：单 VM 重放（Phase 1 的基本单元）

```bash
sudo ./bwx replay --case tikv__pd-5480 --numa-node 2
sudo ./bwx replay --case tikv__pd-5480 --bandwidth 9 --numa-node 2     # 限 9% 带宽
sudo ./bwx replay --case tikv__pd-5480 --bandwidth 54 --hard-limit     # 加 MBHDL 硬限
sudo ./bwx replay --case aftership__clickhouse-sql-parser-128 --vcpus 4 --mem-mib 8192
```

**MPAM 参数（单次重放）**：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--bandwidth PCT` | 不限 | MB 档位 1..100。**0 一律拒绝**（会挂机）。内核会吸附到实际档（gran=9 时 50→54），记录里存 applied 值 |
| `--group NAME` | bwx-replay-<pct> | resctrl 组名；组已存在则直接复用（**不会改它的配置**） |
| `--hard-limit` | off | 同时写 MBHDL=1（仅内核支持时；本机未暴露） |

**VM 规格/放置参数**：`--vcpus N`（默认 2）、`--mem-mib N`（4096）、
`--numa-node N`（config）、`--cpu-list 0,2,4`（显式给 vcpus+1 个 CPU，必须都在
所选 node 上，SMT 兄弟会被拒绝）。

**行为参数**：`--case-timeout S`（1800，CASE_START 后的 guest 预算）、
`--boot-timeout S`（120，启动阶段预算）、`--action-timeout S`（600，单步看门狗）、
`--wait on/off`（off，on=重放轨迹 delay）、`--console off/markers/full`
（markers）、`--bandwidth-interval S`（1，ddrc 采样间隔）、`--no-tools-disk`、
`--keep-disks`、`--out DIR`。

每条命令的输出判定：退出码 0 = completed/budget_exhausted；结果细节看
`logs/replay/<stamp>/result.json`（含 `bandwidth.node2.peak_gbs/mean_gbs`）。

### 第 3 步：敏感度扫描（Phase 1 主体——**并发与档位在这里调**）

```bash
# 基本：每 case × 每档位 × 每重复，1 个 VM 独占 node（本征敏感度）
sudo ./bwx sweep --case-file my-cases.txt --nodes 2

# 修改档位集合（默认由 calibrate 的 gran 推导；细粒度机器建议显式给全低区）
sudo ./bwx sweep --case tikv__pd-5480 --levels 1,2,3,4,5,6,7,8,9,18,26,54,97

# 修改并发：N 个同 case 副本共享同一个 MPAM 档位（"放大"模式）
sudo ./bwx sweep --case tikv__pd-5480 --concurrency 8 --levels 9,26,54 --nodes 2

# 重复次数（降噪，退化率取中位数）
sudo ./bwx sweep --case-file my-cases.txt --repeats 3

# 先看计划不执行
sudo ./bwx sweep --case-file my-cases.txt --concurrency 4 --dry-run
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--concurrency N` | **1** | **并发修改点**：每格同时起 N 个 VM，共用一个 MPAM 组（每 VM 实得 ≈ 档位/N）。N>case 数时自动复制为 `<case>#k` 副本（独立 payload+rootfs 副本，交付盘共享只读）。N=1 测本征敏感度，N>1 测竞争敏感度——**别混在一个 sweep 里比** |
| `--levels LIST` | gran 推导 | 档位集合；吸附后去重，只跑实际不同的档 |
| `--repeats N` | 1 | 每格重复 |
| `--nodes LIST` | 全部 node | 允许使用的 NUMA node（格子间轮转分配） |
| `--stagger S` | 0 | 同格 N 个副本的启动间隔（秒）；0=同刻启动（配合不相交 CPU 分配） |
| `--no-baseline` | off | 跳过 full 基线格（**不建议**：退化率需要分母） |
| `--hard-limit` | off | 所有组加 MBHDL |
| `--wait` / 超时族 / `--out` | 同 replay | 透传给每个 VM |

产出：`sweep.csv`（每 VM 一行：case/level/N/replica/actions_s/node/带宽列）、
`sweep-summary.csv`（每 case×档位 中位数与退化率）。组按**档位复用**（整
个 sweep 一档一组，防 RMID 耗尽），格子结束不删组。

**档位语义提醒**：并发 N 下 VM 实得 ≈ applied/N（MB 是组级上限，内核按需求
比例分）；分析 N* 时用 `--sweep` 传多次不同并发的输出目录。

### 第 4 步：分析与需求建模（Phase 2）

```bash
./bwx analyze --sweep logs/sweep/<stamp> --emit-demand demand.json

# 多个并发档的 N* 计算：--sweep 可重复传
./bwx analyze --sweep logs/sweep/n1-stamp --sweep logs/sweep/n4-stamp \
              --sweep logs/sweep/n8-stamp --emit-demand demand.json

# 调分类口径
./bwx analyze --sweep logs/sweep/<stamp> \
              --tolerance 3 --hungry-threshold 20 --medium-threshold 5
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--tolerance PCT` | 5.0 | 退化率多少以内算"无损失"（决定拐点/knee） |
| `--hungry-threshold PCT` | 15.0 | 最低档退化超过此值 → hungry |
| `--medium-threshold PCT` | 3.0 | medium/light 的分界 |
| `--emit-demand FILE` | - | 导出 `bwx alloc --demand` 需要的需求表 |
| `--sweep DIR`（可重复） | - | sweep 输出目录或直接指 sweep.csv |
| `--alloc DIR` | - | alloc 输出，做策略对比 |

产出：`sensitivity.csv`（case×档位 退化率）、`case-classes.csv`（分类、knee、
cliff、N*、demand_gbs、min_acceptable_level）。

### 第 5 步：分配策略对比（Phase 3——**并发与 MPAM 分配都在这里调**）

```bash
# 预算 4 VM、shared/equal 用 54 档
sudo ./bwx alloc --case tikv__pd-5480 --case moov-io__customers-275 \
     --strategies none,shared,equal --concurrency 4 --cap 54 --nodes 2

# 差异化分配（demand 策略）：总预算 200 = 平均每人 50，按需求加权
sudo ./bwx alloc --mix mix.json --strategies demand,spread \
     --demand demand.json --concurrency 4 --budget 200

# 先看每个 VM 拿到什么组/档/node
sudo ./bwx alloc ... --dry-run
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--concurrency N` | **4** | **并发修改点**：同时运行的 VM 数（mix 不足 N 时轮转复制补齐） |
| `--cap PCT` | 54 | shared/equal 策略的统一档位 |
| `--budget PCT_SUM` | 200 | **MPAM 总预算修改点**：demand 策略所有 VM 的档位之和（按各自 demand 加权分配后取整，钳到 1..100） |
| `--strategies LIST` | none,shared,equal,demand | `none`=不限 / `shared`=全 VM 一个组一个档 / `equal`=每 VM 一个组同档 / `demand`=每 VM 独立组独立档（按 demand.json 的 min_acceptable_level 加权）/ `spread`=再叠加跨 node 分散 |
| `--demand FILE` | - | demand 策略必填（analyze --emit-demand 产出） |
| `--mix FILE` | --case 集合 | 混合负载定义：`{"名称":{"caseID":{"role":"hungry","weight":2}}}` |
| `--repeats N` | 2 | 重复轮（策略顺序轮转消除冷启动偏差） |
| `--nodes LIST` | 全部 | spread 用的 node 池 |
| `--stagger S` / `--seed N` | 0 / 0 | 启动间隔 / 计划洗牌种子 |
| `--hard-limit` | off | 全部组加 MBHDL |

产出：`alloc.csv`（每 VM：策略/组/档/node/actions_s）、`alloc-summary.csv`
（每策略 mean/median actions、makespan、与 none 的配对差）。

### 第 6 步：结论（Phase 4）

```bash
./bwx analyze --alloc logs/alloc/<stamp>
```

看 `alloc-summary.csv`：demand 是否在 makespan/吞吐上赢过 shared/equal，
退化分布是否更公平（配对剔除未完成批次的 VM，避免崩溃美化某策略）。

### 修改 MPAM 参数的三个层级汇总

| 想改什么 | 在哪改 | 示例 |
|---|---|---|
| 单次重放的档位 | `bwx replay --bandwidth` | `--bandwidth 26` |
| 扫描的档位集合 | `bwx sweep --levels` | `--levels 1,2,...,9,18,26,54,97` |
| 每档硬限 | 任何命令 `--hard-limit` | 内核须暴露 MBHDL |
| 统一分配档位 | `bwx alloc --cap` | `--cap 54` |
| 差异化总预算 | `bwx alloc --budget` | `--budget 300`（5 VM 均值 60） |
| 组名（复用已存在的组） | `replay --group` / sweep 自动按档位命名 | 组存在时**原样复用不改配置** |
| 全机兜底（组耗尽时） | config.toml `allow_root_group = true` | 会作用于所有租户，记录 adopted_root |
| 0 档 | **任何途径都改不了** | 硬校验拒绝（曾挂机） |

### 修改并发的三个层级汇总

| 想改什么 | 在哪改 | 语义 |
|---|---|---|
| 单格同 case 复本数 | `sweep --concurrency N` | N 个 VM **共享一个组**，测竞争放大（实得 ≈ 档位/N） |
| 批次总 VM 数 | `alloc --concurrency N` | 异构 mix，策略决定组的分配 |
| VM 自身 vCPU | `replay/sweep/alloc --vcpus` + `--mem-mib` | 每 VM 诉求（占用 vcpus+1 个 host CPU） |

上限提醒：每 VM 需要 `vcpus+1` 个**物理核**（SMT 感知分配），node0/1 各 10 核
只能 3 VM，node2/3 各 35+ 核可以 11-12 VM；allocator 会在放不下时报错而不是
超卖。

## 转换是如何执行的（`bwx convert`）

**输入**：dataset 里的 `manifest.csv`（case→镜像 tag 映射）+ root podman 存储
里的镜像 + `replay-50/<id>.replay.json` 轨迹。**镜像必须已经在 root podman 里**
（`sudo podman images` 可见；没有就先 `sudo podman pull`）。

每个 case 依次经过：

```
1  定位镜像     manifest.csv 按 instance_id 查 image_arm64 tag → 去掉 registry
                前缀的 canonical 名 → 在 root podman 存储查镜像 ID
2  读镜像配置   podman image inspect → Config.Env / WorkingDir / 架构 / Size
                （架构与 host 不符直接拒绝）
3  登记轨迹     读取并校验 replay JSON（每步 action 非空、delay_time 有限非负），
                记下 sha256/步数/延迟总和——只登记，不注入
4  空间预检     剩余空间 ≥ 镜像 Size×1.3 + 8GiB，不够就报错
5  导出 rootfs  podman create --entrypoint /bin/true（不启动容器）→ podman export
                流式写 rootfs.tar（多 GB 不进内存）→ tar --numeric-owner
                --xattrs --acls 解包 → finally 里 podman rm 清容器
6  注入 /bwx    两个文件：init.sh（stub PID1，见"重放"节）+ image-env.sh
                （镜像 Config.Env 逐条 export + BWX_IMAGE_CWD）；预建
                dev/proc/sys/run/tmp/work 挂载点；镜像已有 /bwx 或缺
                /bin/sh 则拒绝
7  估尺寸制盘   du -sb（表观大小）×1.3 + 2GiB（下限 4GiB）；inode=文件数×2；
                truncate 稀疏文件 → mkfs.ext4 -q -F -m 0 -O ^metadata_csum_seed
                -d <rootfs>（-d 直接灌目录树，全程无 loop 挂载）→ e2fsck -fn
8  原子落位     .partial rename 成 image.ext4 → sha256 → 写 cases/<id>/
                base-image.json（id/tag/路径/sha/working_dir/轨迹元数据）
```

关键性质：

- **幂等**：已转换且 sha 对得上 → 跳过；产物与登记不符 → 报错要求显式删除重建
- **镜像极薄**：ext4 里只有 rootfs + 2 个文件。轨迹、guest 框架、python 都**不在**
  镜像里（分别来自 payload 盘和共享工具盘），所以改这些不用重转镜像
- **python 不进镜像**：3/4 镜像缺 python3，由共享只读工具盘在运行时补（见下文）
- 产物 `cases/<id>/image.ext4` 生成后**只读**：replay 用 reflink 副本启动，
  前后 sha256 对账

常用参数：`--case ID`（可重复）、`--case-file FILE`、`--all`（podman 里
所有可转换的 manifest case）、`--image <podman镜像ID>`（绕过 manifest 直接转）、
`--min-gib N`（下限尺寸）。典型耗时：每 GiB 镜像约 1 分钟（export+tar+mkfs）。

## 重放是如何执行的（`bwx replay`）

**输入**：一个已转换的 case。一次运行 = 三块盘 + 一个 guest 生命周期：

| 盘 | host 路径 | guest 内 | 角色 |
|---|---|---|---|
| rootfs | 交付镜像的 reflink 副本 | `/dev/vda` | 镜像 rootfs（写时复制，交付盘不动） |
| payload | 每次运行现做（毫秒级） | `/dev/vdb` → `/work` | 框架+actions+steps+result 区 |
| tools | `work/tools/tools-*.ext4`（缓存） | `/dev/vdc` → `/opt/bwx-tools`（只读） | 共享 python3 + loopback |

host 侧阶段：

```
准备   读轨迹 → 生成 payload 树（run.sh/supervise.sh/init.sh/steps.txt/
       actions/<i>.sh）→ mkfs 成盘 → reflink 拷 rootfs 副本 → 写 Firecracker
       配置（--no-api，无网络，两/三块盘，每轮参数全部走内核 cmdline：
       bwx_token/超时/wait）
启动   numactl --physcpubind=<cpus> --membind=<node> 包 VMM；有 --bandwidth 时
       外面再包一层 MPAM join 启动器（写自身 PID 进 resctrl tasks 再 exec，
       vCPU 线程全部继承组限制）
观测   轮询循环：逐线程绑核（fc_vcpu N→各自 CPU，其余→helper CPU）、采
       /proc 样本、读串口增量解析协议行、成员归属后台采样；host 侧只做
       兜底超时（guest 内监督器才是主看门狗）
收尾   VM 退出后：debugfs 从 payload 盘抽取 /work/result →（串口 summary 与
       盘上 summary 互验 + 全套校验）→ 删除两块 scratch 盘 → 写 result.json
```

guest 侧执行链：内核 `init=/bwx/init.sh`（stub）→ 挂文件系统 → 找到 payload 盘
→ `exec /work/init.sh`（起 loopback、按需加 PATH）→ `supervise.sh`（双阶段预算
看门狗：boot_timeout / case_timeout）→ `setsid run.sh`（逐动作执行）。

**动作执行规则**（`/work/run.sh`）：

- 逐行读 `steps.txt`（`<index> <delay_s>`），`. actions/<i>.sh` **以 source 方式
  执行**——cd/export 的变量/shell 函数跨步保留（轨迹本就是一个持续会话录的）
- 每步的 stdout/stderr 重定向到 `result/actions/<i>.log`，**不进串口**
- 每步有独立 watchdog（默认 600s，`--action-timeout` 可调）
- `--wait on` 时每步前 `sleep delay`（重放模型延迟；默认 off）
- 退出码 126/127（缺解释器/不可执行）→ 记 BWX_ERROR 并中止：这种步的计时无意义
- 轨迹末尾的裸 `submit` 命令由预定义函数兜底（落 `submitted.patch`）

**结果双通道**：串口 `BWX_RESULT:<crc>:<json>`（快，带 CRC）+ payload 盘
`/work/result/summary.json`（权威）。两者必须一致，任一不符判 `result_mismatch`。

常用参数：

| 参数 | 作用 | 默认 |
|---|---|---|
| `--case ID` | 要重放的 case（必填） | - |
| `--bandwidth PCT` | MPAM 档位 1..100（0 拒绝） | 不限制 |
| `--group NAME` | resctrl 组名 | bwx-replay-<pct> |
| `--numa-node N` / `--cpu-list 0,2,4` | 放置（自动分配或显式指定） | config |
| `--vcpus N` / `--mem-mib N` | VM 规格 | 2 / 4096 |
| `--case-timeout S` / `--boot-timeout S` | guest 预算（CASE_START 前后两段） | 1800 / 120 |
| `--action-timeout S` | 单步看门狗 | 600 |
| `--wait on/off` | 重放轨迹延迟 | off |
| `--console off/markers/full` | 串口捕获档位（见下） | markers |
| `--no-tools-disk` | 不挂 python 工具盘 | 挂 |
| `--keep-disks` | 保留 scratch 盘（默认抽取后删除） | 删 |
| `--out DIR` | 输出目录 | logs/replay/<UTC时间戳> |

**共享工具盘**（首次自动构建，缓存在 `work/tools/`）：python 运行时优先取
**仓库内的 `runtime/python/`**（261 MiB，conda 布局：bin + stdlib + .so 闭包，
随 git 走，clone 即自包含），找不到再看 `<dataset>/python-deps/`。staging 时
裁剪 stdlib + 收集 ELF 依赖闭包 + 私有 loader 启动（不触碰 guest libc；host 是
openEuler、镜像是 Ubuntu 22.04，混 libc 必炸）。构建时自测
`import ssl/sqlite3/...`。guest 内 `command -v python3` 失败才前置它进
PATH——**不遮蔽镜像自带解释器**。

## 目录布局

```
cases/<instance_id>/image.ext4     交付镜像（convert 产物，之后只读）
runtime/python/                    仓库内置 python 运行时（工具盘的源，261 MiB）
work/tools/                        共享只读工具盘 + loopback 编译缓存
logs/<command>/<UTC时间戳>/        每次命令的输出（VM 目录、CSV、环境快照）
src/bwx/                           库与命令；src/bwx/guest/ 是 guest 框架
tests/                             离线单元测试（不需要 root/KVM）
probe/                             C 探针（带宽负载 bwx_probe、loopback）
```

## 串口协议与正确性

guest 与 host 之间通过串口传一行一条的 ASCII 标记（`BWX_BOOT/READY/CASE_START/
STEP/CASE_DONE/RESULT/EXIT/ERROR`），每行带本次运行的随机 token，`BWX_RESULT` 的
JSON 载荷用 POSIX `cksum` CRC 保护。验证是强制的（可显式关闭但会被打上
`unverified` 标记）：成功需要正向证据链 BOOT→READY（payload 注入 CRC 对账）→
全 STEP 序列完整→CRC 正确的 RESULT→与结果盘 summary.json 一致→放置校验（vCPU
绑核、guest RAM 落在绑定 node）。Firecracker 在 `panic=1 reboot=k` 下即使 init
失败也退出码 0，因此"退出码 0"永远不构成成功证据。

串口捕获三档（`--console`）：

| 模式 | host 开销 | 保留内容 | 验证 |
|---|---|---|---|
| `off` | 最低（内核 cmdline 去掉 console=，stdout→/dev/null） | 无串口 | 走结果盘校验，标记 `serial_verification=disabled` |
| `markers`（默认） | 低 | 仅协议行；失败时保留原始流供诊断 | 完整串口验证 |
| `full` | 最高（含内核日志） | 全部 | 完整串口验证 + 可看内核启动日志 |

动作的 stdout/stderr 永远不进串口（写 payload 盘的每步日志），所以串口流量与
轨迹内容无关，`markers` 档几乎零成本还保留完整校验。串口是被测干扰源（前人实测
16 并发时占 host ~28% sys 时间），纯性能测量用 `off`，要正确性证据用 `markers`。

## 一次 replay 的产物（logs/replay/&lt;stamp&gt;/vm-*/）

`config.json`（VM+MPAM+放置快照）、`vm-config.json`（Firecracker 配置）、
`command.json`（完整 argv）、`console-markers.log`/`console-failure.log`、
`firecracker.log`、`metrics.jsonl`、`result.json`（含验证详情、放置校验、
MPAM 成员校验、带宽窗口统计）、`guest/result/`（summary.json、events.jsonl、
每步 actions/&lt;i&gt;.log、基线 patch、submitted.patch）、`payload/`（生成树）。

**scratch 盘不保留**：`rootfs.ext4`/`payload.ext4`（单次 1-4 GiB）在结果抽取
完成后自动删除（目录里只留 `disks-removed.txt` 标记），需要事后 debugfs 检查
时加 `--keep-disks`。整个 VM 目录因此只有 ~1 MiB。

计时口径：`actions_s`/`case_wall_s` 来自 guest 的 `date +%s%N`（不受 host 串口
转发/轮询延迟影响），是主指标；`vmm_wall_s` 是 host 墙钟（含 boot/关机），只用于
整轮记账。带宽统计只积分 [CASE_START, CASE_DONE] 窗口，排除 boot/collect 噪声。

## 实验流程速查（详见上文「使用指南」）

```bash
sudo ./bwx calibrate --node 2                                          # Phase 0
sudo ./bwx sweep --case-file cases.txt --nodes 2                       # Phase 1
./bwx analyze --sweep logs/sweep/<stamp> --emit-demand demand.json     # Phase 2
sudo ./bwx alloc --case A --case B --strategies none,shared,equal,demand \
     --demand demand.json --concurrency 4                              # Phase 3
./bwx analyze --alloc logs/alloc/<stamp>                              # Phase 4
```

## 平台事实（本机实测，doctor/calibrate 每机重测）

- MB 域 `MB:0..3` = 4 个 NUMA node 一一对应（calibrate 的 mapping 步骤实测验证）
- `bandwidth_gran=9 min=0`：请求 1..8 全吸附到 9（本机低区分辨率为 0）；
  真实实验机 gran 更细，1..9 是独立档位，所以档位集合一律由目标机推导，不硬编码
- **0 档禁止**（历史上曾导致整机挂死），所有路径硬校验
- 无 `info/MB_MON`（resctrl 只有 llc_occupancy，`num_rmids=1`）：每组带宽读不到，
  ddrc uncore PMU（`hisi_sccl<N>_ddrc<M>/flux_rd|wr/`，32 B/count，perf stat）是
  唯一真值来源；已实测 `perf stat -x,` 输出在 stderr
- **resctrl 组创建是稀缺资源**：`num_closids=32` 但 `num_rmids=1/4 pmg`，快速
  建/删循环（实测 100 档探测）会耗尽池子且**重启 resctrl 也不恢复**（ENOSPC
  "Out of RMIDs"，实测持续 >1h，仅重启机器能解）。因此 GroupPool 按"每个不同
  档位一个组"复用，绝不逐运行建删；`doctor` 会报告当前可建组数。耗尽后可选
  `allow_root_group=true`（config.toml，会全机生效并记录 `adopted_root`）
- **resctrl 文件读取可能极慢**：池子退化后实测 `tasks`/`cpus_list` 单次读
  6-10s（健康时亚毫秒）。成员校验因此放在后台线程 + 预算控制，绝不阻塞
  case watchdog；短 case 在慢机器上可能拿不到成员样本（记录为 unknown
  而非失败）
- ddrc PMU 只能按 node 聚合，并发场景无法给单个 VM 归因带宽（已知局限）

## 测试

```
PYTHONPATH=src python3 -m unittest discover -s tests -v    # 74 个离线测试
```

覆盖：cksum 与 coreutils 一致性、串口协议（跨块分帧、粘连行、CRC 截断检测、
panic=退出码 0 判失败、budget_exhausted 合法截断）、schemata 解析与零填充、
0 档拒绝、placement 不相交/SMT 安全、分配策略纯函数、分析（分类/拐点/N*/
配对剔除未完成批次）。
