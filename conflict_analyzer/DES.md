# conflict_analyzer

PagerMaid-Pyro 群聊冲突分析插件。

### 功能特点

以争吵中的任意消息为锚点，读取前后的连续群聊消息。分析时不会预设只有两方，也不依赖参与者使用回复功能；模型会结合话题、时间、回复关系和提及关系，排除无关闲聊并识别中途加入者。

报告会分别说明冲突起因、升级过程、各方主张、聊天内证据、论证问题和沟通责任。证据不足时明确标注无法判断，不强行判定输赢。

### 常见用法

- 回复争吵中的任意消息：`,conflict`
- 限制候选消息数量：`,conflict 200`（支持 20–500 条）
- 使用时间窗口：`,conflict 30m`、`,conflict 2h`（锚点前后各取对应时长）
- 通过消息链接远程分析：`,conflict -l <消息链接> 30m`

### 配置

- `,conflict setapi <API_KEY>` — 设置 API 密钥
- `,conflict seturl <BASE_URL>` — 设置 OpenAI 兼容 API 地址
- `,conflict setmodel <MODEL>` — 设置模型名称
- `,conflict setdisplay <NAME>` — 设置结果中显示的模型名称
- `,conflict showconfig` — 查看当前配置

如果没有单独配置，本插件会自动复用 `summarize_user` 的 API 密钥、地址和模型配置。

### 注意

分析只能依据当前账号能够读取的消息。已删除消息、群外事实和超出窗口的上下文可能影响结论，报告会提示相应局限。

候选聊天记录会发送到所配置的 OpenAI 兼容 API，请根据群聊隐私要求选择可信的服务。
