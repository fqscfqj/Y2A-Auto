# Security Best Practices Report

## Executive summary

2026-03-16 我通过 GitHub CodeQL API 拉取了 `fqscfqj/Y2A-Auto` 默认分支 `main` 的 open 告警，共 **19 条**，归并为 **5 类问题**：路径注入、开放重定向、SQL 注入、敏感信息日志泄露，以及配置/内部标识泄露日志。

本次已在本地代码中完成修复，重点做了：
- 用 `safe_join` + UUID 规范化约束任务目录与封面文件路径。
- 将 `next` 跳转改为仅允许相对路径并在跳转点内联校验。
- 将动态 SQL 字段拼接改为常量白名单片段拼接。
- 去除代理 URL、接口地址、固定分区 ID、推荐分区 ID 等敏感/内部信息的明文日志。

## Findings

### CBP-001 路径注入（High）
- **Rule ID:** `py/path-injection`
- **Locations:**
  - `app.py:273`
  - `app.py:285`
  - `app.py:288-291`
  - `app.py:325`
  - `app.py:1400-1401`
- **Evidence:** GitHub CodeQL alerts `#43 #44 #45 #46 #47 #48 #49`
- **Impact:** 若任务目录或封面路径被构造/污染，攻击者可能越界访问或覆盖任务目录外的文件。
- **Fix:**
  - 任务目录改为 `UUID` 规范化后再通过 `werkzeug.utils.safe_join` 拼接。
  - 封面路径统一经过 `_safe_join_task_dir(...)` 解析。
  - 读取已有封面时仅接受任务目录内文件名，避免直接信任持久化绝对路径。
  - `/tasks/<task_id>/cover` 复用统一安全解析逻辑。
- **Mitigation:** 保持任务 ID 仅使用系统生成 UUID，避免外部可控文件路径写入数据库。
- **Links:**
  - https://github.com/fqscfqj/Y2A-Auto/security/code-scanning/43
  - https://github.com/fqscfqj/Y2A-Auto/security/code-scanning/44
  - https://github.com/fqscfqj/Y2A-Auto/security/code-scanning/45
  - https://github.com/fqscfqj/Y2A-Auto/security/code-scanning/46
  - https://github.com/fqscfqj/Y2A-Auto/security/code-scanning/47
  - https://github.com/fqscfqj/Y2A-Auto/security/code-scanning/48
  - https://github.com/fqscfqj/Y2A-Auto/security/code-scanning/49

### CBP-002 开放重定向（Medium）
- **Rule ID:** `py/url-redirection`
- **Locations:**
  - `app.py:1049`
  - `app.py:1734`
- **Evidence:** GitHub CodeQL alerts `#36 #37`
- **Impact:** 攻击者可诱导用户点击恶意链接并借助站内跳转跳往外部站点。
- **Fix:**
  - 在两个跳转点直接内联解析 `next` 参数。
  - 明确拒绝带 `scheme`、`netloc`、反斜杠的地址，仅允许相对路径。
  - 跳转目标统一重建为站内相对 URL。
- **Mitigation:** 如后续页面继续使用 `next` 参数，沿用同一模式，不要直接 `redirect(request.args['next'])`。
- **Links:**
  - https://github.com/fqscfqj/Y2A-Auto/security/code-scanning/36
  - https://github.com/fqscfqj/Y2A-Auto/security/code-scanning/37

### CBP-003 SQL 注入风险（High）
- **Rule ID:** `py/sql-injection`
- **Location:** `modules/task_manager.py:985`
- **Evidence:** GitHub CodeQL alert `#14`
- **Impact:** 若动态 SQL 片段可被污染，可能导致未授权数据修改。
- **Fix:**
  - 将允许更新的列从“名称集合”改为“常量赋值片段映射”。
  - `SET` 子句现在仅由白名单里的固定 SQL 片段组成，值仍走参数化绑定。
- **Mitigation:** 后续新增可更新字段时，只能追加到常量映射中，不要直接插值列名。
- **Link:** https://github.com/fqscfqj/Y2A-Auto/security/code-scanning/14

### CBP-004 明文日志泄露敏感连接信息（High）
- **Rule ID:** `py/clear-text-logging-sensitive-data`
- **Locations:**
  - `modules/youtube_handler.py:260`
  - `modules/youtube_handler.py:453`
  - `modules/utils.py:427-433`
- **Evidence:** GitHub CodeQL alerts `#41 #42 #7 #8`
- **Impact:** 代理认证信息、带凭据的 API 地址等若进入日志，可能导致凭据泄露。
- **Fix:**
  - 代理日志改为仅记录“已启用代理”，不再输出代理 URL。
  - 基础 URL 屏蔽逻辑改为只保留协议 + 主机[:端口]；告警日志不再输出接口地址。
- **Mitigation:** 对所有 URL/DSN/代理配置一律默认按敏感信息处理。
- **Links:**
  - https://github.com/fqscfqj/Y2A-Auto/security/code-scanning/41
  - https://github.com/fqscfqj/Y2A-Auto/security/code-scanning/42
  - https://github.com/fqscfqj/Y2A-Auto/security/code-scanning/7
  - https://github.com/fqscfqj/Y2A-Auto/security/code-scanning/8

### CBP-005 明文日志泄露内部配置/标识（High）
- **Rule ID:** `py/clear-text-logging-sensitive-data`
- **Locations:**
  - `modules/ai_enhancer.py:1196`
  - `modules/ai_enhancer.py:1198`
  - `modules/ai_enhancer.py:1281`
  - `modules/ai_enhancer.py:1283`
  - `modules/task_manager.py:5329`
- **Evidence:** GitHub CodeQL alerts `#4 #11 #38 #39 #40`
- **Impact:** 内部固定分区 ID、推荐分区 ID 等虽不是口令，但仍可能暴露内部配置和行为细节。
- **Fix:**
  - 相关日志改为记录“命中固定分区配置”“已更新任务”等状态，不再输出具体 ID。
- **Mitigation:** 对外部平台内部 ID、令牌、配置值统一按最小暴露原则记录。
- **Links:**
  - https://github.com/fqscfqj/Y2A-Auto/security/code-scanning/4
  - https://github.com/fqscfqj/Y2A-Auto/security/code-scanning/11
  - https://github.com/fqscfqj/Y2A-Auto/security/code-scanning/38
  - https://github.com/fqscfqj/Y2A-Auto/security/code-scanning/39
  - https://github.com/fqscfqj/Y2A-Auto/security/code-scanning/40

## Verification
- `python -m py_compile app.py modules/task_manager.py modules/utils.py modules/ai_enhancer.py modules/youtube_handler.py` ✅
- `python -m pytest -q`（系统 Python）❌ 未安装 pytest
- `.\.venv\Scripts\python.exe -m pytest -q` ❌ 现有测试集中有 8 个与本次安全修复无关的既有失败（`tests/test_subtitle_pipeline.py`）
- 本次新增了定向 smoke 检查：任务目录越界拒绝、相对跳转标准化、base URL 脱敏，均通过。

## Output
本报告已写入：`security_best_practices_report.md`
