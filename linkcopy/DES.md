# linkcopy

将 Telegram 消息链接替换成原文或媒体。使用运行 PagerMaid 的账号读取来源，无需额外机器人、API 密钥或依赖。

## 使用

在想发送内容的聊天中输入：

```text
,linkcopy https://t.me/example/123
,linkcopy https://t.me/c/1234567890/123
```

也可以回复一条含链接的消息发送 `,linkcopy`，支持隐藏超链接；被回复消息必须恰好包含一个有效的消息链接。仅回复取链接时，该消息会保留，插件只替换或删除自己的命令消息。

- 文字：直接将命令消息编辑成原文，保留粗体、超链接、自定义表情等 Telegram 文本实体。
- 媒体：复制图片、视频、文件、音频、语音、动图、贴纸、视频圆消息；发送成功后删除命令消息。
- 相册：默认复制链接所在的整个相册，保持顺序和每项配文。添加 `--single` 或使用带 `?single` 的链接，只复制选中的一项。
- 位置：在当前聊天、当前话题发送；显式提供链接时，保留命令的回复关系。

```text
,linkcopy https://t.me/example/123 --single
```

支持公开链接、`t.me/c/...` 私有链接、含话题 ID 的链接、`t.me/s/...` 预览链接，以及 `tg://resolve?...&post=...`、`tg://privatepost?...&post=...`。`?comment=...` 会读取对应讨论组的评论。

## 内容保护与失败处理

所有媒体（包括带内容保护标记的消息）均先尝试复用 Telegram 文件引用，成功时无需下载上传。只有收到明确的内容保护或文件引用错误，才下载到服务器临时目录后重新上传。相册也先尝试整组复用；需要重传时，先下载完所有项目，再整组发送。

下载和重传消耗服务器磁盘、流量和时间，大文件会较慢；成功、失败或任务取消后均清理临时目录。失败时保留命令并显示原因。网络超时不会自动重发，避免重复发送；请先检查目标聊天后再决定是否重试。

权限不足、网络超时或没有发送结果时，不会改用下载重传；重传再次被拒绝时也不会循环尝试。

需要当前账号能够读取来源；私有频道或群组通常需要先加入。插件不会自动加入聊天，也无法读取已删除、不可访问的消息。Telegram 服务端限制仍可能导致下载或重传失败，不能保证所有受保护内容都成功。

服务消息、投票、位置、联系人、付费媒体等暂不支持；不复制消息按钮、转发来源、原消息的浏览量或互动数据。普通粘贴链接不会触发，需使用 `,linkcopy`。只接受当前账号自己的命令，不开放给 sudo 用户。

已在 PagerMaid-Pyro 1.5.13 与其配套的 TeamPGM Pyrogram 上完成离线验证；Telegram 账号在线发送仍需实际验证。

## 现有实现调研

- [PagerMaid 官方 Pyro 插件仓库](https://github.com/TeamPGM/PagerMaid_Plugins_Pyro/tree/v2)：本次检索未找到与此交互相同的现成插件。
- [`shift` 源码](https://github.com/TeamPGM/PagerMaid_Plugins_Pyro/blob/cc82a684c63381abfa87738621e9515865f3c64a/shift/main.py)：用于持续转发或批量备份；检查并拒绝受保护来源，不适合按链接提取。
- [Pyrogram 的消息复制实现](https://github.com/TeamPGM/pyrogram/blob/1.5.13/pyrogram/types/messages_and_media/message.py)：已有按类型重发文字或复用媒体文件的基础；本插件独立实现链接解析、文字替换、保留相册实体和下载重传流程，未复制第三方插件代码。
- [Telegram 消息链接格式](https://core.telegram.org/api/links#message-links)。
