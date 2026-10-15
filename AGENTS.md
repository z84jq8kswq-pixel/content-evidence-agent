# 项目约定

## 标准文档

本项目默认使用 skill `content-evidence-standard-docs`。

- `docs/` 是与用户讨论、修改项目的工作区；
- `文档库/` 是定稿的标准文档库，只在关键改动／大版本变更或周期性整理时同步；
- `文档库/` 内不出现过程性表述与 AI 痕迹（第一人称、对话指代、思考过程、修改缘由、未决事项）。

## 常用命令

- 单元测试：`python -m unittest discover -s tests`
- Web 工作台：`python app/server.py --port 8788`
- 缺口任务评测（离线、零成本）：`python harness/run_agent_eval.py --run-id <id>`
- 重复运行噪声带：`python harness/repeat_agent_eval.py`
