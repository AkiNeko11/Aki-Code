# SWE-bench Lite 评测驱动

在 Linux Docker 容器中运行 Aki-Code，再使用 **SWE-bench 4.1.0 官方评测器**独立判分。支持单题、固定抽样、全量执行、按题续跑、单独重试判分及用量汇总。默认串行，通过 `--workers` 开启按题并行，通过非交互入口启用专用测评权限。

默认仅运行 `sympy__sympy-20590`；完整数据集需要显式指定 `--all`。默认先验证参考补丁，验证通过后才调用模型。`run` 会消耗真实 API；`doctor`、`validate`、`grade` 和 `status` 不调用模型。

## 1. MacBook / Mac mini 安装

需要 Git、**Python 3.11+**、已经启动的 Docker Desktop，以及可访问 GitHub、Hugging Face、PyPI、Docker Hub 和模型 API 的网络。建议使用独立的 Python 3.11 环境。Docker 必须运行 Linux 容器。

Apple Silicon 的宿主机是 ARM，本驱动统一使用 **`linux/amd64` / x86_64 官方题目镜像**，依赖 Docker 的兼容执行；这与原生 ARM 构建是不同路径。速度和兼容性需要在租赁设备上实际检查。`--build-images` 也构建 x86_64 镜像，并不是 ARM 开关。Docker 的虚拟机后端和 amd64 加速选项随版本变化，请按安装版本的官方设置选择。

SWE-bench 官方建议至少 8 核、16GB 内存、120GB 空闲空间；Docker Desktop 的虚拟机资源和磁盘上限也需要足够。建议先短租、单并发验证，再扩大规模。官网的存储数字是估算，实际占用需要持续观察。

在克隆后的项目根目录执行：

```bash
python3.11 -m venv .venv-swebench
source .venv-swebench/bin/activate
python -m pip install -r benchmarks/swebench/requirements.txt
cp .env.example .env
```

编辑 `.env`，配置 `DEEPSEEK_API_KEY`、`DEEPSEEK_BASE_URL`、`MODEL_NAME`。环境变量优先于 `.env`；也可以使用 `--env-file /path/to/config.env` 指定其他文件。Key 只传给推理容器中的进程，不写入运行清单或 Docker 镜像。

```bash
python -m benchmarks.swebench doctor
```

`doctor` 报告宿主/引擎架构、Docker 内存、CPU、项目所在磁盘的空闲空间及 Key 是否存在，并通过一个小型 amd64 Alpine 容器检查兼容执行。`--skip-probe` 可跳过拉取与执行。宿主空闲空间不代表 Docker 虚拟磁盘的剩余空间，还要在 Docker Desktop 中检查。

Linux / WSL 使用相同命令。WSL 需启用对应发行版的 Docker Desktop 集成，或自行提供可用的 `DOCKER_HOST`；不要照搬其他电脑的 socket 路径。

## 2. 先跑一道题

```bash
python -m benchmarks.swebench run \
  --output-dir .task_outputs/swebench/mac-smoke
```

这条命令依次完成：

1. 下载 Lite 的 `test` 数据，固定数据版本，并保存选中题目的官方数据快照。
2. 固定 Agent 源码、模型与上下文配置，下载一次 Linux / Python 3.11 的 Agent 依赖 wheel。
3. 获取该题的 x86_64 镜像，在全新容器中用官方参考补丁验证环境。
4. 在另一个全新容器中把仓库还原到题目指定的 `base_commit`，运行 Aki-Code。
5. Agent 停止后，由外部 runner 停止残留任务、收集相对 `base_commit` 的 Git 改动，再在全新容器中使用官方测试判分，保存官方报告和汇总。

Agent 只收到问题描述和统一修复要求，不收到参考补丁、官方测试补丁、评分测试 ID 或历史运行结果。完整数据快照保存在宿主机，宿主目录和 Docker socket 不挂载给 Agent。Agent 自身使用独立的 Python 3.11 环境，执行仓库命令时优先使用该题 `testbed` 环境。

