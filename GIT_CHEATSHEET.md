# Git 速查手册

> 本项目（rosetta-agent）的 Git 常用命令汇总。所有命令假设在项目根目录下执行。

---

## 0. 概念速懂

Git 数据流向（三层 + 远程）：

```
工作区（你编辑的文件）
   │  git add
   ▼
暂存区（待提交的清单）
   │  git commit
   ▼
本地仓库（.git 里的历史记录）
   │  git push
   ▼
远程仓库（GitHub 上的备份，origin 是默认昵称）
```

- **origin**：远程仓库的昵称，对应 GitHub 真实地址 `https://github.com/clearlove-seven1/rosetta-agent-pipeline.git`
- **分支**：可理解为"草稿纸"，从 main 复印一份出来改，不影响 main

---

## 1. 初始化与配置（一次性）

```bash
# 新项目初始化
git init

# 配置身份（必做，否则无法 commit）
git config user.name "你的名字"
git config user.email "你的邮箱"

# 配置远程地址（已有 GitHub 仓库）
git remote add origin https://github.com/clearlove-seven1/rosetta-agent-pipeline.git

# 查看远程地址
git remote -v
```

---

## 2. 查看状态

```bash
git status              # 看哪些文件改了/新增/未跟踪
git diff                # 看具体改动内容（未暂存的）
git diff --staged       # 看已暂存但未提交的改动
git log                 # 看提交历史
git log --oneline -10   # 简洁模式，看最近 10 条
git branch              # 查看所有本地分支（带 * 是当前）
git branch -a           # 包括远程分支
```

---

## 3. 日常三板斧

### 3.1 添加到暂存区（git add）

```bash
git add 文件名              # 添加单个文件
git add file1 file2 file3  # 添加多个指定文件
git add *.py               # 通配符（所有 .py）
git add 目录名/             # 添加整个目录
git add .                  # 添加当前目录所有改动（全加）
```

### 3.2 提交到本地（git commit）

```bash
git commit -m "说明改了啥"   # 提交暂存区的内容

# 提交规范（建议）
# 类型: 简短描述
# 常见类型：
#   feat     新功能
#   fix      修 bug
#   docs     文档
#   refactor 重构
#   test     测试
#   chore    杂项（更新 ignore 等）
# 示例：git commit -m "feat: 加入ESM-2 disorder过滤"
```

### 3.3 推送到远程（git push）

```bash
git push origin main              # 推 main 分支
git push origin feat/xxx-branch   # 推其他分支

# 简化写法（当前分支）
git push
git push -u origin 分支名        # 第一次推送时设置上游关联
```

---

## 4. 分支操作

### 4.1 创建分支

```bash
git checkout -b 新分支名        # 方法 A：新建并切换（最常用）
git branch 新分支名            # 方法 B：只创建不切换
git switch -c 新分支名         # 方法 C：新语法（Git 2.23+）
```

### 4.2 切换分支

```bash
git checkout 分支名
git switch 分支名              # 新语法
```

### 4.3 删除分支

```bash
git branch -d 分支名           # 安全删除（已合并才能删）
git branch -D 分支名           # 强制删除（不管有没有合并，慎用）
```

### 4.4 分支命名规范

```bash
# 格式：<类型>/<功能名>
<类型>/<功能名>

# 类型
feat/xxxx      # 新功能
fix/xxxx       # 修 bug
docs/xxxx      # 文档
refactor/xxxx  # 重构
test/xxxx      # 测试

# 命名三原则
# 1. 全英文小写，用 - 连单词（不要驼峰、不要下划线、不要空格）
# 2. 简短能看懂（3-5 个词）
# 3. 避免重名（同名需先删旧的）

# 示例
feat/disorder-filter
fix/saturation-mutagenesis-error
docs/readme-update
```

### 4.5 完整 PR 流程

```bash
# 1. 从 main 切新分支
git checkout -b feat/xxx

# 2. 改代码...

# 3. 暂存并提交
git add .
git commit -m "feat: xxx"

# 4. 推送到远程
git push origin feat/xxx

# 5. 去 GitHub 网页提 Pull Request
# 6. 合并后清理
git checkout main
git pull origin main
git branch -d feat/xxx       # 删本地分支
```

---

## 5. 回退操作（救命表）

| 后悔程度 | 命令 | 后果 |
|---|---|---|
| 改坏了想重来（未 add） | `git restore 文件名` | 工作区干净，文件回到上次提交状态 |
| 改坏了想全部重来 | `git restore .` | 所有未暂存的改动全丢 |
| add 后反悔 | `git restore --staged 文件名` | 改动从暂存区回到工作区 |
| add 后想丢弃改动 | `git restore --staged 文件名 && git restore 文件名` | 完全丢弃 |
| commit 后反悔（未 push） | `git reset --soft HEAD~1` | commit 撤销，改动回到暂存区 |
| commit 后想全丢（未 push） | `git reset --hard HEAD~1` | commit + 改动全丢（慎用） |
| 已 push 想撤销 | `git revert HEAD && git push` | 生成反向 commit，安全 |

详细分情况：

### 5.1 修改了还没 add
```bash
git checkout -- 文件名      # 单文件（老写法）
git restore 文件名          # 新写法
git restore .               # 全部回退
```

### 5.2 已经 add 但还没 commit
```bash
# 把文件从暂存区撤回到工作区（保留改动）
git restore --staged 文件名

# 撤回到工作区并丢弃改动
git restore --staged 文件名
git restore 文件名
```

### 5.3 已经 commit 但没 push
```bash
# 软回退：撤销 commit，保留改动在工作区
git reset --soft HEAD~1

# 硬回退：撤销 commit，改动全丢
git reset --hard HEAD~1
```

### 5.4 已经 push 到远程
```bash
# 推荐：用 revert 生成反向 commit
git revert HEAD
git push origin main

# 不推荐：reset + force push（改写历史，团队项目会出事）
git reset --hard HEAD~1
git push --force origin main
```

---

## 6. 常用辅助命令

```bash
git stash                 # 临时保存工作区改动（不提交）
git stash pop             # 恢复刚才保存的改动
git tag v1.0.0            # 打标签（标记版本）
git fetch origin          # 拉取远程信息（不合并）
git pull origin main      # 拉取并合并远程 main
git clone <url>           # 克隆远程仓库到本地
```

---

## 7. .gitignore 常用规则

```gitignore
# 文件
文件名
*.后缀

# 目录
目录名/
目录名/**

# 例：本项目忽略的学习目录
learn_*/
learn_*/**
*_learning
*_learning/**

# 例：本项目忽略的大文件
*.mol2
*.pdb
output/

# 例：环境与缓存
.env
.env.*
__pycache__/
.gradle/
```

---

## 8. 本项目当前状态（参考）

```
remote:  https://github.com/clearlove-seven1/rosetta-agent-pipeline.git
branch:  main（唯一主分支）
.gitignore 已配置：
  - learn_*/ 与 *_learning（含子目录）
  - *.mol2 / *.pdb / output/ / __pycache__/ / .gradio/
  - .env / .env.*（保留 .env.example）
```