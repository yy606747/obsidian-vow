# 2026-09-13 聊天与前端更新

本次同步聊天界面、Markdown 和文件解析，并补齐表情、按天续聊及移动端恢复所需的运行时改动。

## 聊天与排版

双方消息都支持标题、加粗、斜体、删除线、引用、列表、表格和代码块。普通文字保留气泡，较复杂的排版展开到更宽的内容区域。编辑、复制和存储保留 Markdown 原文，朗读提取正文。

渲染器随源码提供，不依赖 CDN。流式回复合并刷新，完成后立即补齐最后一帧。Core 的共同提示前缀说明了格式支持，并提供几种可自由发挥的排版示例。

心语显示在对应回复下，通过头像旁的思考气泡展开全文。默认 prompt 允许 companion 自己选择想留下的内容。已有自定义设置继续优先使用。

## 文件解析

聊天输入框左侧的加号可以选择文件，发送前可展开卡片查看提取正文。

| 格式 | 支持内容 |
| --- | --- |
| TXT | UTF-8、带 BOM 的 UTF-16/32 和 GB18030 文本 |
| MD | 保留 Markdown 原文与缩进 |
| DOCX | 按顺序提取段落和表格 |
| PDF | 提取文字并保留页码 |

单文件上限为 20 MB、60,000 个字符，PDF 最多 300 页。每条消息最多附带 3 个文档，正文合计最多 90,000 个字符。扫描版和加密 PDF 暂不支持；旧版 `.doc` 需要先另存为 `.docx`。

解析在独立进程中完成，结果缓存到数据目录。后续对话使用提取正文，保留附件来源，不额外调用 OCR 或模型。尚未发送的附件可移除；已经被消息引用的文件继续保留。

## 页面与交互

首页保留双猫背景、时钟和图标入口。其他页面统一暗色材质与金色细节，返回入口回到小屋。

头像保留根据消息内容变化的情绪光圈。动态光圈只短暂呼吸，随后保留静态光色。流金光点采用少量节点和限频更新，页面隐藏、原生后台、子页遮盖或开启减少动态效果时暂停；支持电量接口的环境在低电量时也会暂停。

这次还同步了聊天页依赖的表情收藏、按天续聊、陪伴邀请与 Android 页面恢复代码，以及空回复识别和相关存储修复。

## 更新方式

沿用原有数据目录，先备份数据，再安装更新后的依赖并重启后端。文件解析新增 `pypdf`、`python-docx` 和 `lxml`，版本与散列已写入依赖锁：

```bash
python -m pip install -r obsidian-chat/requirements-dev.txt
```

Docker 安装重新构建镜像即可。重新打开聊天页后会加载新版静态资源；Android 原生恢复和陪伴邀请的更新需要重新构建客户端。

## 本地验证

后端检查使用临时数据，并禁止真实网络和模型调用：

```bash
python scripts/check_backend.py
python scripts/check_backend.py tests/test_document_input.py tests/test_daily_chat.py tests/test_public_defaults.py
```

浏览器检查另外需要 Playwright 与 Chromium。以下检查使用合成聊天内容或临时上传文件：

```bash
python -m pip install playwright
python -m playwright install chromium
python obsidian-chat/tests/frontend_markdown_smoke.py
python obsidian-chat/tests/frontend_documents_smoke.py
python obsidian-chat/tests/frontend_heart_notes_smoke.py
python obsidian-chat/tests/frontend_mood_smoke.py
python obsidian-chat/tests/frontend_design_smoke.py
```
