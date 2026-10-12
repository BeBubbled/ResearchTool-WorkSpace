# Research Toolkit · Prompt Bridge

此 Chrome / Edge Manifest V3 扩展连接本机 `/prompts` 工作台与 `https://chatgpt.com/`。

1. 在 Chrome 打开 `chrome://extensions`，或在 Edge 打开 `edge://extensions`。
2. 开启“开发者模式”，点击“加载已解压的扩展程序”，选择本目录（其中包含 `manifest.json`）。
3. 在同一浏览器中刷新本机工作台，确认“扩展已连接”，并登录 ChatGPT。
4. 组合 Prompt、选择附件，点击“提交到 ChatGPT”。扩展会打开新标签页、填充文本并添加附件；请检查内容和附件处理结果后手动点击发送。

工作台必须以 `http://127.0.0.1:<端口>/prompts` 或 `http://localhost:<端口>/prompts` 打开，端口不限。更新扩展后，在扩展管理页点击“重新加载”，再刷新工作台。

已经安装但未连接：确认页面和扩展位于同一 Chrome / Edge 个人资料中。在安装或重新加载扩展之前已打开的页面，需要刷新一次（文字草稿保留，附件需重新选择）。检查扩展“详细信息”中的站点访问权限。Codex 内置浏览器不能使用 Chrome / Edge 中安装的扩展。“服务工作进程（不活动）”是正常休眠，收到消息后会唤醒。

1.0.1 将工作台连接脚本提前加载，加入就绪通知及自动重试；页面分别提示未收到响应、扩展更新后需刷新和后台错误。更新 ZIP 时应覆盖原来的扩展目录，再重新加载，无需删除扩展。

1.0.2 适配没有旧输入框 ID 的新版 ChatGPT 撰写器（`data-chatgpt-composer` / `data-composer-markdown`）。只在撰写器中寻找可编辑输入框，忽略扩展搜索框和隐藏编辑器；普通页面加载最多等待 30 秒，附件等待登录最多 180 秒，并显示等待进度。

扩展只请求本机主机访问权限和临时会话存储。ChatGPT 内容脚本仅处理本扩展为本次提交新建的标签页；已有会话不受影响。凭证与 Prompt 不放在网址里，附件以 512 KB 分块从本机读取，文件内容不会保存到扩展存储。没有调用 OpenAI API，不需要 API Key。

如果等待登录超时、标签页在交接时刷新、ChatGPT 页面结构变动或附件卡片无法确认，扩展会明确报告错误。附件格式、数量、上传处理与账户额度最终由 ChatGPT 决定；检测到卡片表示已交接到页面，不代表服务器已完成处理。可以保留填入的文字并手动补传附件，或从工作台重新提交。刷新工作台后需重新选择本地附件。

实现参考：[Chrome 扩展跨域请求](https://developer.chrome.com/docs/extensions/develop/concepts/network-requests)、[消息通信](https://developer.chrome.com/docs/extensions/develop/concepts/messaging)。
