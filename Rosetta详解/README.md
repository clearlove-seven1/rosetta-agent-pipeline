# Rosetta 详解

本目录是 [Rosetta 软件套件](../README.md) 的完整深度介绍，重点回答：

1. **Rosetta 是什么**：从历史到现状的完整生态
2. **包含哪些工具**：所有主要工具及其用途
3. **本项目用了哪些**：3 个核心工具的深度拆解

## 文档索引

| 章节 | 内容 | 适用场景 |
|---|---|---|
| [01-Rosetta总览与生态](./01-Rosetta总览与生态.md) | 历史 / Baker Lab / 衍生产品全景 | 第一次接触 Rosetta |
| [02-核心架构与能量函数](./02-核心架构与能量函数.md) | C++ 内核 / 力场 / 残基类型系统 | 想理解 Rosetta 底层 |
| [03-蛋白设计工具集](./03-蛋白设计工具集.md) | Design / FixBB / FastDesign / LayerDesign 等 | 想做蛋白设计 |
| [04-结构预测与建模](./04-结构预测与建模.md) | Abinitio / Robetta / RosettaCM / 抗体 | 想做结构预测 |
| [05-对接相互作用与配体](./05-对接相互作用与配体.md) | 蛋白-蛋白 / 蛋白-配体 / 蛋白-肽对接 | 想做对接 |
| [06-核酸糖类膜蛋白](./06-核酸糖类膜蛋白.md) | RNA / DNA / 糖 / 膜蛋白 | 跨领域应用 |
| [07-本项目使用工具详解](./07-本项目使用工具详解.md) | relax / cartesian_ddg / molfile_to_params.py | 本项目相关 |

## 阅读建议

- **第一次接触**：1 → 2 → 7（总览 → 内核 → 本项目相关）
- **想做蛋白设计**：1 → 2 → 3
- **想做结构预测**：1 → 2 → 4
- **想理解 ΔΔG 预测**：1 → 2 → 3 → 7
- **跨领域应用**：6

## 一句话定义

**Rosetta = 由华盛顿大学 David Baker 实验室主导开发的开源分子建模软件套件，提供从原子级能量评估到蛋白设计、结构预测、对接的完整工具链，是当前蛋白设计领域事实标准的科学计算平台。**

## 项目链接

- 官方网站：https://www.rosettacommons.org/
- 论文中心：https://www.rosettacommons.org/docs/latest/publications
- GitHub（部分开源）：https://github.com/RosettaCommons
- 用户手册：https://www.rosettacommons.org/docs/latest/
- 官方论坛：https://www.rosettacommons.org/forum
- 学术协议申请：https://www.rosettacommons.org/software/license