# Aki-Code SWE-bench Lite 300 题评测分析

> 本目录保留核心报告和 Excel。[完整分析 ZIP](swebench_lite_20261020_full.zip) 包含专项核查、交互分析、CSV/JSON、脚本与验证记录。原始运行目录仅本地保存，不包含在此公开 ZIP 中。

分析日期：2026-10-20。运行：`lite-baseline-001`，run_id `559f352a5911`。模型为 `deepseek-flash`，官方 harness 版本 4.1.0，2 个 worker。此次只读取测试脚本及保存的日志、补丁和官方报告，没有重新调用模型、运行 Docker、修改 Agent 或覆盖原始结果。

## 先看结论

**当前最新 attempt 的完整成绩为 214/300 = 71.33%。86 道未通过里，能直接确认是 gold/外部网络/接入兼容性阻断的有 12 道；另有 17 道呈现非常一致的单次输出截断迹象。** 因此，直接把 86 道统称为模型解题失败会误导改进方向。

| 最新结果与失败机制 | 题数 | 占全部300题 | 判断依据 |
| --- | ---: | ---: | --- |
| 官方通过 | 214 | 71.33% | 官方 report.resolved=true |
| 有补丁但未通过官方目标测试集合 | 37 | 12.33% | 31题仅F2P失败、3题仅P2P失败、3题两类均失败 |
| 达到60轮上限，最终无补丁 | 20 | 6.67% | limit + 空diff |
| 疑似thinking输出截断，最终无补丁 | 17 | 5.67% | completed，但末轮只有thinking，输出增量恰好4096 |
| 模型API流式连接中断，最终无补丁 | 5 | 1.67% | incomplete chunked read，无工作区改动 |
| Gold验证失败，Agent未启动 | 5 | 1.67% | gold的F2P全部通过，P2P失败，重试复现 |
| Runner不支持子模块工作区 | 2 | 0.67% | 控制台明确抛出Submodule workspaces are not supported |
| 合计 | 300 | 100.00% | 互斥分类，每题只计一次 |

`F2P` 是问题需要修复的 FAIL_TO_PASS 测试，`P2P` 是要求保持通过的 PASS_TO_PASS 回归测试。表内“输出截断”是强证据诊断，日志没有保存原始 stop_reason，因此没有将其写成已经证实的服务端截断原因。

## 成绩口径：保留全量成绩，单列诊断子集

| 口径 | 分子／分母 | 比例 | 用途 |
| --- | ---: | ---: | --- |
| 最新attempt，全量300题 | 214／300 | 71.33% | 本次全流程观测成绩 |
| 仅gold在本环境通过的题 | 214／295 | 72.54% | 排除无法有效评测的5题，辅助判断 |
| 再排除5道API中断失分及2道子模块接入失败 | 214／288 | 74.31% | 条件子集诊断，具有选择偏差，不替代全量成绩 |
| 仅最终提交非空补丁的题 | 214／251 | 85.26% | 补丁提交后的通过比例，不能当成总体成功率 |

不把排除的题自动补成成功。17道疑似截断和20道达到轮数上限的题也没有被加分。是否能修复并通过，需要修正后独立重跑，不能由现有记录推算。

## 最需要优先处理的异常：thinking-only 被当作 completed

236道Agent状态为completed的题中，有21道最后一次assistant消息只有thinking，没有text或tool_use；这21道末次响应的output_tokens增量全部为4096，与manifest中的单次 `MAX_TOKENS=4096` 一致。其中17道为空补丁、3道有补丁但不通过、1道已通过（sympy-19007）。

例如sympy-22005只执行4轮，最后一轮消耗4096输出token，随后记录completed，工作区仍为空补丁。这与60轮总预算耗尽不同。不能把completed作为“模型完整结束解题”的可靠信号，也不能只靠“加大每题总token预算”修复这一类问题。

建议在后续实现中保留原始stop_reason、响应块类型、单次输出增量；当响应只有thinking或明确被截断时，应进入可恢复的继续执行／异常状态，并受总调用、时间、token预算约束。先用这17题做针对性复测，再比较完整批次。这里提出的是工程验证方向，本次没有读取或修改Agent实现。

17道空补丁题：

django__django-11564、django__django-11630、django__django-12125、django__django-14017、django__django-15388、django__django-15400、django__django-15695、django__django-15996、django__django-16816、matplotlib__matplotlib-23563、psf__requests-2148、pytest-dev__pytest-11148、sphinx-doc__sphinx-7686、sympy__sympy-14317、sympy__sympy-14396、sympy__sympy-16503、sympy__sympy-22005

3道已有补丁但也出现该末轮异常的题：matplotlib-22711、sphinx-8506、sympy-12171。它们同时存在官方测试失败，不能把全部失败责任都归给响应截断。

## Gold失败：是本次测试条件不成立，不能直接说参考补丁写错

以下5题每题保存了两次gold验证，失败测试集合均复现。它们的F2P全部成功，失败都发生在P2P。Agent被gold gate阻止，没有开始推理。

| 题目 | 原始日志现象 | 可支持的归因 |
| --- | --- | --- |
| matplotlib-23913 | test_warn_big_data_best_loc期待UserWarning，实际DID NOT WARN | 警告触发条件／运行表现敏感，具体阈值机制尚未证实 |
| requests-2317 | 对10.255.255.1的连接应ConnectTimeout，实际未按预期超时或为ReadTimeout | 网络环境与测试假设不符 |
| requests-2674 | 同类connect/total-connect-timeout测试失败 | 网络环境与测试假设不符 |
| requests-3362 | 同类连接超时测试失败，另有目标集合外fixture噪声 | 网络假设失效，并伴有历史测试依赖兼容问题 |
| requests-863 | 服务返回Connection: close，测试期待keep-alive | 外部HTTP服务／连接行为与测试假设不符 |

详细测试名、完整证据路径与行号见 [gold专项核查（完整分析包内 `gold_infra_notes.md`）](swebench_lite_20261020_full.zip) 和 `gold_infra_findings.json`。不要将gold验证通过等同于原始pytest全部测试通过：例如Flask-4045的gold原始输出有116 failed/11 errors，但官方跟踪的目标集合仍全部通过；Xarray-4094的gold和Agent日志都有4个失败，官方结果仍是通过。这些基线噪声不能算Agent新引入的缺陷。按可识别的外层测试套件末尾汇总，共24题的gold原始输出含failed/errors，其中19题官方目标集合仍通过。此统计避免将pytest项目嵌套子测试的预期失败计入，不应解释为24道环境阻断题。

## API与运行接入问题

最新attempt出现明确API流式断连的共有6题，其中5题失分：astropy-14182、django-11742、django-11848、django-16910、matplotlib-25332。matplotlib-25079虽然同样断连，已收集到的补丁仍通过，所以API事件数是6，实际失分数是5。

