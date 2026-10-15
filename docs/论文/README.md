# 论文与实验索引

- [论文正文](补天经验生命周期与证据约束研究.md)
- [Word 版论文](补天经验生命周期与证据约束研究.docx)
- [数学控制实验脚本](run_mechanism_experiments.py)
- [实验协议](实验协议.md)
- [数据字典](数据字典.md)
- [认识论与人类思考逻辑](认识论与人类思考逻辑.md)
- [文献筛选记录](references/文献筛选记录.md)

## 当前状态

2026-10-15 已完成数学模型论证、机制控制实验、结果图表和论文排版。实验类型为 `mechanism_control`，实际模型运行数为 0。45 个 B0/B1/B2 同一宿主Agent输入尚未执行，因此不报告宿主模型效果、成本或跨模型泛化结论。

结果目录：`experiment_results/math-controls-20261015/`。其中包含 JSON 结果、CSV 明细、300dpi PNG 和 SHA256 manifest。研究与数学专项测试：19 passed。


2026-10-15：当前宿主 Agent 已完成五类任务各 B0/B1/B2 共15条非盲测条件回答，原始记录见 `docs/论文/experiment_results/within-agent-pilot-20261015/agent-responses.json`。结果仅作同Agent机制试跑；上下文携带、先前答案暴露、model_calls和cost已如实记录，不能当作独立样本或第三方审计。

## 五项命题扩充与生产实测

论文DOCX新增第9—12章，包含容量与隐含信息保留、监督与幻觉、涌现解释、项目闭环条件证明、经验积累到后训练的五项研究命题，并区分条件证明、合成控制和生产实测。当前未执行任何神经模型参数训练，不能将运行期经验积累称为已证明的参数自进化。

- `run_persistent_experience.py`：真实生产服务12条经验持久化试验，写入前0/12、关闭重启后12/12召回；结果在 `experiment_results/persistent-experience-20261015/`。
- `run_evolution_controls.py`：容量玩具模型、标签噪声、多次使用的任务覆盖模拟；结果在 `experiment_results/evolution-controls-20261015-r2/`。
- 回归：42 passed；DOCX生成统计：约7926汉字、15条引用；精确正文以DOCX为准。
