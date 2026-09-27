# Safe Exam Browser 试运行说明

本功能按考试单独启用。未开启 SEB 的旧考试、普通练习、学生首页和管理后台维持原有行为。

## 配置一场试运行考试

1. 在“考试批次管理”创建考试，打开“使用 Safe Exam Browser 安全考试”。Config Key 可以暂时留空。
2. 创建后进入该考试的编辑页，复制页面显示的 `SEB Start URL` 和 `SEB Quit URL`。
3. 在 SEB Config Tool 中把它们分别设置为 Start URL 和 Quit URL。
4. 开启“Use Browser & Config Keys / send in HTTP header”，并完成其余机房限制设置。
5. 保存 `.seb` 文件后，从 SEB Config Tool 复制最终的 64 位 Config Key。
6. 回到考试编辑页粘贴 Config Key 并保存。配置文件发生任何变化并重新保存后，都要重新复制 Config Key。
7. 用学生测试账号和一台机房电脑完成全流程测试，再分发同一份 `.seb` 文件。

## 学生流程

- 学生打开教师发放的 `.seb` 文件，在 SEB 内登录并进入指定考试。
- 每次答题仍按原逻辑立即保存，不需要最后统一提交全部答案。
- 最后一页始终显示“确认完成并退出”，最后一题未答对也可以使用。
- 确认框会显示已答对和未完成题数；确认后记录退出状态并跳转 Quit URL。
- 当前试运行版本不禁止学生在考试有效时间内再次进入，以免误操作造成无法继续答题。正式考试是否禁止再次进入，应在试运行后单独决定。

## 部署设置

`.env` 中：

```env
SEB_INTEGRATION_ENABLED=true
TRUST_PROXY_HEADERS=false
```

如果 Web 容器前方有自己控制的 Nginx 或负载均衡器，并且外部使用 HTTPS，把 `TRUST_PROXY_HEADERS` 改为 `true`。不要在应用可以被绕过代理直接访问时信任任意代理头。

紧急停用 SEB 限制时只需把 `SEB_INTEGRATION_ENABLED=false` 后重新部署；数据库中的考试配置和答题数据不会删除。

## 回退

- 稳定代码标签：`pre-seb-pilot`
- 试运行分支：`codex/seb-pilot`
- 数据库改造只增加字段和 `exam_secure_sessions` 表，回退代码不需要删除这些结构。