astropy-6938、astropy-7746的控制台均明确报 `ValueError: Submodule workspaces are not supported by this runner yet.`。这是运行接入兼容性缺陷，不应归为模型API问题或模型不会解题。

当前脚本的 `--retry-errors` 只重试流水线error/interrupted。上述API中断如果最终产生empty_patch，就不会被它自动选中。因此仅重试error并不能补齐API受损样本。建议未来根据独立的失败原因选择恢复路径：有可校验补丁先重试判分，未生成补丁且明确传输失败再恢复Agent；所有历史尝试继续保留。

## 轮数、权限拒绝与真正的补丁失败

50题触发的全部是60轮主模型上限，没有观察到这些题是80次调用上限或100万token上限停止。其中21题通过、9题有补丁但未通过、20题没有补丁。达到上限不等于补丁错误，但20道始终未落地修改的题值得重点观察“定位／探测消耗过长”的行为。它们消耗14,771,996 tokens，约占最新attempt总token的14.89%。

latest日志共有255次权限拒绝，分布在153题。92道最终通过的题也出现过拒绝，所以拒绝次数只能作为排查信号，不能独立判定主因。常见拒绝围绕git元数据、越界临时目录和runner管理的输出路径。可将允许写入目录、测试临时文件位置与runner职责更清楚地暴露给Agent，减少重复试探；不需要先放宽所有权限。

37道已判分未通过的题中，官方记录40个F2P failure和11个P2P failure。这是测试项数量，不能当成题数。Django-14155有一条F2P实际打印ok，但和前一个测试粘在同一日志行，被官方parser漏掉。该题另外两条F2P仍有真实断言失败，整题仍为unresolved；没有因此调整成绩。其余已核查失败有真实失败输出。

目标测试已通过、仅回归失败的3题是django-13660、pylint-7114、sphinx-10451。这些题应优先检查修复的泛化边界，不能只继续优化问题描述中的单个示例。具体到每道题的失败测试、停止状态、日志解释与下一步建议，见后面的86题清单和可筛选总表。

下面几例说明，不能把原始日志中的其他兼容报错当作排除Agent责任的充分证据：

| 题目 | 官方目标失败的具体原因 | 对改进的启示 |
| --- | --- | --- |
| django-12700 | 递归脱敏把tuple统一变成list，嵌套容器类型不符 | 修复值的同时保持返回类型契约 |
| django-13660 | 目标2项通过，但exec使用空字典导致原有globals行为失效 | 复现新问题后也验证旧调用上下文 |
| matplotlib-22835 | BoundaryNorm不再崩溃，数值输出仍为[-1.]而非[-1.0] | 格式和精度是外部行为的一部分 |
| scikit-learn-10949 | 警告已出现，但消息的was converted与were all converted不符 | 与gold对照可区分真实目标失败和pytest兼容噪声 |
| sympy-13043 | issue允许多个解决方向，Agent选排序list，测试要求set | 结合既有API契约决定返回容器，避免只满足叙述中的一种解释 |

## 重试记录与pass@1

本目录有326个attempt，26题各有两次。第一次流水线通过197题（65.67%），之后最新结果为214题。26个历史attempt里，24个没有模型请求，多为镜像、前置网络或gold阻断；不能把17题恢复通过都说成模型多次采样收益。

仅django-11564、matplotlib-18869实际各运行过两次Agent。它们第一次都留下非空补丁，官方判分却没有生成report，之后整个流水线重跑。按每题第一次Agent启动统计，现有证据确定213题通过，2份旧补丁结果未知。因此严格的该口径尚不能补齐；全量确定通过数为213，若这两份旧补丁都通过则为215。**213至215只是两份缺失判分造成的结果区间，不是统计置信区间。**

现有grade命令只处理最新attempt，不能直接找回这两份旧补丁。建议将旧patch及其原配置复制到独立恢复运行中单独判分；原结果目录保留。本次没有执行该恢复。

## 项目分布

| 项目 | 题数 | 官方通过 | 空补丁 | 补丁未过 | Gold阻断 | 全量通过率 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| astropy/astropy | 6 | 3 | 3 | 0 | 0 | 50.00% |
| django/django | 114 | 90 | 18 | 6 | 0 | 78.95% |
| matplotlib/matplotlib | 23 | 17 | 2 | 3 | 1 | 73.91% |
| mwaskom/seaborn | 4 | 2 | 1 | 1 | 0 | 50.00% |
| pallets/flask | 3 | 2 | 0 | 1 | 0 | 66.67% |
| psf/requests | 6 | 1 | 1 | 0 | 4 | 16.67% |
| pydata/xarray | 5 | 4 | 0 | 1 | 0 | 80.00% |
| pylint-dev/pylint | 6 | 4 | 1 | 1 | 0 | 66.67% |
| pytest-dev/pytest | 17 | 12 | 3 | 2 | 0 | 70.59% |
| scikit-learn/scikit-learn | 23 | 21 | 0 | 2 | 0 | 91.30% |
| sphinx-doc/sphinx | 16 | 10 | 2 | 4 | 0 | 62.50% |
| sympy/sympy | 77 | 48 | 13 | 16 | 0 | 62.34% |

SymPy有16道补丁未过、13道空补丁，是最多的未解决来源（29题）。Django未解决24题，其中18道为空补丁，比其6道补丁未过更突出。scikit-learn为21/23，SymPy为48/77；不同项目难度和样本数不同，这里是描述性比较，不代表能力因果结论。Requests原始1/6主要被4道gold阻断拉低，不能据此判断Agent对Requests尤其弱。

## 用量与耗时

全部attempt累计9,247次请求、9,235次有返回usage的调用、99,620,472 tokens。构成为28,285,519非缓存输入、67,506,944缓存读取输入、3,828,009输出；包含缓存token，不能直接乘统一价格推算费用。缺少响应的请求也可能有供应商计费，精确费用需供应商账单。

仅最新attempt为9,208次请求、99,219,675 tokens。重试被替换的两份模型答案额外消耗39次请求、400,797 tokens，已正确计入summary。没有把重试成本丢掉。

Agent elapsed累计28,889.937秒（8.03小时），是跨题和跨attempt累加的Agent时间，不是墙钟时间。保存的首个任务开始至最后任务完成约为北京时间2026-10-20 01:13:38至12:03:21（10小时49分44秒）。环境准备、gold和官方判分耗时不在Agent elapsed中。

最新有有效耗时记录的293题，Agent耗时中位数79.29秒、P90为179.20秒；2道启动失败没有Agent result耗时，5道gold未启动。仅1题运行到1200秒超时（sympy-20590），补丁仍通过。提高墙钟时限目前不是最有数据支持的首要改进方向。

