# 来源、摘要补全与调度

## 默认频率

| 来源 | 默认间隔 | 行为 |
|---|---:|---|
| arXiv | 官方发布后 | 按 [arXiv 官方时间表](https://info.arxiv.org/help/availability.html)，美东时间周日至周四 20:00 发布后 10 分钟同步；自动处理夏令时，并保留一天回退窗口。 |
| SciRate | 3 天 | 请求 `?range=3` 的滚动三日视图，导入首页按票数排序的 50 篇论文；成功后清除离开窗口的旧热门标记。若被 HTTP 403/Cloudflare 验证拦截，则保留上次成功数据并在设置页显示明确原因。 |
| Google Scholar | 7 天 | 通过 SerpAPI `google_scholar_author` 同步重点作者，按 `pubdate` 读取至多 100 篇。 |
| 重点期刊 | 7 天 | 默认 Nature、Nature Physics、PRL，也可添加公开 HTTPS RSS/Atom。 |

设置页可单独启用/停用来源；arXiv 固定跟随官方发布日历，其他来源可将间隔设为 1–30 天。调度器每 5 分钟检查一次：到期来源成功后计算下次时间；HTTP 429 限流会先做有限退避并在 30 分钟后重试，其他失败 6 小时后重试，不影响其他来源或已有论文。arXiv 的常规抓取时间换算为北京时间是夏令时周一至周五 08:10、冬令时周一至周五 09:10；官方节假日或临时延期可能例外。

## Google Scholar Abstract

SerpAPI 的 Author Articles 响应提供标题、作者、年份、链接和引用量，但通常不提供 abstract。Scholar 导入后的缺失摘要按以下顺序尝试补齐：

1. 本地库中 arXiv ID、DOI、规范化标题、作者与年份的可信匹配。
2. Semantic Scholar `/graph/v1/paper/search/match`；有 `SEMANTIC_SCHOLAR_API_KEY` 时使用 `x-api-key`，未配置时匿名低频请求。
3. Semantic Scholar 返回 arXiv ID 后，从 arXiv API 获取原始 abstract。
4. 已知公开论文页的 `citation_abstract` 元数据。

只有 DOI/arXiv ID 精确一致，或标题相似度至少 0.95 且作者姓氏重合、年份相差不超过一年时才写入摘要。补全会记录来源、置信度、状态与检查时间；查不到会标为 `missing`，不会让 LLM 编造内容。

## DeepSeek

- 每周：读取最多 500 篇有互动的论文（标题、作者、abstract、互动类型与时间），生成结构化偏好画像。
- 每三天：读取最近 7 天候选；不足 50 时从最近 30 天未推荐论文补齐。候选按 40–60 篇批量交给 DeepSeek，返回受严格校验的 ID、偏好分与中文理由。
- 输出不完整、无 key、额度不足或服务失败时，使用确定性本地排序，页面仍可使用。

模型调用记录为 `preference_profile` 与 `recommendation_rerank`，同时保留模型名、prompt 版本和 token 用量。