测评中可修改 `/testbed`，复现脚本、下载的检查材料和测试日志放在 `/tmp/akicode-task`，`TMPDIR` 指向该目录。运行记录及最终补丁由 runner 管理，模型使用 `git diff` 查看当前修改；已有工具归档仍只能用 `glob` / `read_file` 只读取回。禁止的操作返回拒绝原因和允许目录，交给模型选择其他操作；连续三次同工具、同原因的拒绝使运行以 `blocked` 停止。成功获得许可的工具调用会重置该计数，全局调用、Token、时间限额继续生效。需要其他审批的命令也返回拒绝，不自动放宽权限。普通交互和默认非交互运行沿用原有权限行为。

也可以先单独验证参考补丁，不配置 API Key：

```bash
python -m benchmarks.swebench validate \
  --output-dir .task_outputs/swebench/mac-reference
```

`validate` 的成功只表示参考补丁通过，不计为 Agent 解题成功。参考验证和 Agent 试跑使用不同输出目录。

默认预算为主 Agent 60 轮、全部模型请求 80 次、1,000,000 Token 软阈值、Agent 执行 600 秒；每个环境准备步骤和官方测试分别限制为 1800 秒。Agent 时限不包括下载和依赖准备。外层额外留出 60 秒用于 Agent 收尾；超时后先终止任务进程，尽量保留容器用于收集补丁，随后移除容器。容器已退出、Docker 不可用或无法停止残留任务时，补丁收集可能失败，会记录为 `unavailable`，不会伪装成空补丁。Token 包含缓存输入；在途请求可能超出软阈值。

Mac 上如需更长时限，先在新的试跑目录中调整，再固定正式配置：

```bash
python -m benchmarks.swebench run \
  --output-dir .task_outputs/swebench/mac-smoke-longer \
  --timeout 1200 --eval-timeout 1800
```

## 3. 查看、续跑与重试判分

```bash
python -m benchmarks.swebench status \
  --output-dir .task_outputs/swebench/mac-smoke

python -m benchmarks.swebench run \
  --output-dir .task_outputs/swebench/mac-smoke --resume
```

`status` 读取原子保存的题目状态并即时汇总，不写文件，不需要 Docker、评测依赖或 Key。`--resume` 使用原来保存的源码、配置和题目快照，跳过已完成题目；未完成的题目从新的 attempt 开始，不恢复单题中途的 Agent 会话。已记录为错误或中断的题目默认保留，显式重试：

```bash
python -m benchmarks.swebench run \
  --output-dir .task_outputs/swebench/mac-smoke --resume --retry-errors
```

重试保留旧 attempt，并把它的模型用量计入总消耗。不要把多次尝试中成功的一次直接标为 pass@1。已完成但未解决的题目不会自动再试。

如果 runner 已收集有效补丁，只有官方判分失败或尚未进行，可以只重试判分，避免再次调用模型。即使 Agent 未能写出自己的 `result.json`，仍可使用已收集并校验的补丁：

```bash
python -m benchmarks.swebench grade \
  --output-dir .task_outputs/swebench/mac-smoke
```

每次判分使用新的官方 `run_id` 和日志目录，避免旧结果缓存。已有成功判分会被跳过；修改补丁、运行配置或 benchmark 驱动后，应创建新的运行。更新 Agent 主体源码后，旧运行仍使用原先保存的 Agent 源码快照。

本版清单为 `schema_version: 2`。旧版使用 Agent 本地导出的补丁，本版使用 runner 收集的补丁，因此升级后请新建输出目录；旧结果仍可用 `status` 查看，续跑旧结果须切回原 Git 版本。

同一运行目录只允许一个调度进程；并行 worker 各写自己的题目目录，调度进程统一更新汇总。关闭终端或设备重启后，可以重新激活环境并执行上述续跑命令。长时间运行应使用远程设备支持的持久会话或后台任务方式，避免睡眠中断。

