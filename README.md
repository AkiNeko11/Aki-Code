# Aki-Code
my coding agent

## 安装

```bash
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env，填入你的 DeepSeek API Key
```

## 运行

```bash
python -m aki_code.main
```

## 项目结构

```
aki_code/
├── main.py           # CLI 入口 + 对话循环
├── config.py         # 配置管理
├── llm/
│   ├── client.py     # DeepSeek API 客户端
│   └── prompts.py    # System Prompt
└── tools/
    ├── registry.py   # 工具注册与调度
    ├── bash.py       # 执行 shell 命令
    ├── file_read.py  # 读取文件
    └── file_write.py # 写入文件
```

## S16：保存的工作流

工作流由项目中的 Python 代码注册。主 Agent 可以调用 `workflow` 工具，
也可以通过以下 CLI 命令直接执行；外层同步等待，内部步骤使用有上限的并发。

```text
/workflows list
/workflows run review-changes {"changes":"检查当前工作区中修改的 Python 文件","dimensions":["correctness","error handling"]}
/workflows runs
/workflows show RUN_ID
/workflows resume RUN_ID
```

`review-changes` 会分别审查各个维度，对每条候选问题再做独立模型复核，最后去重排序。
`changes` 可以传入固定 diff 或明确的审查范围；首版审查工具仅支持仓库内受限读取、搜索，
没有 shell、文件修改、团队控制或 MCP 工具。报告会明确标注这是模型审查结论，没有实际执行测试。

也可以直接对 Aki 说：“先读取当前工作区的实际 git diff，再把完整 diff 作为 `changes` 参数调用
`review-changes`，检查正确性和异常处理。”传入完整 diff 能明确变更前后的代码；
仅传入文件范围时，工作流可以检查当前源码，但该范围本身不提供版本前后的差异证据。

状态快照、成功步骤的结果 journal 和最终报告保存在 `.workflows/`（可通过 `WORKFLOWS_DIR` 修改）；
进度事件在执行期间实时显示。
恢复时会核对流程版本、参数、工具/模型配置和工作区快照，只复用成功且校验通过的步骤。
读取的文件内容也会按快照 hash 校验。检测到输入变化的运行不可恢复，需要发起一次新运行。
代码或输入发生变化后，应重新运行。取消会停止调度新步骤，等待正在执行的请求收尾后保存状态；
按 Ctrl+C 可以中断当前调用，随后通过 `runs`、`show` 查看保存结果。

可选环境变量：

| 配置 | 默认值 | 含义 |
| --- | --- | --- |
| `WORKFLOW_MAX_CONCURRENCY` | `3` | 并发模型调用上限 |
| `WORKFLOW_MAX_CALLS` | `60` | 一次执行的模型调用上限，包含格式修复 |
| `WORKFLOW_MAX_TURNS` | `12` | 单个 Agent 步骤的模型轮数上限 |
| `WORKFLOW_MAX_TOKENS` | `100000` | 一次执行的 Token 软阈值 |
| `WORKFLOW_REQUEST_TIMEOUT` | `120` | 单次请求超时，秒 |

每次运行或恢复都会重新计量本次预算。结果中的 `usage` 是本次真实请求消耗，
`total_usage` 是此运行的历史累计消耗。Token 在请求完成后统计，并发的在途请求可能使总量超过阈值；
调用次数与并发上限在调度时约束。工作流用量也计入会话 `/usage`。