## 额外源码资料的使用

日志中观察到下载其他发行版本及查看其源码的行为，需与失败原因分开审计。73题命中了pip download/PyPI相关命令，这只证明尝试，不能证明全部成功；此候选口径也没有覆盖所有raw GitHub下载。一个已通过题django-11019的attempt-0002明确下载并读取后续Django stable/3.0.x版本的widgets.py、test_media.py及topological_sort.py，并与本地源码／测试做diff。events.jsonl的36、62、66、79、88、154行保留相关命令和结果。另见django-15213读取Django4.2源码、sympy-17655跨版本point.py对比。

这些是额外源码资料使用的具体证据。71.33%反映当前工具和联网资料条件下的观测结果；如果目标是测量自行修复能力，应将这种资料访问作为独立实验变量控制和披露。不能仅凭下载行为认定全部答案污染，也不能由此推断SWE-bench官方禁止所有联网。此次没有因此扣分，未推断全部214份正确补丁的来源。具体候选、命令和证据见 `agent_findings.json` 与 [Agent专项核查（完整分析包内 `agent_notes.md`）](swebench_lite_20261020_full.zip)。

## 建议的处理顺序

| 顺序 | 处理目标 | 本次证据 | 验证方式 |
| --- | --- | --- | --- |
| 1 | thinking-only／输出截断的结束判断与恢复 | 17道空补丁，另3道未过和1道通过也有相同末轮形态 | 先保存原始stop_reason，再对17题做独立对照复测 |
| 2 | API流中断恢复、按根因恢复任务 | 6题受影响，5题实际失分；empty_patch不被retry-errors覆盖 | 固定输入和预算，记录每次恢复与调用用量 |
| 3 | 子模块工作区支持 | 2道Astropy在推理前失败 | 先做不调用模型的启动检查，再补跑 |
| 4 | gold环境与外部服务假设稳定性 | 5题持续P2P失败，另有目标集合外基线噪声 | 先独立验证gold稳定通过，再运行Agent |
| 5 | 减少60轮仍无修改的探索，改进候选补丁后的回归验证 | 20道空补丁，37道补丁未过，其中6道涉及P2P | 追踪首次修改时间和失败测试覆盖，按具体案例改进 |
| 6 | 分离模型尝试、判分重试与流水线恢复 | 2题旧非空补丁判分缺失；24次重试无模型 | 补判旧patch，结果／成本分别保留attempt维度 |

这些是基于当前日志的优先级，不是收益预测。修复评测条件与运行缺陷后，仍需独立复测，不能把未完成样本预先计为成功。

## 数据完整性与复核范围

分析从manifest的300个ID出发，按脚本规则选择latest attempt，检查全部326份state。558份存在的gold／Agent官方report均与state的resolved及tests_status完全一致；引用报告均存在。295条predictions没有重复ID，其补丁逐字与latest patch一致（其中44条为空补丁，非空只有251条）。保存的patch哈希无不一致，events.jsonl无坏JSON行。dataset、Agent源码归档、依赖快照SHA匹配manifest，当前benchmark驱动SHA也匹配。

9题补丁触及测试路径，逐份检查见新增测试、断言或相应import，没有发现删除／放宽原有断言，未因路径命中就标记违规。评测正确性仍受官方目标测试集合覆盖范围限制，resolved不等于在所有场景完全正确。

完整方法、代码行证据和官方4.1.0 parser来源见 [评测脚本审计（完整分析包内 `harness_notes.md`）](swebench_lite_20261020_full.zip)。未来复现还需固定镜像digest、历史依赖及外部网络条件；现有镜像tag和版本号不等同于所有执行内容的字节级固定。

## 86道未通过题逐题清单

下面的“机制分类”是互斥主分类；同一道题还可能有权限拒绝、截断、回归失败等次要因素。每题的原始日志证据与更细归因在 `instances.json` 的analysis字段、Excel逐题页和专项JSON中保留。置信程度以专项记录为准，不由关键字自动假定。

