# 插件验证

测试直接加载 PagerMaid、Pyrogram、httpx 和 SqliteDict，使用独立容器的配置与数据库。容器禁用网络，不需要 Telegram session 或 API 密钥。

在已经运行 PagerMaid 容器的主机上，从本仓库目录执行：

```bash
review_image=$(docker inspect pagermaid --format '{{.Image}}')
docker run --rm --network none \
  -e PGM_PLUGIN_TESTING=1 \
  -e PGM_ALLOW_ANALYTIC=false \
  -e PGM_ERROR_REPORT=false \
  -e TMPDIR=/pagermaid/workdir/data \
  -v "$PWD:/review:ro" \
  --entrypoint sh "$review_image" /review/tests/run.sh
```

验证内容包括删除任务的取消、重试、恢复和完成，inline bot 消息过滤，分析输入的数量与截断标记，无冲突结果处理，长报告的 HTML 解析，以及真实 HTTP 连接失败后停止剩余批次。

自动删除回归测试还会按分发顺序执行真实注册的监听器和后续命令包装器，确认自动删除不会占用消息处理标记、阻止普通命令执行。

`linkcopy` 使用真实 Pyrogram 消息、媒体和实体类型，并校验调用参数与客户端方法签名一致。覆盖公开/私有/话题/评论链接、文字替换、相册顺序与配文实体、受保护媒体优先复用及被拒绝后的下载重传、回复位置、限流、网络失败和取消后的临时文件清理。

`linkcopy` 批量回归还覆盖示例区间的 79 条视频顺序、批量读取与缺失消息跳过、文字实体与回复/话题、跨读取批次的相册去重和区间裁剪、逐条发送、进度更新、下载重传、失败中止及取消/续传位置、并发任务保护和取消后的任务清理。

`linkcopy` 下载加速回归使用真实 FileId 编解码和原始请求序列化，校验媒体会话与发送方法签名，覆盖 4 路并发上限、乱序分块完整性、尾块、原生回退及 CDN 残片清理、短/长期限流、超时、取消后排空 worker、跨客户端的并发限制、重传元数据及进度通知失败。没有连接 Telegram，不代表真实网络吞吐量测试。

如果本机没有运行中的 PagerMaid 容器，可将上述 `review_image` 换成已有的兼容测试镜像（本次使用 `pagermaid-plugins:linkcopy-test`）；其余参数相同。

Telegram 服务器的搜索结果和消息发送需要在线账号验证；上述命令不会连接 Telegram 或发送消息。
