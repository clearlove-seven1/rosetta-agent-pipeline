# PROJECT-ROADMAP — 蛋白结构与突变计算工作台 总进度

> 建立日期:2026-11-21(初始基线快照,基于当日实测);更新:2026-11-21 12:50(阶段 3 Rosetta 侧完成)
> 总目标:Rosetta 负责结构准备与突变初筛,GROMACS 负责选定体系的分子动力学模拟与分析;
> 两者各自独立运行,由统一任务系统、数据规范和报告连接。后续可扩展对接等新执行器。
> 本文档按总规划在每阶段结束时更新。

## 一、服务全景(2026-11-21 12:15 实测)

| 端口 | 进程 | 归属 | 启动 | 状态 |
|---|---|---|---|---|
| 7779 | (11-21 12:3x 再次重启) | rosetta1 `rosetta_server.py` | 11-21 12:3x | **契约 v0.1 代码已上线**(阶段 3;生产验收 PASS 见阶段 1) |
| 7782 | (12:3x 随之恢复) | rosetta1 MCP streamable-http | 11-21 12:3x | 已恢复并加载契约代码;Trae stdio 实例随其重连恢复 |
| 7778 | pid 3147302 | gromacs `gromacs_server.py` | 11-21 11:31 | 自报 r4;**用户在另一会话管辖推进(r3→r5 备份),本工作台不介入** |
| 7795 | pid 581741 | `server.py`(cwd 未核实,与旧 7779 同批启动) | 11-18 | 运行中(本次未动) |
| 7860 | pid 51924 | ros-gro `workbench.agent`(智能体网页) | 11-18 | 运行中 |
| 8080/8081 | pid 3018475/51916 | `工作台/总部ui` hq_server.py / api.py | — | 运行中 |
| 8083 | pid 3023824 | docking `agent_server.py` | — | 运行中 |
| 7796 | pid 84707 | bioplot `plot_agent_server.py` | 11-18 | 运行中 |
| 8082 / 7790 / 7781 | — | wetlab / KAG×2 | — | 与本工作台无关 |

GPU 空闲,无 mdrun/gmx 计算进程。

## 二、四个相关仓库状态(2026-11-21 实测)

### 1. ~/projects/rosetta1(Rosetta Agent)
- HEAD 5ca61e9 → **2026-11-21 已限路径提交修复线**(核心 py/task_runtime/测试/交付文档/本 ROADMAP;见 git log;
  static/、plot_agent/、rosetta_manuals 增量、.bak-*、日志等并行会话/产物文件未纳入提交,由 tar 快照覆盖)。
- 修复验收:guardrails **26/26** + eval_p0 **76/76** + eval_review_followup **14/14**;编号地面真值 **7/7 MATCH**;
  真实流水线 1MC9 PASS + 可控失败 PASS。
- **生产切换与验收(2026-11-21 12:10–12:20,全部实测 PASS)**:
  - 切换前自查:三套测试重跑全绿;0 个 runner 进程、GPU 空闲(`/api/tasks` 的 live:true 为内存残留标志)。
  - 7779 重启:`/api/tasks` 新格式含 status/status_note;`/api/host/status` 200;错误位点 `Z_999` 提交 →
    **HTTP 400 + 任务目录 task_status.json=failed**(预检前移生效);重启后持久状态查询
    (1MC9_verify=completed、FAILSIM=failed)✅(CHANGES §六 C-1/C-3 等价完成)。
  - 7782 恢复:MCP 冒烟 PASS——19 工具、原子工具 schema 含 task_id 无 ctx、
    `set_workspace("../../x")` 返回【执行报错】(设计即文本报错)、`list_tasks` 实调输出 `status=completed/failed` 持久状态。
  - **octop 客户端需重连冒烟**(7782 已恢复,重连后工具列表/绑定按新会话语义)。
- 备份链:`.bak-codexfix-20261120`、`.bak-review-20261121`、tar 快照(见 §五)。

### 2. ~/projects/gromacs(GROMACS Agent,LLM 驱动)
- git 仅基线提交 66a40d0,大量未提交;`.bak-r3/.bak-r4` 双备份。
- codex 今日完成第 3/4 轮修复(检索/索引安全/错误分级/任务状态持久化/安全/科学表述),109 unittest 全过;
  r3@10:51 → r4@11:31 上线。快照/回退:`~/backups/gromacs_pre_r3_*.tar.gz`、`gromacs_rollback_r3_code_*.tar.gz`。
- 遗留(其文档自述):真实 MD/FEP 回归、UI 回归、生产索引回填、无认证/TLS、许可证待法务。
- **该仓库由另一会话管辖,本工作台阶段内不触碰、不代做提交。**

