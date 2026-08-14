# summarize_user

PagerMaid-Pyro 用户画像分析插件

### 功能特点：
抓取指定用户在群组或频道的发言，调用 OpenAI 兼容 API 生成精准的用户画像分析报告。
支持大批量消息 Map-Reduce 分块处理（支持 2000+ 条消息）和远程群组定向分析（零痕迹）。

### 常见用法：
- **链接分析**（最简单，支持私密群组及无用户名用户）：`,summarize_user -l <消息链接> [数量]`
- **远程无痕分析**：`,summarize_user -g <群组ID/用户名> [数量] <目标用户>`
- **当前群内分析**：`,summarize_user [数量] [目标用户]` 或直接回复对方发送 `,summarize_user [数量]`

### 进阶配置：
- `,summarize_user setapi <API_KEY>` — 设置 API 密钥
- `,summarize_user seturl <BASE_URL>` — 设置自定义 API 地址
- `,summarize_user setmodel <MODEL>` — 设置模型名称
- `,summarize_user setdisplay <NAME>` — 设置输出显示的模型名称
- `,summarize_user setprompt <PROMPT>` — 自定义系统提示词
- `,summarize_user showconfig` — 查看当前配置

### 指令说明：
详细指令请在 Telegram 内使用 `,help summarize_user` 查看。
