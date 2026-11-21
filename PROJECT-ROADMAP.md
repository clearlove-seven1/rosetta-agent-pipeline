# PROJECT-ROADMAP — 蛋白结构与突变计算工作台 总进度

> 建立日期:2026-11-21(初始基线快照,基于当日实测);更新:2026-11-21(阶段 3 完成 + **目标调整:不做结合**)
> 总目标(2026-11-21 调整,用户拍板):把 Rosetta 侧与 GROMACS 侧做成**各自独立、各自可靠的科研计算工具**;
> **不做系统级结合、不做统一智能体/工作台**。需要联动时人工接力:Rosetta 导出候选突变体结构 → 用户自行投入其 GROMACS 会话。
> 任务契约(contract_check)保留,作为 Rosetta 侧可追溯性与未来任何消费方(含人工)的通用接口。

## 一、服务全景(2026-11-21 12:15 实测)

| 端口 | 进程 | 归属 | 启动 | 状态 |
|---|---|---|---|---|
| 7779 | (11-21 15:xx 再次重启) | rosetta1 `rosetta_server.py` | 11-21 15:xx | **契约+导出+计划层+能力包①-⑥已上线**(含 SqliteSaver 对话持久化,跨重启续聊实证 PASS) |
| 7782 | (15:xx 随之恢复) | rosetta1 MCP streamable-http | 11-21 15:xx | 21 工具(新增 suggest_plan/summarize_results);Trae stdio 实例随其重连恢复 |
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
| 4 跨工具流程 | **已裁剪(2026-11-21 用户拍板)** | 不做系统级结合;Rosetta 侧仅保留"突变体结构导出"作为独立能力,支撑人工接力 |
| 5 统一工作台 | **已裁剪** | 不做统一智能体/工作台;总部ui(8080)继续作为用户自用聚合门户,不改造 |
| 6 科学评估与扩展 | 未系统化(可选) | Rosetta 侧:ΔΔG 基准/重复性/耗时成本——按需再做;GROMACS 侧归另一会话 |

## 四、决策记录(2026-11-21,用户委托"按推荐执行")

1. ✅ **生产切换已执行**:7779 重启加载新代码 + 7782 恢复;验收 PASS(当时无运行任务,GPU 空闲)。
2. ✅ **rosetta1 修复线已限路径 git 提交**(避开并行会话文件与产物目录;完整工作树由 tar 快照固化)。
3. **GROMACS 路线选型(方向已定)**:统一任务层(阶段 3/4)以 **ros-gro workbench.gromacs 确定性执行器为主对接路径**
   (manifest/断点恢复/固定种子/CLI 可编排,已有真实 MD+MMPBSA 实战);**gromacs agent(LLM 驱动,7778)保留为独立执行器**,
   经其 HTTP API 作为独立入口接入,不并入主流程契约。阶段 3 定契约时落实字段映射。
4. **总部ui 定位**:继续作为用户侧聚合门户(候选统一入口),阶段 5 设计统一工作台时正式评审;本阶段不改其代码。
5. **ros-gro GitHub 远程:不上传(2026-11-21 用户拍板,终局决定)**——只维护 rosetta-agent-pipeline
   一个仓库;清密钥历史一事随之取消,后续不再提议。
6. **(2026-11-21 用户拍板,覆盖第 3/4 条中"整合"部分)不做 Rosetta×GROMACS 系统级结合、不做统一智能体/工作台**;
   两工具独立演进,联动=人工接力——Rosetta 侧只需补"突变体结构导出"这一独立能力;
   contract_check 保留为 Rosetta 侧可追溯接口(未来任何消费方通用,含人工接力场景)。

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
- 2026-11-21 下午:**目标调整(用户拍板)**——不做 Rosetta×GROMACS 系统级结合、不做统一智能体/工作台;
  阶段 4(跨工具流程)与阶段 5(统一工作台)裁剪;剩余工作收敛为 Rosetta 侧独立能力清单(见 §七)。
- 2026-11-21 13:5x:**§七.1 突变体结构导出完成**——export_structures/export_sites 上线;
  stub 端到端 23/23 + 回归 159 项 + 真实 1MC9 A_67 全 19 突变导出与落点独立复核 19/19 PASS。
- 2026-11-21 14:xx:**知识库计划层完成(§七.5)**——playbooks+体系判定+suggest_plan+检索分层上线,
  真实问计划实测 PASS;"输入新蛋白(和配体)→ 问出计划与参数"能力就位,参数默认值仍由工具护栏锁定。
- 2026-11-21 15:xx:**能力包①-⑥完成(§七.6)**——提示词/RAG 评测(24/24)/start_all+运维手册/
  结果报告工具/SqliteSaver 持久化(跨重启实证)/playbook D;start_all 首跑实测抓到 7779 启动崩溃
  (NameError)并当场修复,评测新增"启动链路导入守卫"防复发;ros-gro 不上传(终局决定)。
- 2026-11-21:octop 冒烟 PASS(用户侧截图:21 工具可见、suggest_plan/summarize_results 实调成功)。
  **§七 全部完成,无挂账**;后续输入=用户使用反馈。
