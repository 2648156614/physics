# 本地前端依赖

以下固定版本资源来自 npm 官方发布包，随应用一起部署。浏览器运行时不需要访问 npm 或 CDN。

| 目录 | npm 发布包 | 用途 |
| --- | --- | --- |
| bootstrap-5.1.3 | bootstrap@5.1.3 | 公共样式与组件 |
| bootstrap-icons-1.11.0 | bootstrap-icons@1.11.0 | 公共页面图标与字体 |
| mathjax-3.2.2 | mathjax@3.2.2 | 公式引擎，完整 es5 目录含扩展和字体 |
| chartjs-4.5.1 | chart.js@4.5.1 | 学生统计图表，仅统计页面加载 |
| echarts-5.5.1 | echarts@5.5.1 | 管理员题目统计 |
| gsap-3.12.5 | gsap@3.12.5 | 管理员学生详情动画 |

保留发布包的许可证、NOTICE 或 README/package.json，以及文件中的版权声明。原有 `bootstrap-icons` 目录继续用于独立登录页面。

更新时从 `https://registry.npmjs.org/<包名>/-/<包名>-<版本>.tgz` 获取发布包，使用新的版本目录，并同步更新模板路径。MathJax 必须保留完整的 `es5` 目录，不能只复制入口脚本。

## 浏览器验证

`tests/frontend_rendering.cjs` 使用真实公共模板与换题函数，验证冷启动、并发换题、移动端、后台图表、引擎加载失败；所有资源请求在本地处理，外部请求或缺失文件会导致失败。

需要 Node.js、Playwright、Python/Jinja2 和 Microsoft Edge。运行 `node tests/frontend_rendering.cjs`；可用 `PYTHON` 指定 Python 可执行文件、`BROWSER_CHANNEL` 指定 Playwright 浏览器通道。
