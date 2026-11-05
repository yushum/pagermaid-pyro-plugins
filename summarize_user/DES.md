# summarize_user

PagerMaid-Pyro 用户画像分析插件

### 功能特点：
读取指定用户在群组或频道中的发言，调用 OpenAI 兼容 API 分析话题、表达方式和活跃规律。

支持 Map-Reduce 分段分析；单次最多读取 5000 条消息，实际数量受账号读取权限、可用记录和模型输入限制影响。远程分析会在当前对话显示进度和结果，无需在来源群组发送命令。

分析结果仅供参考，聊天记录不足以确定用户的真实身份、性格或群外行为。候选发言会发送到配置的 API 服务，请根据聊天隐私要求选择可信服务。

### 常见用法：
- **链接分析**（支持私有群组及没有用户名的用户）：`,summarize_user -l <消息链接> [数量]`
- **远程群组分析**：`,summarize_user -g <群组ID/用户名> [数量] <目标用户>`
- **当前群内分析**：`,summarize_user [数量] [目标用户]` 或直接回复对方发送 `,summarize_user [数量]`

### 进阶配置：
- `,summarize_user setapi <API_KEY>` — 设置 API 密钥
- `,summarize_user seturl <BASE_URL>` — 设置自定义 API 地址
- `,summarize_user setmodel <MODEL>` — 设置模型名称
- `,summarize_user setdisplay <NAME>` — 设置输出显示的模型名称
- `,summarize_user setprompt <PROMPT>` — 自定义系统提示词
- `,summarize_user showconfig` — 查看当前配置

### 更新记录：
- **1.7**：长报告延续命令所在 Topic；分块请求失败后停止后续批次。
- **1.6**：修复超长输出、明文远程 API 地址和连接池泄漏，并限制单次分析分块数量。

### 指令说明：
详细指令请在 Telegram 内使用 `,help summarize_user` 查看。
