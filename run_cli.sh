for pdb in inputs/pdbs/*.pdb; do
    if [ -f "$pdb" ]; then
        echo "=================================================="
        echo " 正在提交高通量任务: $pdb"
        echo "=================================================="
        
        python rosetta_agent.py \
            --mode cli \
            --protein "$pdb" \
            --mutation 76,73 \
            --params "并发数 48, 延迟 1.5" \
            --msg "当前为复合物突变任务，请严格按顺序执行：1.配体参数化 2.带配体弛豫 3.执行饱和突变（直接传 PDB 晶体学编号，工具内部自动转换行号，切勿手动转换）"
            
    else
        echo " 未找到匹配的 PDB 文件，请检查 inputs/pdbs/ 目录。"
    fi
done