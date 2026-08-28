# AI-Tender Agent 约束

这是个人、非商业使用的西北五省新能源招投标搜索项目。

## 路径约束

- 总目录：`D:\AI-Tender`
- 主代码：`D:\AI-Tender\app`
- 参考仓库：`D:\AI-Tender\references`
- Python 虚拟环境：`D:\AI-Tender.venv`

不要把第三方代码直接复制到主项目，除非许可证允许且确实必要。参考仓库与主代码必须分离。

## 预定技术路线

- Scrapling：固定网站主要采集器
- Crawl4AI：未知复杂网页的 AI 友好清洗
- DDGS：全网发现
- PyMuPDF4LLM：普通 PDF 解析
- MinerU：复杂或扫描 PDF 的可选 fallback
- DrissionPage：特殊登录态或国内动态网站的可选 fallback
- RapidFuzz：项目名称去重
- DiskCache：网页、搜索和文档缓存
- SQLite：业务数据库
- FastAPI + Jinja2：本地页面

## 重要原则

- 固定网站优先确定性 Adapter。
- 公开 API 优先于浏览器。
- 不要所有网站使用 AI。
- 不要所有网页使用 Crawl4AI。
- 不要所有 PDF 使用 OCR。
- 不绕过验证码，不破解登录，不伪造抓取结果。
- 所有重要字段以后尽可能保存原始证据，包括来源 URL、来源文件、页码、原文、提取方式和置信度。
- 当前阶段不要实现中国政府采购网、甘肃、新疆或其他省份 Adapter，不实现招标状态判断、AI 字段抽取和业务 Web 页面。