## 4. 小批量与全量

固定抽取 20 题作为迁移试跑，记录种子和完整题目列表：

```bash
python -m benchmarks.swebench run \
  --output-dir .task_outputs/swebench/pilot-20 \
  --sample 20 --seed 17
```

抽样按 seed 与题目 ID 的 SHA-256 排序，跨 Python 版本保持一致；这不是完整 Lite 成绩。指定题目使用 `--instance-ids ID1 ID2`，与 `--sample`、`--all` 互斥。

两题并行试跑：

```bash
python -m benchmarks.swebench run \
  --output-dir .task_outputs/swebench/wsl-parallel-5 \
  --sample 5 --seed 42 --workers 2
```

`--workers 2` 表示最多同时运行两道题，任意一道结束后自动补入下一题。每道题的镜像获取、参考验证、Agent 修复和官方判分由独立进程执行，工作目录、容器及日志相互隔离。源码、数据和依赖在调度进程中准备一次，最终统一生成 `summary.json` 和 `predictions.jsonl`。本地构建镜像时，共用基础环境的构建在同一运行内串行进行，题目执行仍可并行。

默认 `--workers 1`；Token、轮数、请求和时间预算都按每题独立计算。并发数保存进清单，`--resume` 时省略 `--workers`，自动沿用原值。修改并发数或升级驱动请新建输出目录。`validate` 也支持此参数；单独的 `grade` 命令保持串行。

中断时调度进程停止分配新题；Linux/macOS 下先给运行中的 worker 共计 20 秒收尾机会，再终止其残留进程并清理本次运行的容器。Windows 宿主直接终止 worker 进程树，再清理容器。worker 异常退出时，先清理该题遗留的容器再补入下一题，其他题目继续执行。已完成题目保留，中断题目可通过 `--resume --retry-errors` 重跑；已有补丁也可以单独 `grade`。强制终止 worker 时仍可能无法完整收集补丁。并发会增加 CPU、内存、带宽和 API 请求压力，不保证耗时按并发数等比下降。

正式运行前固定提交、模型、预算及提示词。完整 Lite `test` 集为 300 题：

```bash
python -m benchmarks.swebench run \
  --output-dir .task_outputs/swebench/lite-baseline-001 \
  --all --timeout 1200
```

默认每题先验证参考补丁；如果已有足够环境验证依据，可在创建新运行时用 `--skip-gold` 跳过。Goal 默认关闭，长期记忆始终关闭；可通过 `--goal "条件"` 创建单独的 Goal 实验，条件中不要包含官方评分测试信息。

默认 `--cache-level env`：完成当前题目后，删除本驱动新获取的题目镜像，保留运行前已有镜像和共享层/本地构建的基础环境缓存。`--cache-level instance` 保留题目镜像，会显著增加磁盘占用。镜像按题获取，不提前下载所有题目；官方判分也按题执行，避免提前准备整批环境。

没有全局 prune 操作，不删除其他任务的容器或镜像。中断后会清理当前运行有所有权标签的推理容器，以及当前判分的确切容器。强制杀死宿主进程可能留下镜像或官方判分容器，需要根据该运行的日志确认并清理。

如果 Docker Hub 预构建镜像不可用，可在新运行中加 `--build-images`，调用官方构建器本地构建；该路径依赖项目的历史依赖源可用，通常更慢。`image-build.log` 从首次查询本地镜像开始记录阶段与完整异常；Docker 连接中断、连接超时及部分 5xx 服务错误最多尝试三次。鉴权、镜像不存在和本地构建失败不自动重试。修改镜像来源后使用新运行，保留原失败记录。

## 5. 产物与统计口径

