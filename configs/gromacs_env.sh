#!/usr/bin/env bash
# Protein Design Workbench — GROMACS 2026.1 运行环境
#
# 背景：/opt/software/gromacs-2026.1 是 CUDA 构建（GPU support: CUDA + cuFFT），
# 动态链接 libcufft.so.11（CUDA 12.x 的 cuFFT soname）；系统现只装了 CUDA 13.0
# （仅带 libcufft.so.12），直接运行 gmx 会报
#   "libcufft.so.11: cannot open shared object file"。
#
# 方案：直接复用项目 env `rosetta-agent`（本项目的唯一运行环境）——
# 其 torch 2.11.0+cu128 依赖链自带 nvidia-cufft-cu12==11.3.3.83，即 libcufft.so.11。
# 本脚本注入该库路径，不修改系统环境、不借用其他业务的 env。
#
# ⚠️ 依赖风险：若将来升级 rosetta-agent 里的 torch，nvidia-cufft-cu12 可能变版
#   （so.11 消失）导致 gmx 启动失败——届时重装 nvidia-cufft-cu12==11.3.3.83 即可。
#   本脚本在库缺失时会明确警告，不会静默失败。
#
# 用法：  source configs/gromacs_env.sh
# 验证：  gmx --version        # 应看到 GPU support: CUDA 且无加载错误
# 记录：  正式任务 manifest 应记录 GMX_ROOT 与本脚本路径、cuFFT 包版本。

GMX_ROOT="${GMX_ROOT:-/opt/software/gromacs-2026.1}"
WB_CUFFT_LIB="$HOME/.conda/envs/rosetta-agent/lib/python3.10/site-packages/nvidia/cufft/lib"

source "$GMX_ROOT/bin/GMXRC.bash"

if [ -d "$WB_CUFFT_LIB" ] && [ -e "$WB_CUFFT_LIB/libcufft.so.11" ]; then
    export LD_LIBRARY_PATH="$WB_CUFFT_LIB${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
else
    echo "[gromacs_env.sh] 警告: 未找到 cuFFT 11 兼容层 ($WB_CUFFT_LIB)" >&2
    echo "[gromacs_env.sh] gmx 可能因缺 libcufft.so.11 无法启动。请先执行:" >&2
    echo "[gromacs_env.sh]   ~/.conda/envs/rosetta-agent/bin/pip install nvidia-cufft-cu12==11.3.3.83" >&2
fi
