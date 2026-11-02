# MPAM 内存带宽差异化分配实验方案

> 目标：为每个 sandbox case 建立「带宽档位 → 执行时间」敏感度曲线，据此在并发场景做**差异化带宽分配**（给访存饥饿型多、给轻载型少），最大化整体吞吐 / 最小化 makespan，优于当前「同组等分」。

---

## 0. 两套环境的区别（务必先读）

本方案涉及两台机器，**带宽档位的精度不同**，所有档位扫描集合必须由「实际测试环境」实测的 resctrl 元数据推导，**禁止硬编码某一台的吸附结果**。

| | 标定机（/home/cxd，本机） | 实际测试环境（跑实验的机器） |
|---|---|---|
| `info/MB/bandwidth_gran` | 9（粗） | 更小 / 更细（1–9 各档可区分） |
| 请求档 1–8 的行为 | 全部吸附到 **9**（≈13.7 GB/s，低端只有一个工作点） | **1–9 逐档可区分**，可继续压低带宽 |
| 低带宽区间分辨率 | 无（1–9 等价） | 有（1,2,…,9 是独立工作点） |
| 用途 | 方法学验证、ddrc 换算标定、域↔node 映射验证 | 敏感度画像与并发分配正式实验 |

**关键推论**：
1. 实际测试环境里 **1–9 是可用的低带宽刻度**，饥饿型 case 的真实退化曲线就藏在这一段，必须在低区**逐档加密扫描**。
2. 标定机的 sweep 结果（9/26/54/97 四档 wall 几乎无差）不能外推到实际环境——那里低档更细、更容易咬到访存敏感 case。
3. `min_bandwidth=0`，但 **0 档在本机历史上曾导致系统挂死重启，全线严禁**（脚本层硬校验 ≥1，实际低档下限取 1）。

---

## 1. 平台事实（标定机实测，实际环境需重测同名项）

| 项 | 标定机实测值 | 对实验的含义 | 实际环境需重测 |
|---|---|---|---|
| 架构 / NUMA | aarch64，4 NUMA node，192 CPU | VM 可按 node 物理隔离 | node 数、每 node CPU |
| sccl → node 映射 | node0=sccl3, node1=sccl1, node2=sccl11, node3=sccl9（由 ddrc PMU `cpumask` 反查） | ddrc 计数按 node 归组 | ✅ 重测（见 §5.3） |
| MB 域 | `MB:0;1;2;3` 四域 = 四 NUMA node，**可独立设档** | 差异化分配的天然维度 | 域数、域↔node |
| `num_closids` | 32（去 root ≈ 31 组） | 最多 ~31 个并发 VM 各自独立档位 | ✅ |
| `bandwidth_gran` / `min_bandwidth` | 9 / 0 | 决定有效档位集合 | ✅ **最关键** |
| `delay_linear` | 1 | 档位与延迟近似线性 | ✅ |
| MB 监控 | **无 `info/MB_MON`**，resctrl 仅 `llc_occupancy` | **每组带宽不能用 resctrl 读** | 若有 MB_MON 则优先用它 |
| 带宽真值来源 | `hisi_sccl{N}_ddrc{M}/flux_rd|flux_wr/`，scale=32 B/count，需 root（`perf_event_paranoid=2`） | 唯一的带宽观测手段 | PMU 名、scale |
| L3 CAT（可选） | `cbm_mask=fffffff`(28bit)，`sparse_masks=1` | 第二调节维度，本方案先只用 MB | — |

---

## 2. 指标定义（统一口径）

- **性能**
  - `case_wall_s`：端到端墙钟（含 boot）
  - `actions_s`：去 boot 的 guest 执行时间（主指标）
  - **退化率** `degradation = (actions_s@level − actions_s@full) / actions_s@full`
- **带宽真值**（ddrc PMU 折算）
  - node 峰值 / 均值 GB/s，区分 rd / wr
  - **VM 驻留期积分**（只统计 case 运行窗口，排除 boot/collect）
- **敏感度分类**：按「最低档（level=1）退化率」分 3 类
  - 饥饿型：> 15%
  - 中间型：3 – 15%
  - 轻载型：< 3%
- **并发聚合**：makespan、吞吐（case/小时）、退化率分布、公平性（最大/最小退化率之比）

---

## 3. 档位扫描集合（由目标环境元数据推导）

