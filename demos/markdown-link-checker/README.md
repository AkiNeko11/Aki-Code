# Markdown 链接检查器：录制准备

[▶ 观看本用例的终端录屏（MP4，约 1 分 14 秒）](../demo.mp4)

演示项目名为 **PaperTrail Link Check**，只依赖 Python 标准库。它会检查 Markdown 中本地链接的目标文件是否存在。

本例有意保留一个缺陷：从项目根目录检查 `docs/guide.md` 时，实际存在的 `../README.md` 和 `notes/design.md#local-links` 被误报为不存在。固定的 8 项测试中，初始状态是 **3 项失败、5 项通过**，覆盖子目录、上级目录，以及当前目录同名文件造成的误判；其余测试覆盖已有正常行为。

## 1. 录制前准备

在 **Aki-Code 仓库根目录**打开 Windows PowerShell 或 PowerShell 7，确认项目 `.venv` 已安装根目录 `requirements.txt`，API 配置仍使用根目录 `.env`。

```powershell
.\demos\markdown-link-checker\demo.ps1 -Action Prepare
.\demos\markdown-link-checker\demo.ps1 -Action Check
```

`Prepare` 创建新的初始副本和独立的本地 Git 基线，位置是 `.task_outputs/demos/markdown-link-checker/run-时间-编号/`；旧副本保留，主工程源码和模板不会被重置。`Check` 离线检查导入路径、工作目录、中文编码及状态路径，不请求模型。

如果 PowerShell 拦截未签名脚本，可以仅对这次进程使用：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\demos\markdown-link-checker\demo.ps1 -Action Prepare
```

后面的命令也可以使用同样的 `-File ... -Action ...` 形式，无需永久修改系统执行策略。

## 2. 开始录制，展示问题

```powershell
Clear-Host
.\demos\markdown-link-checker\demo.ps1 -Action Before
```

脚本会实际执行下面两条命令并展示原始结果：

```console
python -B -m linkcheck docs/guide.md
python -B -m unittest discover -s tests -v
```

初始预期：检查器报告 `2 missing`，测试报告 `Ran 8 tests` 和 `FAILED (failures=3)`。`Before` 要求检查命令退出码为 1，并严格核对测试确实是 8 项中 3 项断言失败；运行环境出错不能当作演示中的业务失败。

## 3. 启动真正的交互模式

```powershell
.\demos\markdown-link-checker\demo.ps1 -Action Start -CopyTask
```

启动脚本将刚准备的副本设为 Agent 工作区，使用 Aki-Code 的正常交互入口；模型任务会使用你配置的 API。`-CopyTask` 会把 [task.txt](task.txt) 中的单行提示词放入剪贴板，方便进入交互界面后粘贴。

如果当前终端无法访问剪贴板，脚本会提示直接打开 `task.txt` 手工复制。

在 Agent 提示符中，先输入：

```text
/mode accept-edits
```

再按 `Ctrl+V` 粘贴任务，按回车发送。提示词已说明复现现象、保留的行为、禁止改动的固定测试与示例文档，以及验证命令；没有提供参考补丁。

初次演示的主线是：**一条任务 → 自主读取与修改 → 测试验证 → 最终说明**。保留关键工具调用和测试结果；较长等待可以在剪辑时加速并标注。

如果模型停在解释而没有完成修复，可以真实地追加：

```text
请继续完成实际代码修改，并运行现有测试和 docs/guide.md 的检查命令；确认通过后再总结结果。
```

这属于真实追加交互，录制时保留它。完成后可输入 `/usage` 查看本次用量，再输入 `/exit` 退出。

## 4. 展示修复后的验收结果

退出 Agent 后，在原来的 PowerShell 中执行：

```powershell
.\demos\markdown-link-checker\demo.ps1 -Action After
```

`After` 会：

1. 校验原有 Markdown 示例和 `tests/test_checker.py` 与准备时一致。
2. 重跑检查命令，要求退出码为 0；成功输出应为 `0 missing`。
3. 运行全部测试，要求通过；Agent 新增的测试文件也会被发现。
4. 展示 `git status` 和实际 diff。

现有固定测试保持不变；需要扩展回归时可新增测试文件。模型的完成声明不代替这一步实际验收。如果测试仍失败，可以继续给同一运行副本追加任务，或者准备新副本重新录制。

## 5. 重录和查看工作区

重新从有缺陷的版本开始：

```powershell
.\demos\markdown-link-checker\demo.ps1 -Action Prepare
```

后续 `Before`、`Start`、`After` 都自动使用这次最新副本；之前的修改保留在旧运行目录。

查看当前副本的确切位置：

```powershell
.\demos\markdown-link-checker\demo.ps1 -Action Where
```

每个新副本有独立的记忆、任务、定时任务记录及其他运行状态目录，避免继承主工程旧状态。重复启动同一副本会继续使用该副本已有状态，重新 `Prepare` 才会得到新的起点。启动脚本结束后会还原终端原来的环境变量和工作目录。

## 已验证的范围

- 初始模板：8 项测试，3 项失败；CLI 报告 2 个缺失链接。
- 独立校验副本：修复定位基准后，原有 8 项测试全部通过，CLI 报告 0 个缺失链接。
- 原始模板保持带缺陷状态，未在录制工作区提前放置参考答案。
- Python 程序不需要安装额外依赖，不检查外部网站可达性或 Markdown 标题是否存在。

以上是录制前的离线准备检查；实际 Agent 的修复与测试过程见[已录制的终端演示](../demo.mp4)。