```text
运行目录/
  manifest.json           # 配置、提交、源码 hash、数据版本、题目列表
  dataset.json            # 官方数据快照，含参考答案；仅宿主与判分器可见
  agent-source.tar        # 本次使用的 Agent 源码快照
  agent-requirements.txt  # 固定的 Agent 依赖
  host-packages.txt       # 宿主实际安装的依赖版本
  wheels/ + wheels.json  # Linux wheel 与校验和
  predictions.jsonl      # 最新 attempt 的官方格式提交；原子重建
  summary.json           # 完整选择集的成绩、状态和累计用量
  instances/<ID>/attempt-0001/
    state.json           # 阶段、Agent 状态、判分结果、异常原因
    image.json           # 镜像 ID、digest、架构
    prepare.log
    agent-console.log
    execution.json       # 外部进程执行状态；Agent 无结果时仍保留原因
    patch.diff           # runner 从工作区独立收集的提交补丁
    patch.json           # base_commit、收集状态、字节数和 SHA-256
    patch-collection.log # 补丁收集的 stderr，与补丁正文分离
    agent/run/           # Agent 的 result、messages、events、日志及归档
    prediction.json
    gold/<唯一编号>/     # 独立参考验证日志
    grading/<唯一编号>/  # 官方 report.json、测试输出及过程日志
```

`summary.json` 的 `resolved_rate` 分母始终是本次选中的全部题目；环境失败、缺少补丁、Agent 失败不会从分母消失。`counts` 区分 `pending`、`running`、`resolved`、`unresolved`、`empty_patch`、`error`、`interrupted`。`agent_statuses`、`patch_statuses`、`evaluation_statuses` 分别记录 Agent 运行、补丁收集和官方判分；题目条目保留 `agent_reason` 和异常原因。参考验证使用 `validated`，不计入解决数量。

Agent 的 `completed` 不代表解题成功；官方 `resolved=true` 才计为解决。Agent 限额或失败时如果 runner 仍收集到有效补丁，也会交给官方测试。收集使用临时 Git index，覆盖新增、修改、删除、二进制文件及相对基线的改动，不依赖 Agent 的 `patch_exported` 标记，不修改实际暂存区。空补丁属于 `empty_patch`，收集失败属于错误；二者分开统计。汇总的请求数/Token 累计所有已保存的 attempt；若进程在请求响应前被硬停止，无法获知供应商实际计费，精确费用还需对照供应商账单。

正常完成流水线返回 0，存在环境/执行错误或未完成题目返回 1，用户中断返回 130。补丁未通过测试属于正常的 `unresolved` 结果，不代表驱动崩溃。

运行产物默认放在已忽略的 `.task_outputs/`。可拷贝整个运行目录到本地归档；清理设备前至少保留 `manifest.json`、`summary.json`、提交补丁和官方判分日志。

## 6. 可复现性与边界

- 主执行入口仍是 `python -m aki_code.main run`；本驱动只负责环境、调度、补丁转换和官方判分。
- 使用固定版本的官方评测逻辑，不提供自定义“通过”判定。修改配置使用新运行，失败和复测均保留记录。
- 题目镜像目前通过官方 tag 获取，每题记录实际 ID/digest；跨时间重新开始的运行仍可能获得更新的镜像。严格复现时还需保留原镜像或按记录恢复。
- 使用 x86_64 Linux 容器；Apple Silicon 的兼容执行和并发资源占用需要在目标设备试跑。未提供原生 ARM 镜像执行路径。
- 权限规则不是操作系统沙箱。推理在独立容器中进行，无宿主工作区、Key 文件或 Docker socket 挂载；容器进程仍持有调用模型所需的 Key，并可访问网络。

参考：[官方评测流程](https://www.swebench.com/SWE-bench/guides/evaluation/)、[4.1.0 安装及平台说明](https://github.com/SWE-bench/SWE-bench/blob/v4.1.0/README.md)、[Docker 虚拟机与架构兼容设置](https://docs.docker.com/desktop/features/vmm/)。