设目标环境 `gran = G`、`min = M`（从 `info/MB/` 读），有效档位 = 内核吸附后的实际档。

**低区（实际环境重点）**：逐档请求 `1,2,3,4,5,6,7,8,9`
- 标定机：这 9 个请求全部落到实际档 9（低端不可分，仅用于方法学验证）
- 实际环境：预期为 9 个独立工作点，**饥饿型退化曲线的主要观测区**

**中高区**：在吸附后的实际档上取代表点，例如
`{低区全部} ∪ {18, 26, 35, 44, 54, 72, 90, 97, full}`
- 轻载 case 在档 ≥18 通常无差异（标定机已证），故中高档**减少重复次数**（1–2 次）
- 饥饿 case 在低区**加密重复**（≥3 次）

> 落地：脚本启动时读 `info/MB/bandwidth_gran`、`min_bandwidth`，把「请求档列表」经一次 dry-run 吸附探测（写→回读）转成「实际档列表」，去重后再排实验矩阵。严禁把标定机的 9/18/26… 写死。

---

## 4. 分阶段设计

### Phase 0 — 平台标定（半天，1 组 VM，两机各做一次）
1. **域↔node 独立性**：合成访存负载 `taskset` 钉 node0，扫档，验证 **node0 的 ddrc 掉、其余 node 平** → 确认 MB 域确实按 node 隔离。
2. **ddrc→GB/s 换算**：标定 `scale_b_per_count`（标定机=32）与采样窗口；对齐 `logs/vm-bw/` 既有采集法。
3. **档位吸附表**：对 1..100 请求档做一次「写→回读」，得到本环境的 `请求→实际档` 映射，确认低区分辨率。
4. **最低安全档**：确认 0 档被拒，实际下限取 1。
- 产出：`platform-calib.md`（两机各一份：域映射、GB/s 换算、吸附表、噪声水平）。

### Phase 1 — 单 case 敏感度画像（核心，串行隔离）
- **选样**：按语言分层抽样 ~15–20 个（go / rust / ts / java / python / php / js / c），覆盖 manifest `wall_sec_arm` 的 p10 / p50 / p90。**不要只测一个 case**（标定机 sweep 只测了 `aftership__clickhouse-sql-parser-128`，峰值仅 5.37 GB/s、均值 1.51，属轻载，导致四档无差 → 采样偏差教训）。
- **隔离**：每个 case 独占一个 NUMA node（其余 node 静默），VM 绑该 node。
- **矩阵**：case × 档位（§3）× 重复（低区≥3、中高区1–2）。
- **执行**：用 `scripts/mpam-replay-e2e.sh`（单 VM），档位经 `--bandwidth`；带宽真值用旁路采集器 `perf stat -a -e <该 node 全部 ddrc>/flux_rd,flux_wr/`（脚本已有采样框架可扩展），只积分 case 窗口。
- 产出：`case-sensitivity.csv`（case, level_requested, level_applied, actions_s, degradation, node_peak_gbs, node_mean_gbs, rd_gbs, wr_gbs, run_idx）。

### Phase 2 — 分类与建模（半天）
- 按「level=1 退化率」分饥饿 / 中 / 轻三类。
- 对饥饿型拟合「档位 → 实测GB/s → actions_s」曲线，找**拐点档位**（再降就急剧退化的临界点）。
- 产出每 case：`min_acceptable_level`（保性能下限档）、`bw_demand_GBps`（拐点处所需带宽）。

### Phase 3 — 并发差异化分配（验证假设，1–2 天）
- 固定并发数 N（如 4、8），混合负载（饥饿 + 轻载搭配），每策略 ≥3 次重复：
  - **A 基线**：全部塞同一组等分（当前 `mpam-batch-replay-e2e.sh` 行为）
  - **B 等档独立组**：每 VM 独立组但同档
  - **C 需求比例分配**：按 Phase 2 的 `bw_demand` 加权分档，饥饿型给高档、轻载型给低档，总和受 node 带宽预算约束
  - **D 域感知**：把饥饿型 VM 分散到不同 NUMA node，各享满档（验证「四域独立 → 分散优于挤同 node」）
- 测 makespan / 吞吐 / 退化率分布 / 公平性。
- **需改造**：现有 `mpam-batch-replay` 所有 VM 共用一个 `--group`；策略 C/D 要**每 VM 独立 group + 独立 bandwidth + 独立 node 绑定**（`--numa-node` / `--cpu-list` 已支持，group 名需按 case 传入，bandwidth 需按 case 传入）。