- 2026-11-21 23:xx:**TMol GPU 引擎接入工作台(§七.7,用户点名)**——/opt/software/tmol
  (atomworks 生态,GPU)=Rosetta 能量函数 PyTorch 重实现;修复其部署(torch 2.8 ABI 不匹配→
  用户目录 ~/.tmol_torch 影子加载 torch 2.11+cu130);GPU 原生突变扫描全链路打通
  (beta_nov2016_cart,mutate_residue+Dunbrack,单位点约 20-40s);run_tmol_scan 工具(MCP 22 工具/
  7779 GENERAL_TOOLS)、任务级 engine 字段(rosetta/tmol)+前端下拉+三处提示词引擎路由;
  summarize_results 双口径/TMol GPU 报告;端到端 PASS(1MC9 A_25 SER→LYS -0.753)。
  诚实边界:tmol 口径数值与 ref2015_cart 不可比;tmol 引擎不产出突变体结构;固定骨架单点近似
  (拥挤位点 PRO 突变 +314 为已知现象)。测试 eval_tmol 16/16,9 套全绿。依赖:jieba/
  langgraph-checkpoint-sqlite/torch2.11-cu130(~/.tmol_torch)。
- 2026-11-21 19:xx:**借鉴 /opt/software/docking 生态的 docking agent 方式,上线成本/耗时可见化**——
  ①LLM token 逐条记录 output/usage_stats.jsonl(回调式,失败不影响对话);②每轮对话收尾自动上屏
  〔token 用量〕本轮/任务累计(来自消息自带 usage_metadata);③suggest_plan 增【预计耗时】
  (A/B/C 三类实测粗估,大体系 ×2-3 提醒);④summarize_results 增【实际耗时】(创建→最近更新)。
  实测:一次对话 3 条 jsonl(glm-5.3-flash)、55.5 分钟报告;修复 json 未导入致记录被吞(评测+实测双抓)。
  测试 eval_usage 12/12,8 套全绿。

## 七、目标调整后的剩余工作清单(Rosetta 侧,按价值排序)

1. ✅ **突变体结构导出(2026-11-21 完成,真实验收 PASS)**:
   - `run_saturation_mutagenesis` 新增 `export_structures`(默认 False,行为与旧版逐字节一致)/
     `export_sites`(扫描位点子集,非法输入明确报错)两参数;MCP schema 已暴露(13:4x 上线);
   - 归集至 `saturation_mutagenesis/mutant_structures/<wt>_<site>_<mut>/` + **内容级落点核对**
     + `structures_manifest.json` + manifest/artifacts 登记;下载走既有 files/download 通道(已验证可见);
   - 诚实修正:cartesian_ddg 实测**本就落盘** MUT 结构(此前"不产出"系 11-16 旧行为误判),开关只做归集/核对/登记;
   - 验证:stub 端到端 **23/23**(含真实 run_ddg.py 执行)+ 全套回归 **159 项** +
     真实 1MC9 A_67(**19/19 落点独立复核全对**,contract_check ok=True,task_20261121_1347455b7b)。
2. ✅ **octop 重连冒烟(2026-11-21 完成,用户截图验收)**:octop(TQC-worklab :8090)的
   「Rosetta 突变扫描」agent 重连 7782 后可见 21 工具(含 suggest_plan/summarize_results),
   并成功实调两者(分析 3HTB 配体任务 + 生成结果报告);agent 口头计数 19/18 有小出入,
   服务端实测恒为 21(18 原子+3 管理),不影响使用。
3. 可选:Rosetta 侧科学评估(阶段 6 缩水版)——ΔΔG 基准复验/重复性/耗时成本,写简历或评估可信度时再做。
4. 可选:报告/下载易用性小改进(按使用痛点来,不预先设计)。
5. ✅ **知识库计划层(2026-11-21 完成)**:三份体系 playbook(`docs/playbooks/`,入库)+ `classify_system`
   体系判定(单链/蛋白+配体/复合物)+ `suggest_plan` 工具(MCP 20 工具、7779 对话 GENERAL_TOOLS 均挂载)
   + BM25 计划手册优先检索;**真实 1MC9 问计划实测 PASS**(判定/步骤/缺项/停止条件全输出);
   测试 16/16+全套 175 项。配体参数化实测文档待用户提供真实配体文件后补写。
6. ✅ **能力包①-⑥(2026-11-21 完成)**:① REPLY_SYSTEM 提示词打磨(必引 playbook/停止条件/计划纪律);
   ② RAG 评测集 24 题(**命中率 24/24**,jieba 中文分词修复根因:默认空白切词致中文整句成 token);
   ③ `start_all.sh` 幂等拉起(实测抓到并修复 7779 启动崩溃)+ OPS-RUNBOOK 运维手册;
   ④ `summarize_results` 一键结构化报告(排序/口径/接力 MD 建议映射已导出结构);
   ⑤ `SqliteSaver` 检查点持久化(**跨重启续聊实证 PASS**:暗号 maple7 重启后答对);
   ⑥ playbook D 定点设计(fixbb 三条 limitation 如实入册)。测试 19/19,全套 7 套全绿。
   依赖新增:langgraph-checkpoint-sqlite、jieba(已入 rosetta-agent env);TASK-CONTRACT 迁至 docs/。

**已明确不做**:统一任务层、GROMACS 对接消费端、结构筛选层(为 MD 准备)、统一工作台、跨工具综合报告、合并智能体。
(现有 rosetta1 平台 7779/MCP 本就是独立智能体,保持现状继续服务,不属于"合并"范畴。)
