# PagerMaid-Pyro Plugins

> PagerMaid-Pyro 自用插件合集。

## 📦 插件列表

| 插件名称 | 简述 | 说明 |
| :--- | :--- | :--- |
| `autodelplus` | 自动清理消息 | 设置当前聊天或全局的消息自动删除定时，支持将特定群组排除在全局规则外，以及远程无痕管理其他聊天的定时任务。 |
| `summarize_user` | 用户画像分析 | 抓取指定用户发言，调用 OpenAI 兼容 API 生成精准的用户画像分析报告，支持跨群远程零痕迹分析。 |
| `conflict_analyzer` | 群聊冲突分析 | 以消息为锚点还原多人争执，分析起因、升级过程、各方依据和沟通责任。 |

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
```

## 📝 许可证

本项目插件遵循 PagerMaid-Pyro 的开源规范，自由使用。