### Phase 4 — 结论（半天）
- 对比 A–D 的 makespan 与退化率，选最优策略。
- 产出「case 类别 → 推荐档位 / node」分配表 + 可落地启发式规则（作为调度器输入）。

---

## 5. 方法与工具细节

### 5.1 档位下发（复用现有脚本）
`mpam-replay-e2e.sh --group <名> --bandwidth <档>`：组不存在则建组并写 `MB:0=..;1=..;2=..;3=..`，写后**回读内核吸附值**并记录（`applied` 字段）。差异化分配（策略 C/D）需扩展为每 VM 独立组、独立档。

### 5.2 带宽真值采集（无 MB_MON 的唯一手段）
```
perf stat -a -I 1000 \
  -e hisi_sccl<node>_ddrc<M>/flux_rd/,hisi_sccl<node>_ddrc<M>/flux_wr/ ... sleep <T>
```
- 需 root（`perf_event_paranoid=2`）。
- 每 node 的全部 ddrc 域求和 × scale(=32 B/count) ÷ 时间 = 该 node GB/s。
- 只积分 case 运行窗口（用 `t_launch / t_vmm / t_case / t_end` 打点，参考 `logs/vm-bw/vm-bw-summary.json`）。

### 5.3 sccl → node 映射反查（每机重做）
```
for s in $(ls /sys/bus/event_source/devices/ | grep -o 'hisi_sccl[0-9]*' | sort -u); do
  cpu=$(cat /sys/bus/event_source/devices/${s}_ddrc0_0/cpumask)
  node=$(grep -l "(^|,)$cpu(,|$)" /sys/devices/system/node/node*/cpulist ...)
done
```
标定机结果：node0=sccl3, node1=sccl1, node2=sccl11, node3=sccl9。

---

## 6. 假设

- **H1**：case 敏感度呈双峰——饥饿型（编译 / 大内存 go、rust、ts）在低档显著退化，轻载型全程平坦。
- **H2**：并发下按敏感度加权分配带宽，聚合完成时间 < 等分分配。
- **H3**：MB 四域独立 → 饥饿型 VM 分散到不同 node 各享满档，优于挤同一 node。

---

## 7. 必须避开的陷阱

1. **0 档严禁**（本机曾致挂死重启）——脚本硬校验 ≥1，安全下限取 1。
2. **档位集合不可硬编码**——标定机 gran=9 使 1–8 全吸附到 9；实际环境 gran 更细，1–9 是独立刻度。一律由目标环境 `bandwidth_gran` 推导。
3. **无 MB_MON**——带宽真值只能靠 ddrc uncore PMU，需 root，区分 rd/wr，按 node 归组。
4. **单 case 采样偏差**——现有结论只基于 1 个轻载 case，必须扩到分层样本。
5. **高档位不 binding**——轻载 case 在档 ≥18 无差异，别浪费重复；重复次数向低区倾斜。
6. **噪声**——共享机（标定机当前 load≈4.5），每配置 ≥3 次取中位数，记录前后基线漂移；敏感度测量时 VM 独占 node。
7. **setup.json 路径**——换机器要重转换，或 replay 时 `--kernel/--firecracker` 覆盖（新机器已踩过：case 从旧机拷来、setup.json 指向不存在的二进制路径 → VM 起不来、console/firecracker 日志全空）。

---

## 8. 工作量估算

| Phase | 内容 | 估算 |
|---|---|---|
| 0 | 平台标定（两机） | 各半天 |
| 1 | ~15 case × ~12 档 × 低区3/中高区1–2 次 | 最重，按均值 ~200s/case ≈ 25–30h（可挑短 case 或减档） |
| 2 | 分类建模 | 半天 |
| 3 | 并发 4 策略 × N∈{4,8} × 3 次 | 1–2 天 |
| 4 | 结论 | 半天 |

---

## 9. 产出物清单

- `platform-calib.md`（标定机 + 实际环境各一份）
- `case-sensitivity.csv`（Phase 1 主数据）
- `case-classes.csv`（Phase 2 分类 + min_acceptable_level + bw_demand）
- `concurrent-strategies.csv`（Phase 3 A–D 对比）
- `CONCLUSION.md`（Phase 4 分配表 + 启发式规则）
