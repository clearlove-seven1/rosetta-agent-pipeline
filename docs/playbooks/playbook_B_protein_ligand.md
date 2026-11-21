# Playbook B — 蛋白 + 小分子配体

> 引用方式:`suggest_plan` 工具输出本文件指引;`search_rosetta_docs` 检索时计划手册优先命中。
> 铁律同 Playbook A:计划是建议;科学参数由工具默认值锁定;缺条件必须停下问,不猜测。

## 适用条件

- 蛋白(≥1 条链)+ 小分子配体(独立 `.mol2`/`.pdb` 文件,或复合物 PDB 中的 HETATM)。
- 目标:配体存在下的突变扫描与界面/口袋分析。**本系统不做分子对接**——对接请人工接力
  用户自己的 docking 工具,再把对接复合物拿回来做扫描。

## 前置条件(缺 → 停下问)

- [ ] 蛋白结构 PDB。
- [ ] 配体文件:7779 支持 `.pdb`/`.mol2` 上传;未上传时系统会用 PyMOL 自动分离 HETATM
      (自动加氢转 MOL2)。配体残基名必须唯一可识别(3 字母代号)。
- [ ] **质子化状态由输入文件决定**;pH/质子化选择是用户的决策,LLM 只提示、不替用户决定。
- [ ] 配体残基名与标准残基/水冲突 → 停下问。

## 步骤与默认参数

| 步骤 | 工具 | 默认参数 | 说明 |
|---|---|---|---|
| 1 体系判定 | `suggest_plan` | — | 输出检测到的 HETATM 残基名与链/残基统计 |
| 2 配体参数化 | `parameterize_ligand` | 自动侦测 3 字母代号;PDB→PyMOL 加氢→MOL2 | 产出 `parameterize_ligand/*.params`;纯蛋白任务如实跳过(不报错) |
| 3 复合物弛豫 | `run_cartesian_relax` | nstruct=2 | 参数化成功后自动拼接配体进入弛豫 |
| 4 突变扫描 | `run_saturation_mutagenesis` | iterations=3;自动携带 `-extra_res_fa <params>` | 位点建议:口袋/界面残基(可用 `analyze_interface` 辅助选点) |
| 5 解读 | `ddg_results.txt` + `analyze_interface` | ΔΔG(REU);界面 dG_separated 等 | 配体存在时 ΔΔG 已含配体环境效应,口径仍是稳定性预测 |

## 停止条件(必须停下问用户)

- 配体文件缺失且结构中无非水 HETATM → 走 Playbook A;
- params 生成失败 → 读 `parameterize_ligand` 日志定位(电荷/键级/原子名问题),停下问用户;
- 配体原子名/元素解析异常 → 不猜测,请用户提供干净的 mol2;
- 用户要求改力场/权重 → 同 Playbook A 铁律。

## 产物与注意

- 同 Playbook A;`manifest.ddg_params.extra_res_fa=true` 标记配体参数已携带;
- Rosetta 配体参数(params)**不可假定**适用于 GROMACS 拓扑——下游 MD 需用户自行参数化
  (GAFF/CGenFF 等),来源记录在其任务 manifest;
- 大配体/多配体体系先由用户确认算力预算再开跑。

## 实测记录(2026-11-21,RCSB 3HTB:单链 HIV 蛋白酶 1-163 + 配体 JZ4)

- **全流程 PASS**:判定(B)→ parameterize_ligand(自动侦测代号 **JZ**,生成 .params)→ 复合物弛豫
  → A_25 扫描 19 突变全部 exit 0(自动携带 `-extra_res_fa`)→ 结构导出 19 目录 → contract ok。
  科学合理性:活性位点 Y25 全部替换为正 ΔΔG(去稳定),G/P/K 最狠(+13.4/+11.4/+10.6)——符合催化位点预期。
- **实测踩坑(已修/已记)**:
  1. `parameterize_ligand` 曾裸调 `"python"`(不在服务环境 PATH)→ `[Errno 2] No such file or
     directory` → 已改为 `sys.executable`(2026-11-21 修复);
  2. 复合物中的离子/共存小分子(PO4、BME)会被分类器**如实列出**但**不会**自动参数化——
     用户需明确哪些要进计算,LLM 不得替用户决定;
  3. altLoc 备用构象(本例 64 行)在判定阶段即如实剔除并计入 anomalies(与弛豫清洗一致);
  4. 配体文件只需 JZ4 的 HETATM 行(`ligand.pdb`),PyMOL 加氢转 MOL2 → params 全自动;
  5. `summarize_results` 曾把 structures_manifest.json 误计为结构目录(20 vs 19)→ 已修(isdir 过滤)。
- 重现:`python real_ligand_test_20261121.py`(任务 task_20261121_1817520d8c_3HTB_ligtest 留档)。