### 3. ~/projects/ros-gro(Protein Design Workbench 主仓库)
- HEAD c628c33;未提交仅 4 文件 8 行(另一会话 mmpbsa 小改)。
- 已有实战:2HBB MD 全 succeeded、MMPBSA 10/10、S669 评测闭环(r=+0.34);挂账 5 项见其 PROGRESS.md。

### 4. 其他执行器与入口
- docking(8083)、总部ui(8080/8081 门户)、octop(MCP 客户端)、bioplot(7796)运行中。
- `~/projects/rosetta`(单数)为旧项目存档。

## 三、七阶段对照(总规划)

| 阶段 | 状态 | 依据 / 缺口 |
|---|---|---|
| 0 固化版本 | **基本完成** | 备份链齐;修复线已 git 提交;rosetta1+ros-gro 代码 tar 快照(见 §五);gromacs 归另一会话自有快照纪律 |
| 1 Rosetta 上线前验收 | **已完成(2026-11-21)** | 代码级验收(地面真值 7/7、流水线 1MC9、fail-sim)+ 生产端口验收(§二.1)全 PASS;剩 octop 重连冒烟由用户侧确认 |
| 2 GROMACS 独立体检 | 大部分完成(codex) | 剩真实 MD/FEP 回归、UI 回归、索引回填、认证;⚠️ 与 ros-gro 确定性执行器的选型已定方向(§四.3) |
| 3 统一任务与数据规范 | **Rosetta 侧完成(2026-11-21)** | `TASK-CONTRACT-v0.1.md` + `task_runtime.contract_check`(可执行拒绝语义)+ partial 状态(三处一致)+ manifest v0.1 字段(sha256/计数/版本);真实 1MC9 流水线端到端 ok=True;**下游消费侧(含 GROMACS 字段对齐)待其 r 轮收敛后对齐** |
| 4 第一条跨工具流程 | 未开始 | ros-gro v0.5-03 已有小参照(2HBB);rosetta1 侧缺突变体结构导出(生产 dump_pdbs=false) |
| 5 统一工作台 | 未开始 | 总部ui 与 7860 网页的入口归属见 §四.4 |
| 6 科学评估与扩展 | 未系统化 | ros-gro S669 闭环 + MD 副本噪声带方法学可作基础 |

## 四、决策记录(2026-11-21,用户委托"按推荐执行")

1. ✅ **生产切换已执行**:7779 重启加载新代码 + 7782 恢复;验收 PASS(当时无运行任务,GPU 空闲)。
2. ✅ **rosetta1 修复线已限路径 git 提交**(避开并行会话文件与产物目录;完整工作树由 tar 快照固化)。
3. **GROMACS 路线选型(方向已定)**:统一任务层(阶段 3/4)以 **ros-gro workbench.gromacs 确定性执行器为主对接路径**
   (manifest/断点恢复/固定种子/CLI 可编排,已有真实 MD+MMPBSA 实战);**gromacs agent(LLM 驱动,7778)保留为独立执行器**,
   经其 HTTP API 作为独立入口接入,不并入主流程契约。阶段 3 定契约时落实字段映射。
4. **总部ui 定位**:继续作为用户侧聚合门户(候选统一入口),阶段 5 设计统一工作台时正式评审;本阶段不改其代码。
5. **ros-gro GitHub 远程:暂缓**(涉及公开发布/脱敏审查,须用户最终拍板;挂账保留于其 PROGRESS.md)。

## 五、快照与回滚索引

- 代码快照(2026-11-21):`~/backups/rosetta1_snapshot_20261121_code.tar.gz`(含 .git,排除 output/chroma/manuals/日志/.env)、
  `~/backups/ros_gro_snapshot_20261121_code.tar.gz`(排除 .git/runs/.env)。
- rosetta1 回滚:git revert/checkout + `.bak-codexfix-20261120`(恢复 11-20 前行为,见 CHANGES §八);
  服务回滚=回滚文件后重启 7779/7782。
- gromacs 回滚:其自带 `~/backups/gromacs_pre_r3_*` 与 `.bak-r4-*`(由管辖会话维护)。

## 六、变更记录

- 2026-11-21 上午:初始基线快照(实测;四仓库/服务/端口/任务/备份盘点)。
- 2026-11-21 12:15:阶段 1 生产验收完成并关账;rosetta1 修复线限路径提交(16d4b8e);决策记录 §四 落档;快照索引 §五。
- 2026-11-21 12:50:**阶段 3 Rosetta 侧完成**——契约 v0.1(文档+校验器)、partial 状态、manifest
  v0.1 字段落地上线(测试 26+76+14+20 全绿;真实 1MC9 流水线 contract_check ok=True;
  rosetta_version/sha256/成功失败计数如实落盘)。用户明确:gromacs 由其另一会话推进,本工作台不介入。
- 下一步:阶段 4 前置(Rosetta 侧突变体结构导出)+ GROMACS 侧契约对齐(待其 r 轮收敛,由用户触发)。
