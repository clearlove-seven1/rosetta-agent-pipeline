# v0.5-04 MM/PBSA 集成与观察报告 — 2HBB 五体系（探索性工具输出）

> **run 类别：探索性工具输出（exploratory tool output），非科学结论**
> 日期：2026-11-14｜工具：gmx_MMPBSA 1.7.0（用户 hdac8 env，AmberTools 26.0）｜模式：stability（`-s`）
> 执行入口：`workbench/mmpbsa.py`（v0.5-04 新模块）
> **结果不含熵贡献、协议未经验证、3ns 轨迹未收敛；不是结合/折叠自由能；跨体系差异不得解读为突变效应。**

---

## 1. 集成内容

- 调用链：既有 MD run（`md.tpr`/`md.xtc`/`topol.top`）→ `gmx trjconv -pbc mol`（分子完整性）→ `gmx make_ndx`（单链按残基中点拆两段）→ `gmx_MMPBSA -s -cg <g1> <g2>`（GB：igb=8+PBRadii=4+saltcon 0.15；PB：istrng 0.15）→ 解析 `FINAL_RESULTS_*.dat` 与 `FINAL_ENERGY_*.csv`；
- 采样窗：第 101–300 帧、每 4 帧取 1（跳过前 1ns 平衡段），每体系 51 帧；
- 全部参数/命令/哈希/退出码入 manifest（runs/mmpbsa/，与 MD/Rosetta 同构）；
- **集成关键点（全部探针实证，已写入模块注释）**：稳定模式需 `-s` 旗标；`-cg` 取 **0 基**组号且两组必须不同（单链蛋白拆两个残基段合并定义溶质）；`PBRadii` 属 `&general`（放 `&gb` 报 Unknown variable）；`igb=8` 与 `PBRadii=4` 配对；`amber.sh` 不会冲掉 GROMACS cuFFT 兼容层（实测）。

## 2. 结果（观察性；仅供流程演示）

| 体系 | GB TOTAL (kJ/mol) | PB TOTAL (kJ/mol) | 帧数 |
|---|---|---|---|
| WT rep1 | −984.7 ± 70.4 | −966.2 ± 70.5 | 51 |
| WT rep2 | −981.3 ± 64.5 | −963.5 ± 64.7 | 51 |
| F31A | −981.6 ± 65.1 | −964.1 ± 64.2 | 51 |
| K12M | −966.4 ± 60.8 | −946.7 ± 60.5 | 51 |
| I18V | −991.9 ± 81.3 | −973.1 ± 81.0 | 51 |

（GB 与 PB 全部 10 个分析 run 均 `succeeded`，零失败；GB ~2 分钟/个，PB ~1 分钟/个。）

## 3. 观察记录（仅观察；无可解读信号）

1. **副本噪声**：WT 双副本 GB 差 3.4 kJ/mol（PB 差 2.7），单帧 SD 60–81 kJ/mol；
2. **跨体系差异与噪声同量级**：最大差（K12M vs WT ~17 kJ/mol）落在 SD 带内，且 K12M 的起点结构/拓扑与其他体系不同——**本批数据没有任何可解读的突变信号**；
3. GB 与 PB 的体系排序一致（PB 数值略低于 GB，属两种隐溶剂模型的正常系统差）；
4. 这不是"MM/PBSA 无效"的证据，只是"本配置（3ns×51 帧、无熵、未收敛）不产生可解读信号"——如实记录。

## 4. 若要科研化（正式方案的前置条件）

多副本收敛级轨迹（≥100 ns 量级）、熵贡献（nmode/quasi-harmonic）、配对起点设计、协议与实验对照验证——全部属正式模拟方案（待用户确认科学问题后设计）。

## 5. 复现

```bash
bash scripts/run_mmpbsa_v05.sh    # 5 体系 × (GB+PB) 批量
# 单个：
python -m workbench.mmpbsa --md-run runs/gromacs/<run_id> --calc gb
```
