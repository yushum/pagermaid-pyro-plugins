# PagerMaid-Pyro Plugins

> PagerMaid-Pyro 自用插件合集。

## 📦 插件列表

| 插件名称 | 简述 | 说明 |
| :--- | :--- | :--- |
| `autodelplus` | 自动清理消息 | 设置当前聊天或全局的消息自动删除定时，支持排除特定聊天，并在当前对话管理其他聊天的规则。 |
| `summarize_user` | 用户画像分析 | 读取指定用户的发言，通过 OpenAI 兼容 API 分析话题和表达特征；支持读取其他群组，在当前对话显示结果。 |
| `conflict_analyzer` | 群聊冲突分析 | 围绕指定消息分析多人争执，支持追踪跨天回复、读取引用消息附近的上下文，并补充搜索参与者发言。 |
| `linkcopy` | 按链接复制消息 | 按单个链接或链接区间复制文字和媒体，支持相册、视频封面、文件传输进度和停止任务；复制成功后自动删除进度消息。详见 [使用说明](linkcopy/DES.md)。 |

## 🚀 如何安装

1. 首先，在 Telegram 的任意聊天中向你的 PagerMaid-Pyro 发送以下命令，将本仓库添加为第三方源：

```text
,apt_source add https://raw.githubusercontent.com/yushum/pagermaid-pyro-plugins/master
```

2. 添加源后，使用以下命令安装对应的插件：

```text
,apt install autodelplus
,apt install summarize_user
,apt install conflict_analyzer
,apt install linkcopy
```

## 📝 许可证

本项目插件遵循 PagerMaid-Pyro 的开源规范，自由使用。
