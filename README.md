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
