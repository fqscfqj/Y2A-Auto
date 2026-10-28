## 审核结论：可以合入（建议 nits 后续跟进，无需再开一轮）

改动范围集中、和模块既有抽象贴合（字段加进 `MONITOR_CONFIG_FIELD_DEFAULTS` 后自动被 `_collect_monitor_config_values` 的通用 SQL 覆盖，不需要再手改多处 INSERT/UPDATE），默认空值 = 不过滤，向后兼容。下面是我本地跑过的验证和 6 条非阻塞建议。

---

### 已验证的部分

| 项目 | 结果 |
| --- | --- |
| `tests/test_youtube_monitor_title_filter.py` | 11 个用例全部通过 |
| `tests/test_youtube_monitor_config_template.py`（含新增回显用例） | 3 个用例全部通过 |
| 相关既有监控测试（`config_sql_dedup` / `dedup_before_truncation` / `quota_budget` / `api_status`） | 42 个用例全部通过，无回归 |
| 集成路径（真实 `_filter_videos` + API 形状的假视频） | `title_keywords='asmr'` 时 `【ASMR】耳语` 通过、`游戏实况` 被过滤 ✅ |
| 老库迁移（建库后 `DROP COLUMN title_keywords/title_match_mode` 再重新实例化） | 两列被 `ALTER TABLE` 正确补回，且值为 `''` / `'any'` ✅ |

另外核实了 PR 描述里"所有模式都生效"这一点：`run_monitor` → `_filter_videos` 是唯一入口（`modules/youtube_monitor.py:900`、`:974`），标题过滤挂在 `_meets_criteria`（`:1751`），latest / historical / 频道搜索 / 关键词搜索确实全覆盖。

---

### 建议（按优先级）

**1）空格作为分隔符会破坏"短语"匹配，容易误伤（建议至少在文档里写明）**
`_split_title_keywords`（`modules/youtube_monitor.py:1683`）把空格也当分隔符，所以 `deep sleep` 会被拆成 `deep` + `sleep`。我实测在 `any` 模式下标题 `Sleep Walking` 也会命中：

```
_split_title_keywords('deep sleep') -> ['deep', 'sleep']
_title_matches('Sleep Walking', {'title_keywords': 'deep sleep', 'title_match_mode': 'any'}) -> True
```

对 Issue #158 的 `asmr, 助眠` 场景没问题，但想让用户按短语过滤时行为会不符合直觉。建议二选一：支持引号短语（`"deep sleep"`），或在 `templates/youtube_monitor_config.html` 的 `form-text` / README 里明确写"空格等同分隔符，暂不支持含空格短语"。

**2）历史搬运模式下 filter 变化不会重置 `historical_offset`（建议本 PR 一并处理）**
`historical_offset` 统计的是**过滤后**列表的下标（`modules/youtube_monitor.py:908` 的 `filtered_videos[current_offset:]`、`:1985` 的 `new_offset = current_offset + added_count`）。给一个已有历史搬运配置新增/修改标题关键词后，过滤集合本身变了，offset 会指向错位，可能漏跑或重跑一批视频。`update_monitor_config` 里已经有 start/end date 变化时重置 offset 的先例（`should_reset_offset`），建议把 `title_keywords` / `title_match_mode` 也纳入同一判断。`exclude_keywords` 其实有同样问题，可以顺手一起处理或另开 issue 说明。

**3）`update_field_defaults` 建议给标题字段加兜底（防御性）**
`update_monitor_config` 的 `update_field_defaults`（`modules/youtube_monitor.py:784` 附近）目前只兜底 `historical_offset` 和 `video_types`。因为 `_collect_monitor_config_values` 对缺省 key 会回落到 `MONITOR_CONFIG_FIELD_DEFAULTS`，任何漏传 `title_keywords` 的更新调用都会**静默把过滤条件清空**。现在只有 `app.py` 的表单一个调用方，所以不是线上 bug，但加两行兜底（沿用旧值）成本很低。

**4）迁移测试名与实测内容不符，建议补真实迁移用例**
`test_existing_database_gets_title_filter_columns` 实际只断言"新建库自带这两列"，没有走 `ALTER TABLE` 分支，老库升级路径目前是零覆盖。我已验证下面这种写法可行：

```python
def test_legacy_database_is_migrated(self):
    with sqlite3.connect(self.monitor.db_path) as conn:
        conn.execute("ALTER TABLE monitor_configs DROP COLUMN title_keywords")
        conn.execute("ALTER TABLE monitor_configs DROP COLUMN title_match_mode")

    YouTubeMonitor()  # 重新初始化触发迁移

    with sqlite3.connect(self.monitor.db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(monitor_configs)")}
    self.assertIn("title_keywords", columns)
    self.assertIn("title_match_mode", columns)
```

（`DROP COLUMN` 需要 SQLite ≥ 3.35，CI 的 Python 3.11 自带版本满足。）

**5）一致性小项**
- `templates/youtube_monitor_history.html:74` 附近的配置摘要已经展示 `keywords` / `exclude_keywords`，但没展示新的标题关键词，从历史页看不到这个过滤条件。
- `templates/youtube_monitor.html:101` 只显示关键词、不显示 OR/AND，`any` 和 `all` 在列表上看不出区别（可以考虑像类型那样加个小 badge）。
- `app.py:3918` / `app.py:3990` 两处 `request.form.get('title_match_mode')` 取值 + 白名单校验重复，建议抽一个 `normalize_title_match_mode()`（模块内常量 + helper），避免以后加第三种模式时只改一处。

**6）文档（可选）**
README 只在「YouTube 监控」列表里加了一条 bullet，如果「使用指南」里有监控配置的分步说明，建议也在那里补一句 `asmr, 助眠` + 「任一/全部」的实际用法。

---

### 其他

PR 描述里提到本地有 2 条 VAD 用例因未装 `torch` 失败——我在本机同样环境复现，与本改动无关，CI 装了 `requirements.txt` 后会正常。`Closes #158` 与 Issue 场景对得上，没问题。
