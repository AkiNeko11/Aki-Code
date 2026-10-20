# 评测分析

此目录保存可随仓库发布的评测结论和分析归档。原始测试目录及其 ZIP 仅保留在本地，不提交到 GitHub。

## SWE-bench Lite，2026-10-20

使用 `deepseek-flash`、SWE-bench 官方 harness 4.1.0，完成 Lite `test` 全部 300 题。每题取最新 attempt，**214/300 通过，解决率 71.33%**；仅 gold 在本环境验证通过的 295 题中为 **214/295 = 72.54%**，这是诊断子集口径。

| 文件 | 内容 |
| --- | --- |
| [核心报告](swebench_lite_20261020/report.md) | 整体结果、归因、统计口径、改进优先级及 86 道未通过题逐题清单 |
| [Excel 分析表](swebench_lite_20261020/analysis.xlsx) | 总览、300 题明细和 326 次尝试历史，支持筛选 |
| [完整分析 ZIP](swebench_lite_20261020/swebench_lite_20261020_full.zip) | 完整报告、离线交互分析、CSV/JSON、专项核查、分析与验证脚本、预览及检查记录 |
| [SHA-256 校验值](swebench_lite_20261020/SHA256SUMS) | 核心报告、Excel 和完整分析 ZIP 的校验值 |

未通过的 86 题包括：37 道补丁测试未通过、20 道达到轮数上限仍无补丁、17 道疑似 thinking 输出截断后结束、5 道 API 断连失分、5 道 gold 验证阻断、2 道子模块工作区接入失败。具体证据和判断边界见报告。

本批有基础设施恢复重试，且部分成功题读取了其他版本的源码或测试，因此这里披露实际执行条件，不将 71.33% 标为严格 pass@1，也不把排除故障的样本自动计为成功。

## 目录保持精简

```text
analysis/
  README.md
  swebench_lite_20261020/
    report.md
    analysis.xlsx
    swebench_lite_20261020_full.zip
    SHA256SUMS
```

完整分析 ZIP **不包含 `task_output-wsl/` 原始目录**。归因材料保留支持结论的证据路径和必要摘录；本机运行时的 `node_modules` 目录联接未打包。

## 解压和复核

在项目根目录的 Windows PowerShell 中，将完整材料解压到已忽略的本地工作目录：

```powershell
Expand-Archive `
  -LiteralPath .\analysis\swebench_lite_20261020\swebench_lite_20261020_full.zip `
  -DestinationPath .\.task_outputs\analysis-unpacked-20261020
```

先阅读解压目录内的 `ARCHIVE_README.md`。离线交互分析位于 `analysis/swebench_lite_20261020/dashboard.html`；ZIP 内的 `ARCHIVE_MANIFEST.json` 记录各文件的字节数和 SHA-256。

分析 Python 脚本只使用标准库，需要 Python 3.11+。仅从归一 JSON 重建报告无需原始日志；重建归一数据需要另外提供原始运行目录。Excel 生成脚本依赖 `@oai/artifact-tool`，运行时依赖未随归档分发。

可用以下命令取得 ZIP 校验值，与 `SHA256SUMS` 中同名条目核对：

```powershell
Get-FileHash `
  -Algorithm SHA256 `
  -LiteralPath .\analysis\swebench_lite_20261020\swebench_lite_20261020_full.zip
```

## 仅本地保留的原始数据

本次原始运行目录为 `task_output-wsl/lite-baseline-001/`。其完整归档位于：

```text
.task_outputs/archives/swebench-lite-baseline-001-raw-20261020.zip
.task_outputs/archives/raw-archive-manifest.json
```

归档内保留 `task_output-wsl/` 顶层路径，清单记录原始文件的 SHA-256。原始目录与 `.task_outputs/` 均由 `.gitignore` 排除；原始 ZIP 和校验清单不属于公开分析包。整理前的完整分析工作目录另保留在本地 `.task_outputs/analysis-working/swebench_lite_20261020/`。