| 题目 | 机制分类 | Agent状态 | grading F2P失败／P2P失败 | 具体观察与下一步 |
| --- | --- | --- | ---: | --- |
| `astropy__astropy-14182` | API流式连接中断 | error | 未判分 | 模型API流式响应断开（incomplete chunked read），agent异常终止且最终补丁为空，未运行grading。日志只能证明这次运行受外部连接故障中断，无法据此判断其本可解题。 对流式读失败做可恢复重试/断点续跑，再在相同参数下单独重测。 |
| `astropy__astropy-6938` | Runner不支持子模块工作区 | error | 未判分 | agent-console.log记录ValueError: Submodule workspaces are not supported by this runner yet.；未生成result.json，补丁为空。属于runner工作区识别/兼容故障，非代码修复错误。 修正容器内仓库/submodule工作区识别，在启动阶段预检查，再重测。 |
| `astropy__astropy-7746` | Runner不支持子模块工作区 | error | 未判分 | agent-console.log记录ValueError: Submodule workspaces are not supported by this runner yet.；未生成result.json，补丁为空。属于runner工作区识别/兼容故障，非代码修复错误。 修正容器内仓库/submodule工作区识别，在启动阶段预检查，再重测。 |
| `django__django-11564` | 疑似thinking输出截断且无补丁 | completed | 未判分 | 原result标completed且text为空；最后assistant仅含thinking，无text/tool_use；末次响应输出token增量恰为4096，与max_output_tokens相同，随后run_stopped。runner收集到空补丁。强烈提示模型输出截断被当成正常完成，但未保存finish_reason，不能仅凭日志断言内部实现原因。 保存并检查finish_reason/stop_reason；对仅thinking或length结束继续推理/执行，完成前检查实际补丁是否存在。 |
| `django__django-11630` | 疑似thinking输出截断且无补丁 | completed | 未判分 | 原result标completed且text为空；最后assistant仅含thinking，无text/tool_use；末次响应输出token增量恰为4096，与max_output_tokens相同，随后run_stopped。runner收集到空补丁。强烈提示模型输出截断被当成正常完成，但未保存finish_reason，不能仅凭日志断言内部实现原因。 保存并检查finish_reason/stop_reason；对仅thinking或length结束继续推理/执行，完成前检查实际补丁是否存在。 |
| `django__django-11742` | API流式连接中断 | error | 未判分 | 模型API流式响应断开（incomplete chunked read），agent异常终止且最终补丁为空，未运行grading。日志只能证明这次运行受外部连接故障中断，无法据此判断其本可解题。 对流式读失败做可恢复重试/断点续跑，再在相同参数下单独重测。 |
| `django__django-11815` | 补丁目标测试未通过 | completed | 2／0 | Enum序列化输出使用属性访问 TextEnum.A / RegexFlag.DOTALL，正式测试要求名称索引 TextEnum['A'] / RegexFlag['DOTALL']；2个F2P均失败。最终回复把失败解释为旧测试，但gold在同框架通过，因此不能把该明确的序列化差异归因于框架故障。 按正式序列化格式和特殊Enum/Flag情况补齐实现；保留失败测试作为验收项。 |
| `django__django-11848` | API流式连接中断 | error | 未判分 | 模型API流式响应断开（incomplete chunked read），agent异常终止且最终补丁为空，未运行grading。日志只能证明这次运行受外部连接故障中断，无法据此判断其本可解题。 对流式读失败做可恢复重试/断点续跑，再在相同参数下单独重测。 |
| `django__django-11910` | 达到轮数上限且无补丁 | limit | 未判分 | 达到60个lead model turns上限，最终未留下可收集的工作树补丁，grading未运行。日志显示主要在阅读、复现和方案探索；仅增加轮数能否成功尚无证据。 在前段轮次完成最小补丁并测试，设置剩余轮次提示/检查点；减少重复阅读和低收益探索。 |
| `django__django-12125` | 疑似thinking输出截断且无补丁 | completed | 未判分 | 原result标completed且text为空；最后assistant仅含thinking，无text/tool_use；末次响应输出token增量恰为4096，与max_output_tokens相同，随后run_stopped。runner收集到空补丁。强烈提示模型输出截断被当成正常完成，但未保存finish_reason，不能仅凭日志断言内部实现原因。 保存并检查finish_reason/stop_reason；对仅thinking或length结束继续推理/执行，完成前检查实际补丁是否存在。 |
| `django__django-12700` | 补丁目标测试未通过 | completed | 1／0 | 递归清理已处理敏感字段，但把tuple统一转换为list；正式test_cleanse_setting_recurses_in_list_tuples比较嵌套容器类型而失败。 保留list/tuple的原始容器类型，增加嵌套组合与敏感字段检查。 |
| `django__django-13660` | 补丁回归测试未通过 | completed | 0／2 | 2个F2P通过，但2个P2P失败：shell -c和stdin执行后的globals检查输出False而要求True。补丁使用exec(..., {})使原有全局上下文行为丢失。 在共享执行命名空间中兼顾新导入可见性和shell原有globals，重验两类测试。 |
| `django__django-14017` | 疑似thinking输出截断且无补丁 | completed | 未判分 | 原result标completed且text为空；最后assistant仅含thinking，无text/tool_use；末次响应输出token增量恰为4096，与max_output_tokens相同，随后run_stopped。runner收集到空补丁。强烈提示模型输出截断被当成正常完成，但未保存finish_reason，不能仅凭日志断言内部实现原因。 保存并检查finish_reason/stop_reason；对仅thinking或length结束继续推理/执行，完成前检查实际补丁是否存在。 |
| `django__django-14155` | 补丁目标测试未通过 | completed | 3／0 | partial视图处理已有修改，但ResolverMatch repr的url_name/route字符串未带repr引号，正式test_repr等F2P不符；3个报告目标失败。 其中test_resolver_match_on_request在原始输出实际为ok且与前一测试拼接，存在解析错配的混合证据；其余真实断言失败足以判整题未解。 统一repr中字符串参数的转义/引号形式，兼顾普通和partial视图。 |
| `django__django-14534` | 补丁目标测试未通过 | limit | 1／0 | 达到60轮上限后提交472字节补丁；2个F2P中1个通过，另一个在BoundWidget.id_for_label直接索引attrs['id']触发KeyError。 对无id的widget安全返回预期值；把这个失败用例纳入最小验证。 |
| `django__django-14667` | 补丁同时未修复目标且破坏回归 | completed | 1／1 | defer/only链的字段加载语义仍不正确：test_defer_only_chaining触发3次查询而要求1，P2P test_defer_only_clear为3而要求0。F2P和回归均有失败。 重建only/defer/clear组合的状态转换与SQL列选择，验证查询次数。 |
| `django__django-14997` | 达到轮数上限且无补丁 | limit | 未判分 | 达到60个lead model turns上限，最终未留下可收集的工作树补丁，grading未运行。日志显示主要在阅读、复现和方案探索；仅增加轮数能否成功尚无证据。 在前段轮次完成最小补丁并测试，设置剩余轮次提示/检查点；减少重复阅读和低收益探索。 |
| `django__django-15213` | 达到轮数上限且无补丁 | limit | 未判分 | 达到60个lead model turns上限，最终未留下可收集的工作树补丁，grading未运行。日志显示主要在阅读、复现和方案探索；仅增加轮数能否成功尚无证据。 在前段轮次完成最小补丁并测试，设置剩余轮次提示/检查点；减少重复阅读和低收益探索。 |
| `django__django-15252` | 达到轮数上限且无补丁 | limit | 未判分 | 达到60个lead model turns上限，最终未留下可收集的工作树补丁，grading未运行。日志显示主要在阅读、复现和方案探索；仅增加轮数能否成功尚无证据。 在前段轮次完成最小补丁并测试，设置剩余轮次提示/检查点；减少重复阅读和低收益探索。 |
| `django__django-15388` | 疑似thinking输出截断且无补丁 | completed | 未判分 | 原result标completed且text为空；最后assistant仅含thinking，无text/tool_use；末次响应输出token增量恰为4096，与max_output_tokens相同，随后run_stopped。runner收集到空补丁。强烈提示模型输出截断被当成正常完成，但未保存finish_reason，不能仅凭日志断言内部实现原因。 保存并检查finish_reason/stop_reason；对仅thinking或length结束继续推理/执行，完成前检查实际补丁是否存在。 |
| `django__django-15400` | 疑似thinking输出截断且无补丁 | completed | 未判分 | 原result标completed且text为空；最后assistant仅含thinking，无text/tool_use；末次响应输出token增量恰为4096，与max_output_tokens相同，随后run_stopped。runner收集到空补丁。强烈提示模型输出截断被当成正常完成，但未保存finish_reason，不能仅凭日志断言内部实现原因。 保存并检查finish_reason/stop_reason；对仅thinking或length结束继续推理/执行，完成前检查实际补丁是否存在。 |
| `django__django-15695` | 疑似thinking输出截断且无补丁 | completed | 未判分 | 原result标completed且text为空；最后assistant仅含thinking，无text/tool_use；末次响应输出token增量恰为4096，与max_output_tokens相同，随后run_stopped。runner收集到空补丁。强烈提示模型输出截断被当成正常完成，但未保存finish_reason，不能仅凭日志断言内部实现原因。 保存并检查finish_reason/stop_reason；对仅thinking或length结束继续推理/执行，完成前检查实际补丁是否存在。 |
| `django__django-15996` | 疑似thinking输出截断且无补丁 | completed | 未判分 | 原result标completed且text为空；最后assistant仅含thinking，无text/tool_use；末次响应输出token增量恰为4096，与max_output_tokens相同，随后run_stopped。runner收集到空补丁。强烈提示模型输出截断被当成正常完成，但未保存finish_reason，不能仅凭日志断言内部实现原因。 保存并检查finish_reason/stop_reason；对仅thinking或length结束继续推理/执行，完成前检查实际补丁是否存在。 |
| `django__django-16408` | 达到轮数上限且无补丁 | limit | 未判分 | 达到60个lead model turns上限，最终未留下可收集的工作树补丁，grading未运行。日志显示主要在阅读、复现和方案探索；仅增加轮数能否成功尚无证据。 在前段轮次完成最小补丁并测试，设置剩余轮次提示/检查点；减少重复阅读和低收益探索。 |
| `django__django-16816` | 疑似thinking输出截断且无补丁 | completed | 未判分 | 原result标completed且text为空；最后assistant仅含thinking，无text/tool_use；末次响应输出token增量恰为4096，与max_output_tokens相同，随后run_stopped。runner收集到空补丁。强烈提示模型输出截断被当成正常完成，但未保存finish_reason，不能仅凭日志断言内部实现原因。 保存并检查finish_reason/stop_reason；对仅thinking或length结束继续推理/执行，完成前检查实际补丁是否存在。 |
| `django__django-16820` | 达到轮数上限且无补丁 | limit | 未判分 | 达到60个lead model turns上限，最终未留下可收集的工作树补丁，grading未运行。日志显示主要在阅读、复现和方案探索；仅增加轮数能否成功尚无证据。 在前段轮次完成最小补丁并测试，设置剩余轮次提示/检查点；减少重复阅读和低收益探索。 |
| `django__django-16910` | API流式连接中断 | error | 未判分 | 模型API流式响应断开（incomplete chunked read），agent异常终止且最终补丁为空，未运行grading。日志只能证明这次运行受外部连接故障中断，无法据此判断其本可解题。 对流式读失败做可恢复重试/断点续跑，再在相同参数下单独重测。 |
| `matplotlib__matplotlib-22711` | 补丁目标测试未通过 | completed | 2／0 | 正式RangeSlider两个方向均失败：val为(0.2,0.6)时手柄位置为(0.1,0.34)，预期(0.2,0.6)。末轮还出现仅thinking且4096输出上限特征，补丁未完成验证。 该运行末轮仅thinking且用满4096输出token，存在结束识别异常的额外证据。 修正手柄坐标更新逻辑并用两个方向的正式目标验收；同时修正响应截断识别。 |
| `matplotlib__matplotlib-22835` | 补丁目标测试未通过 | completed | 1／0 | BoundaryNorm不再崩溃，但format_cursor_data精度格式仍不符：正式测试要求[-1.0]，补丁给出[-1.]。 按边界间距确定有效位数并覆盖边界内外数值格式。 |
| `matplotlib__matplotlib-23299` | 补丁目标测试未通过 | limit | 1／0 | 60轮上限后，目标test_no_backend_reset_rccontext仍失败：上下文内backend设为module://aardvark后返回agg。原始输出另有非计分警告失败，不能替代该目标失败证据。 修正rc_context退出时backend恢复策略，并以目标backend测试确认。 |
| `matplotlib__matplotlib-23563` | 疑似thinking输出截断且无补丁 | completed | 未判分 | 原result标completed且text为空；最后assistant仅含thinking，无text/tool_use；末次响应输出token增量恰为4096，与max_output_tokens相同，随后run_stopped。runner收集到空补丁。强烈提示模型输出截断被当成正常完成，但未保存finish_reason，不能仅凭日志断言内部实现原因。 保存并检查finish_reason/stop_reason；对仅thinking或length结束继续推理/执行，完成前检查实际补丁是否存在。 |
| `matplotlib__matplotlib-23913` | gold验证失败，Agent未启动 | 未启动 | 未判分 | gold目标修复测试全部通过，但既有test_warn_big_data_best_loc期待UserWarning而实际未发出，P2P失败；属于本次测试警告行为/运行条件不满足。测试与大数据legend性能有关，疑计时或机器性能敏感，日志不足以断言具体阈值或gold代码错误。 先修复/固定参考补丁可通过的评测环境，再单独重测；保留原始300题结果。 |
| `matplotlib__matplotlib-25332` | API流式连接中断 | error | 未判分 | 模型API流式响应断开（incomplete chunked read），agent异常终止且最终补丁为空，未运行grading。日志只能证明这次运行受外部连接故障中断，无法据此判断其本可解题。 对流式读失败做可恢复重试/断点续跑，再在相同参数下单独重测。 |
| `mwaskom__seaborn-2848` | 达到轮数上限且无补丁 | limit | 未判分 | 达到60个lead model turns上限，最终未留下可收集的工作树补丁，grading未运行。日志显示主要在阅读、复现和方案探索；仅增加轮数能否成功尚无证据。 在前段轮次完成最小补丁并测试，设置剩余轮次提示/检查点；减少重复阅读和低收益探索。 |
| `mwaskom__seaborn-3407` | 补丁目标测试未通过 | completed | 1／0 | MultiIndex列名pairplot目标仍失败：g.diag_vars与list(cols)比较触发NumPy数组truth-value ambiguous异常，说明列标签表示/类型处理不完整。 保持MultiIndex元组标签与普通列表的稳定表示，再验证pairplot对角变量。 |
| `pallets__flask-4045` | 补丁目标测试未通过 | completed | 1／0 | 2个F2P中1个通过；带点endpoint的Blueprint.route仍抛AssertionError，正式测试要求ValueError。原始输出还包含Werkzeug弃用兼容错误，但该计分目标有明确产品行为差异。 同时处理blueprint名称和endpoint名称的校验异常类型。 |
| `psf__requests-2148` | 疑似thinking输出截断且无补丁 | completed | 未判分 | 原result标completed且text为空；最后assistant仅含thinking，无text/tool_use；末次响应输出token增量恰为4096，与max_output_tokens相同，随后run_stopped。runner收集到空补丁。强烈提示模型输出截断被当成正常完成，但未保存finish_reason，不能仅凭日志断言内部实现原因。 保存并检查finish_reason/stop_reason；对仅thinking或length结束继续推理/执行，完成前检查实际补丁是否存在。 |
| `psf__requests-2317` | gold验证失败，Agent未启动 | 未启动 | 未判分 | gold目标修复测试全部通过；两条P2P连接超时测试使用TARPIT地址10.255.255.1，期望连接阶段超时，实际一次未抛超时、一次抛ReadTimeout。当前网络路由/连接行为不满足旧测试假设，不能据此判定gold算法错误。 先修复/固定参考补丁可通过的评测环境，再单独重测；保留原始300题结果。 |
| `psf__requests-2674` | gold验证失败，Agent未启动 | 未启动 | 未判分 | gold目标修复测试全部通过；两条P2P连接超时测试使用TARPIT地址10.255.255.1，期望连接阶段超时，实际一次未抛超时、一次抛ReadTimeout。当前网络路由/连接行为不满足旧测试假设，不能据此判定gold算法错误。 先修复/固定参考补丁可通过的评测环境，再单独重测；保留原始300题结果。 |
| `psf__requests-3362` | gold验证失败，Agent未启动 | 未启动 | 未判分 | gold目标修复测试全部通过；两条P2P连接超时测试使用TARPIT地址10.255.255.1，期望连接阶段超时，实际一次未抛超时、一次抛ReadTimeout。当前网络路由/连接行为不满足旧测试假设，不能据此判定gold算法错误。 先修复/固定参考补丁可通过的评测环境，再单独重测；保留原始300题结果。 |
| `psf__requests-863` | gold验证失败，Agent未启动 | 未启动 | 未判分 | gold目标修复测试全部通过；P2P test_connection_keepalive_and_close期望httpbin响应Connection为keep-alive，实际为close。外部服务或网络连接行为与旧测试假设不符；不能据此认定gold修复代码错误。 先修复/固定参考补丁可通过的评测环境，再单独重测；保留原始300题结果。 |
| `pydata__xarray-4493` | 补丁目标测试未通过 | completed | 1／0 | DataArray惰性数据处理有补丁，但正式test_as_variable要求的DeprecationWarning未发出，F2P失败。原始输出中dask/pandas兼容失败不属于报告列出的计分失败。 在避免DataArray提前计算的同时保留as_variable的警告契约；核查正式测试覆盖的数据入口。 |
| `pylint-dev__pylint-7080` | 达到轮数上限且无补丁 | limit | 未判分 | 达到60个lead model turns上限，最终未留下可收集的工作树补丁，grading未运行。日志显示主要在阅读、复现和方案探索；仅增加轮数能否成功尚无证据。 在前段轮次完成最小补丁并测试，设置剩余轮次提示/检查点；减少重复阅读和低收益探索。 |
| `pylint-dev__pylint-7114` | 补丁回归测试未通过 | limit | 0／5 | 目标F2P通过，但5个P2P的fix_import_path失败：目录参数路径被退到父目录，例如/tmp/...替代/tmp/.../a。60轮后留下回归补丁。 区分包目录、普通目录和文件入口的python path查找规则，先修复5个回归用例。 |
| `pytest-dev__pytest-11148` | 疑似thinking输出截断且无补丁 | completed | 未判分 | 原result标completed且text为空；最后assistant仅含thinking，无text/tool_use；末次响应输出token增量恰为4096，与max_output_tokens相同，随后run_stopped。runner收集到空补丁。强烈提示模型输出截断被当成正常完成，但未保存finish_reason，不能仅凭日志断言内部实现原因。 保存并检查finish_reason/stop_reason；对仅thinking或length结束继续推理/执行，完成前检查实际补丁是否存在。 |
| `pytest-dev__pytest-5103` | 达到轮数上限且无补丁 | limit | 未判分 | 达到60个lead model turns上限，最终未留下可收集的工作树补丁，grading未运行。日志显示主要在阅读、复现和方案探索；仅增加轮数能否成功尚无证据。 在前段轮次完成最小补丁并测试，设置剩余轮次提示/检查点；减少重复阅读和低收益探索。 |
| `pytest-dev__pytest-5413` | 补丁目标测试未通过 | completed | 1／0 | ExceptionInfo.__str__被改为exconly()，但正式test_excinfo_repr_str要求<ExceptionInfo ValueError tblen=4>而实际ValueError。需求解释与基准验收选择不一致。 按实际验收契约调整ExceptionInfo字符串表示，避免把issue建议当作最终规格。 |
| `pytest-dev__pytest-7168` | 补丁目标测试未通过 | completed | 1／0 | 11个F2P中10个通过；broken __getattribute__情况下saferepr输出类名<unknown>，测试仍要求SomeClass。 用不触发实例自定义属性访问的方式获取类名，保留异常安全性。 |
| `pytest-dev__pytest-7220` | 达到轮数上限且无补丁 | limit | 未判分 | 达到60个lead model turns上限，最终未留下可收集的工作树补丁，grading未运行。日志显示主要在阅读、复现和方案探索；仅增加轮数能否成功尚无证据。 在前段轮次完成最小补丁并测试，设置剩余轮次提示/检查点；减少重复阅读和低收益探索。 |
| `scikit-learn__scikit-learn-10949` | 补丁目标测试未通过 | completed | 1／0 | DataFrame dtype转换警告已发出，但文本为Data with input dtype object was converted to float64.，正式要求were all converted to float64.；目标F2P因此失败。原始输出的pytest.raises(message=...)兼容错误不能解释这个计分目标。 补齐DataFrame多列转换警告的具体文本和混合dtype处理。 |
| `scikit-learn__scikit-learn-11040` | 补丁目标测试未通过 | completed | 1／0 | n_neighbors负数的校验时机不符：正式测试要求Expected n_neighbors > 0. Got -3，却先抛NotFittedError。 把参数有效性检查放在符合API契约的执行顺序，覆盖未fit调用。 |
| `sphinx-doc__sphinx-10451` | 补丁回归测试未通过 | limit | 0／1 | 2个F2P均通过，但P2P autodoc documented __init__输出中*args/**kwargs的星号和加粗表示不符；已达60轮上限。 统一已写文档参数和类型提示参数的匹配，保留可变参数星号。 |
| `sphinx-doc__sphinx-7686` | 疑似thinking输出截断且无补丁 | completed | 未判分 | 原result标completed且text为空；最后assistant仅含thinking，无text/tool_use；末次响应输出token增量恰为4096，与max_output_tokens相同，随后run_stopped。runner收集到空补丁。强烈提示模型输出截断被当成正常完成，但未保存finish_reason，不能仅凭日志断言内部实现原因。 保存并检查finish_reason/stop_reason；对仅thinking或length结束继续推理/执行，完成前检查实际补丁是否存在。 |
| `sphinx-doc__sphinx-8273` | 补丁目标测试未通过 | completed | 1／0 | man_make_section_directory已实现，但补丁把页面放入man1/python.1；正式测试要求1/python.1，目录命名规格不一致。 使用正式预期的数字节目录布局，并核对配置说明。 |
| `sphinx-doc__sphinx-8282` | 补丁目标测试未通过 | completed | 1／0 | 关闭typehints后重载签名展示仍不符：正式输出在Bar(x,y)处出现额外行，要求对应module行；test_autodoc_typehints_none_for_overload失败。 同时处理重载函数、方法和类签名，确认关闭类型提示后的行数与签名选择。 |
| `sphinx-doc__sphinx-8474` | 达到轮数上限且无补丁 | limit | 未判分 | 达到60个lead model turns上限，最终未留下可收集的工作树补丁，grading未运行。日志显示主要在阅读、复现和方案探索；仅增加轮数能否成功尚无证据。 在前段轮次完成最小补丁并测试，设置剩余轮次提示/检查点；减少重复阅读和低收益探索。 |
| `sphinx-doc__sphinx-8506` | 补丁目标测试未通过 | completed | 1／0 | 带[开头的cmd option被当成整体名称[enable=]PATTERN，测试要求name为[enable并分离参数；末轮仅thinking且4096上限特征。 该运行末轮仅thinking且用满4096输出token，存在结束识别异常的额外证据。 修正选项名称/参数拆分规则，覆盖括号形式并处理响应截断。 |
| `sympy__sympy-11400` | 补丁目标测试未通过 | completed | 2／0 | ccode sinc与Relational两个目标均失败；补丁仅增加sinc的Piecewise改写，正式关系式与sinc代码输出仍不满足验收。最终示例中出现Ne(theta,0)而不是C关系运算表达。 完善关系运算打印与Piecewise条件输出，按目标字符串验证。 |
| `sympy__sympy-11870` | 达到轮数上限且无补丁 | limit | 未判分 | 达到60个lead model turns上限，最终未留下可收集的工作树补丁，grading未运行。日志显示主要在阅读、复现和方案探索；仅增加轮数能否成功尚无证据。 在前段轮次完成最小补丁并测试，设置剩余轮次提示/检查点；减少重复阅读和低收益探索。 |
| `sympy__sympy-11897` | 达到轮数上限且无补丁 | limit | 未判分 | 达到60个lead model turns上限，最终未留下可收集的工作树补丁，grading未运行。日志显示主要在阅读、复现和方案探索；仅增加轮数能否成功尚无证据。 在前段轮次完成最小补丁并测试，设置剩余轮次提示/检查点；减少重复阅读和低收益探索。 |
| `sympy__sympy-12171` | 补丁同时未修复目标且破坏回归 | completed | 1／1 | Mathematica Derivative目标要求Hold[D[Sin[x], x]]，补丁仅输出D[...]；另有P2P test_Pow失败。末轮仅thinking且4096上限特征。 该运行末轮仅thinking且用满4096输出token，存在结束识别异常的额外证据。 补齐Hold与浮点/幂打印语义，验证F2P和P2P后再结束。 |
| `sympy__sympy-12236` | 达到轮数上限且无补丁 | limit | 未判分 | 达到60个lead model turns上限，最终未留下可收集的工作树补丁，grading未运行。日志显示主要在阅读、复现和方案探索；仅增加轮数能否成功尚无证据。 在前段轮次完成最小补丁并测试，设置剩余轮次提示/检查点；减少重复阅读和低收益探索。 |
| `sympy__sympy-13043` | 补丁目标测试未通过 | completed | 1／0 | decompose(separate=True)选择返回排序list，但正式test_decompose要求set，最简单decompose(x,True)=={x}即失败。issue允许多种方向，agent选择与实际验收不一致。 按正式容器契约返回无序集合，并检查全部分支的一致性。 |
| `sympy__sympy-13177` | 补丁目标测试未通过 | limit | 1／0 | Mod修复条件过窄：加入base.is_integer限制后，正式test_mod里的Mod(l**p,l)==0仍失败；60轮上限。 按符号假设、幂指数和模数关系细化化简规则。 |
| `sympy__sympy-13437` | 补丁目标测试未通过 | completed | 1／0 | bell(oo)直接返回oo的处理扩大到多参数形式；正式测试要求bell(oo,x)抛ValueError，而实际未抛。 把无限指标的特例限定到合法参数形式，保留Bell多项式异常契约。 |
| `sympy__sympy-13773` | 补丁目标测试未通过 | limit | 1／0 | 60轮后Matrix @标量路径抛ValueError，但正式test_matmul要求直接调用a.__matmul__(2)返回NotImplemented，以便Python执行运算分派。 在不支持的操作数类型上返回NotImplemented，区分直接魔术方法与最终运算异常。 |
| `sympy__sympy-13895` | 达到轮数上限且无补丁 | limit | 未判分 | 达到60个lead model turns上限，最终未留下可收集的工作树补丁，grading未运行。日志显示主要在阅读、复现和方案探索；仅增加轮数能否成功尚无证据。 在前段轮次完成最小补丁并测试，设置剩余轮次提示/检查点；减少重复阅读和低收益探索。 |
| `sympy__sympy-14308` | 达到轮数上限且无补丁 | limit | 未判分 | 达到60个lead model turns上限，最终未留下可收集的工作树补丁，grading未运行。日志显示主要在阅读、复现和方案探索；仅增加轮数能否成功尚无证据。 在前段轮次完成最小补丁并测试，设置剩余轮次提示/检查点；减少重复阅读和低收益探索。 |
| `sympy__sympy-14317` | 疑似thinking输出截断且无补丁 | completed | 未判分 | 原result标completed且text为空；最后assistant仅含thinking，无text/tool_use；末次响应输出token增量恰为4096，与max_output_tokens相同，随后run_stopped。runner收集到空补丁。强烈提示模型输出截断被当成正常完成，但未保存finish_reason，不能仅凭日志断言内部实现原因。 保存并检查finish_reason/stop_reason；对仅thinking或length结束继续推理/执行，完成前检查实际补丁是否存在。 |
| `sympy__sympy-14396` | 疑似thinking输出截断且无补丁 | completed | 未判分 | 原result标completed且text为空；最后assistant仅含thinking，无text/tool_use；末次响应输出token增量恰为4096，与max_output_tokens相同，随后run_stopped。runner收集到空补丁。强烈提示模型输出截断被当成正常完成，但未保存finish_reason，不能仅凭日志断言内部实现原因。 保存并检查finish_reason/stop_reason；对仅thinking或length结束继续推理/执行，完成前检查实际补丁是否存在。 |
| `sympy__sympy-15308` | 补丁目标测试未通过 | completed | 1／0 | Trace的LaTeX功能增加后，正式test_trace要求\mathrm{tr}\left (A \right )等格式，补丁使用\operatorname{tr}\left(...)，字符串格式不符。 对齐Trace打印的运算符、括号和空格契约。 |
| `sympy__sympy-16503` | 疑似thinking输出截断且无补丁 | completed | 未判分 | 原result标completed且text为空；最后assistant仅含thinking，无text/tool_use；末次响应输出token增量恰为4096，与max_output_tokens相同，随后run_stopped。runner收集到空补丁。强烈提示模型输出截断被当成正常完成，但未保存finish_reason，不能仅凭日志断言内部实现原因。 保存并检查finish_reason/stop_reason；对仅thinking或length结束继续推理/执行，完成前检查实际补丁是否存在。 |
| `sympy__sympy-16988` | 达到轮数上限且无补丁 | limit | 未判分 | 达到60个lead model turns上限，最终未留下可收集的工作树补丁，grading未运行。日志显示主要在阅读、复现和方案探索；仅增加轮数能否成功尚无证据。 在前段轮次完成最小补丁并测试，设置剩余轮次提示/检查点；减少重复阅读和低收益探索。 |
| `sympy__sympy-17022` | 达到轮数上限且无补丁 | limit | 未判分 | 达到60个lead model turns上限，最终未留下可收集的工作树补丁，grading未运行。日志显示主要在阅读、复现和方案探索；仅增加轮数能否成功尚无证据。 在前段轮次完成最小补丁并测试，设置剩余轮次提示/检查点；减少重复阅读和低收益探索。 |
| `sympy__sympy-17630` | 补丁目标测试未通过 | limit | 1／0 | 两个F2P中1个通过；Add(ZeroMatrix(2,2),ZeroMatrix(2,2))仍未化简为ZeroMatrix；60轮上限。 覆盖Add与MatAdd两类入口及零矩阵维度合并。 |
| `sympy__sympy-17655` | 达到轮数上限且无补丁 | limit | 未判分 | 达到60个lead model turns上限，最终未留下可收集的工作树补丁，grading未运行。日志显示主要在阅读、复现和方案探索；仅增加轮数能否成功尚无证据。 在前段轮次完成最小补丁并测试，设置剩余轮次提示/检查点；减少重复阅读和低收益探索。 |
| `sympy__sympy-18057` | 补丁目标测试未通过 | limit | 1／0 | 60轮后正式test_var仍失败，返回对象与字符串列表比较v != [d,e,fg]的语义不符；目标断言有实际失败证据。 明确var的返回类型及符号与字符串比较行为，验证目标断言。 |
| `sympy__sympy-18199` | 补丁目标测试未通过 | completed | 1／0 | 零模数根情况有局部修复，但正式test_solve_modular在Mod(x**3,8)-1的整数解集合仍失败，说明复合模数/求解器路径覆盖不足。 覆盖素数幂和复合模数的nthroot_mod到solveset路径。 |
| `sympy__sympy-20639` | 补丁目标测试未通过 | completed | 2／0 | 根式pretty修复后，test_issue_6285和test_issue_17616两个正式字符串断言仍失败，既有1/pi符号根又有1/E场景不满足预期。 以ASCII/Unicode根式基线分别验证指数分母提取与排版。 |
| `sympy__sympy-21171` | 补丁目标测试未通过 | completed | 1／0 | SingularityFunction外层幂已支持，但正式latex(SingularityFunction(x,4,5)**3)字符串仍不符，属于目标LaTeX输出契约未对齐。 对齐外层括号、幂和分组格式，覆盖正负指数。 |
| `sympy__sympy-21612` | 达到轮数上限且无补丁 | limit | 未判分 | 达到60个lead model turns上限，最终未留下可收集的工作树补丁，grading未运行。日志显示主要在阅读、复现和方案探索；仅增加轮数能否成功尚无证据。 在前段轮次完成最小补丁并测试，设置剩余轮次提示/检查点；减少重复阅读和低收益探索。 |
| `sympy__sympy-22005` | 疑似thinking输出截断且无补丁 | completed | 未判分 | 原result标completed且text为空；最后assistant仅含thinking，无text/tool_use；末次响应输出token增量恰为4096，与max_output_tokens相同，随后run_stopped。runner收集到空补丁。强烈提示模型输出截断被当成正常完成，但未保存finish_reason，不能仅凭日志断言内部实现原因。 保存并检查finish_reason/stop_reason；对仅thinking或length结束继续推理/执行，完成前检查实际补丁是否存在。 |
| `sympy__sympy-22840` | 补丁目标测试未通过 | completed | 1／0 | 2个F2P中1个通过；CSE避免提取MatrixSymbol后，test_multidim_c_argument_cse生成的C代码仍与expected不同。 验证CSE替换顺序、标量临时变量和多维数组参数的最终代码。 |
| `sympy__sympy-23191` | 补丁同时未修复目标且破坏回归 | limit | 1／1 | 向量pretty排版的F2P test_issue_23058与P2P test_pretty_print_unicode_v同时失败；60轮后补丁仍有布局回归。 将新增复杂向量和原有Unicode向量版式一起作为验收。 |
| `sympy__sympy-24102` | 补丁目标测试未通过 | completed | 1／0 | Unicode标识符相关2个F2P中1个通过，但test_mathematica的既有表达式映射仍失败；只扩大token正则没有覆盖所有解析语义。 定位test_mathematica中首个不符表达式，保持旧语法和Unicode标识符同时正确。 |
| `sympy__sympy-24909` | 补丁目标测试未通过 | completed | 1／0 | Prefix与Quantity的乘除有修改，但正式test_prefix_operations要求milli*kilo is S.One，补丁破坏/未保持此前缀乘法单位元契约。 保持Prefix×Prefix的规范化和对象单例结果，再单独处理Quantity运算。 |

## 文件与复核方式

| 文件 | 用途 |
| --- | --- |
| [analysis.xlsx](analysis.xlsx) | 总览、300题逐题分析与326次尝试历史 |
| [完整分析 ZIP](swebench_lite_20261020_full.zip) | 完整报告、离线dashboard、CSV/JSON、专项核查、分析与验证脚本及预览 |
| [SHA256SUMS](SHA256SUMS) | 本页、Excel和完整分析ZIP的SHA-256 |

包内报告保留完整材料之间的相对引用；本页将归档材料入口统一指向ZIP，分析结论不变。ZIP中的 `ARCHIVE_MANIFEST.json` 为各成员提供字节数和SHA-256；`ARCHIVE_README.md` 说明目录结构和复核方法。

解压后可直接查看 `analysis/swebench_lite_20261020/dashboard.html`，无需联网。Python分析脚本使用标准库，需要Python 3.11+；仅从包内已有JSON重建报告不需要原始日志。重建归一数据时，需另外提供本地原始归档，并在同一项目根目录恢复 `analysis/swebench_lite_20261020/` 和 `task_output-wsl/lite-baseline-001/`。原始测试数据不随GitHub仓库或本公开分析ZIP分发。

Excel生成脚本另需 `@oai/artifact-tool`，本机依赖目录联接没有打包。Windows解压、校验及归档位置见 [分析索引](../README.md)。
