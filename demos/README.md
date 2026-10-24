# 终端演示用例

这里保存 README 的演示动图、完整终端录屏，以及录制时使用的项目模板、任务提示词和操作步骤。

[查看 GIF 动图](demo.gif) · [▶ 观看完整演示（MP4，约 1 分 14 秒）](demo.mp4) · [静态截图](demo-preview.jpg)

| 用例 | 演示内容 | 准备材料 |
| --- | --- | --- |
| Markdown 链接检查器 | 复现误报、读取代码、修复实现、运行测试、展示 diff | [录制指南](markdown-link-checker/README.md) · [任务提示词](markdown-link-checker/task.txt) |

每次 `Prepare` 都从带缺陷的 `starter/` 创建一个新的独立 Git 工作区，放在已忽略的 `.task_outputs/demos/`。实际录制时由 Aki-Code 自主修改该副本，模板保留原始缺陷，便于重录。演示测试故意包含失败用例，其初始失败结果不代表 Aki-Code 主工程回归失败。
