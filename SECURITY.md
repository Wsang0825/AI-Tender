# Security Policy

## Reporting a vulnerability

请不要在公开 Issue 中披露可利用的凭据、个人数据、完整恶意文档或未修复漏洞的操作细节。

如果仓库启用了 GitHub Private Vulnerability Reporting，请优先使用该机制；否则请通过 GitHub 账号的私下渠道联系维护者，并提供：

- 受影响的版本或 commit；
- 可复现步骤或最小样例；
- 影响范围；
- 已知的缓解方式。

当前仓库没有配置公开安全邮箱，因此不提供未经核实的邮箱地址。

## Security scope

AI-Tender 会处理不可信的外部输入，包括：

- HTML 和外部网站响应；
- PDF、DOCX、XLSX 等文档；
- 公开下载附件；
- 搜索 Provider 返回的标题、摘要和 URL；
- 本地 Snapshot、解析文本和缓存内容。

潜在风险包括但不限于：

- 恶意文档触发解析器漏洞；
- 路径穿越或不安全文件处理；
- SSRF 或意外网络请求；
- 恶意 HTML 或被污染的来源内容；
- 凭据、Cookie、浏览器 Profile 或数据库泄露；
- 命令注入；
- 不安全的 ZIP 等归档解压；
- 第三方依赖漏洞。

本文件列出风险模型，不代表系统已经防御了其中每一项。运行抓取和文档解析时，应使用权限受限的本地账户和隔离的运行目录，并对来源条款、附件和网络边界进行单独评估。

## Secrets and local data

以下内容不得提交 Git：

- `.env` 和其他本地 secret 文件；
- Cookie、登录会话、浏览器 Profile 和 tokens；
- API credentials；
- SQLite 数据库、WAL/SHM 文件和数据库备份；
- 下载的招标附件、Snapshot、缓存、日志和临时输出；
- 含有个人信息或内部信息的测试数据。

如果凭据已经被提交到远程仓库，不要只删除当前文件：应立即撤销/轮换凭据，并私下报告暴露的 commit 和类型。

## Safe reporting for source access

遇到验证码、登录、人机验证或 HTTP 412/403/429 时，不要尝试绕过访问控制。保留来源、动作类型、状态码和未覆盖范围，让使用者完成合法的人工验证后再定向重试。
