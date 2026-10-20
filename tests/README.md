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

Telegram 服务器的搜索结果和消息发送需要在线账号验证；上述命令不会连接 Telegram 或发送消息。
