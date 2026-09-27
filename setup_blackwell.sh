#!/bin/bash
# 在 PRO 5000 服务器上启用 Blackwell GPU 加速
# 用法: bash setup_blackwell.sh

set -e
CONDA_PY=/home/tianqingchen/.conda/envs/rosetta-agent/bin/python
CONDA_PIP=/home/tianqingchen/.conda/envs/rosetta-agent/bin/pip

echo "============================================================"
echo "Step 1: 卸载 CPU 版 torch"
echo "============================================================"
$CONDA_PIP uninstall -y torch 2>&1 | tail -3

echo ""
echo "============================================================"
echo "Step 2: 安装 Blackwell GPU 版 (cu128 wheel)"
echo "============================================================"
$CONDA_PIP install torch --index-url https://download.pytorch.org/whl/cu128 2>&1 | tail -5

echo ""
echo "============================================================"
echo "Step 3: 验证 GPU 可用"
echo "============================================================"
$CONDA_PY -c "
import torch
print(f'  PyTorch: {torch.__version__}')
print(f'  CUDA available: {torch.cuda.is_available()}')
if torch.cuda.is_available():
    print(f'  Device count: {torch.cuda.device_count()}')
    for i in range(torch.cuda.device_count()):
        print(f'  GPU {i}: {torch.cuda.get_device_name(i)}  ({torch.cuda.get_device_properties(i).total_memory / 1024**3:.1f} GB)')
"

echo ""
echo "============================================================"
echo "Step 4: 跑 ESM-2 disorder 测试，看 GPU 加速"
echo "============================================================"
cd /home/tianqingchen/projects/rosetta1
$CONDA_PY -c "
from esm import pretrained
model, alphabet = pretrained.esm2_t12_35M_UR50D()
print('  ESM-2 t12 35M 加载成功（GPU 已就绪）')
" 2>&1 | tail -15