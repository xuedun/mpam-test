# bwx -- MPAM 内存带宽沙箱实验工具

一个自包含的实验工具：把 SWE-bench podman 镜像转成 ext4、用 Firecracker 重放轨迹、
通过 resctrl(MPAM) 限制内存带宽、用 ddrc uncore PMU 测量真实带宽，最后做敏感度分类
和带宽分配策略对比。标准库 Python 3.9+，无第三方依赖。

```
./bwx doctor            # 机器体检：工具/KVM/resctrl/ddrc PMU/数据集
./bwx calibrate         # Phase 0：ddrc 标定、域↔node 映射、档位吸附表、档位上限
./bwx convert --all     # podman 镜像 -> cases/<id>/image.ext4（幂等）
./bwx replay --case ID  # 单 VM 重放（可加 --bandwidth 54 上 MPAM 限制）
./bwx sweep             # Phase 1：case × 档位 × 并发 敏感度矩阵
./bwx analyze           # Phase 2/4：退化率/分类/拐点/N*/策略对比
./bwx alloc             # Phase 3：并发多 case 分配策略对比（none/shared/equal/demand/spread）
./bwx list              # 已转换 case 一览
./bwx cleanup           # 清理遗留 resctrl 组与 scratch 目录
```

先复制 `config.example.toml` 为 `config.toml`（已 gitignore）并按本机改路径。
优先级：命令行 > 环境变量（`BWX_*`）> config.toml > 内置默认。
所有会碰 KVM/resctrl/podman 的命令需要 root（`sudo ./bwx ...`）。

## 目录布局

```
cases/<instance_id>/image.ext4     交付镜像（convert 产物，之后只读）
work/tools/                        共享只读工具盘（python3 + loopback，缓存）
work/vm-*/                         单次 VM 的 scratch（副本盘 + payload + 结果）
logs/<command>/<UTC时间戳>/        每次命令的完整输出（records/CSV/environment）
src/bwx/                           库与命令
tests/                             离线单元测试（不需要 root/KVM）
probe/                             C 探针（带宽负载、loopback）
```

## 串口协议与正确性

guest 与 host 之间通过串口传一行一条的 ASCII 标记（`BWX_BOOT/READY/CASE_START/
STEP/CASE_DONE/RESULT/EXIT/ERROR`），每行带本次运行的随机 token，`BWX_RESULT` 的
JSON 载荷用 POSIX `cksum` CRC 保护。验证是强制的（可显式关闭但会被打上
`unverified` 标记）：成功需要正向证据链 BOOT→READY→全 STEP→CRC 正确的 RESULT→
与结果盘上的 summary.json 一致。Firecracker 在 `panic=1 reboot=k` 下即使 init
失败也退出码 0，因此"退出码 0"永远不构成成功证据。

串口捕获三档（`--console`）：

| 模式 | host 开销 | 保留内容 | 验证 |
|---|---|---|---|
| `off` | 最低（stdout→/dev/null，内核无 console=） | 无串口 | 走结果盘校验，标记 `serial_verification=disabled` |
| `markers`（默认） | 低 | 仅协议行；失败时保留原始流供诊断 | 完整串口验证 |
| `full` | 最高（含内核日志） | 全部 | 完整串口验证 + 可看内核启动日志 |

动作的 stdout/stderr 永远不进串口（写入 payload 盘的每步日志文件），所以串口
流量与轨迹内容无关，`markers` 档几乎零成本还保留完整校验。

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
