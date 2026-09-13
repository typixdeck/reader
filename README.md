# TypixReader 0.2

面向 TypixDeck CM4 小屏和键盘的原生 GTK3 本地阅读器。支持官方 Raspberry Pi OS ARM64 Bookworm / Trixie；界面默认全屏，不需要浏览器、Node 或网络服务。

## 已实现

- 可点击、可键盘操作的最近书架，直接继续上次阅读；最多展示 20 本本地图书。
- 文件选择、命令行 `.desktop %f` 打开和本地文件拖入；取消载入后恢复当前图书。
- TXT / Markdown：UTF-8（含 BOM）、GB18030、Big5；Markdown 一级至三级标题生成目录，正文以可读纯文本显示。
- EPUB：按 OPF spine 顺序读取本地 XHTML 文字，保留行内文字顺序、抽取章节标题，长章分段；不执行脚本、加载远程图片或使用 EPUB CSS。
- CBZ：数字自然顺序排列图片页，逐页后台读取和解码，按当前可用宽高适配；不会把整本漫画的图片载入内存。
- 全书文字搜索、结果高亮和循环查找；目录跳转；16–34 px 字号。
- 记忆章节 / 漫画页和正文视口顶部字符位置；字体偏好保存在本机。
- 打开、解析、搜索和漫画解码在可取消的后台工作线程执行；过期任务不能覆盖新图书。
- 文件缺失、权限不足、格式损坏、加密、资源超限均显示可恢复提示；打开失败保留当前图书。
- 写入失败时可继续阅读并提示检查存储 / 权限，之后继续尝试保存。状态原子替换失败时保留旧文件。

本阶段支持 TXT、MD / Markdown、EPUB、CBZ。PDF、CBR、MOBI / AZW、FB2、TTS、EPUB 排版 / 内嵌图片尚未实现，不能用这个版本替代它们的系统阅读器。

## 键盘

| 操作 | 快捷键 |
| --- | --- |
| 打开本地文件 | Ctrl+O |
| 选择按钮 / 图书 | Tab / Shift+Tab，Enter |
| 上一 / 下一章节，或漫画页 | ← / →（正文或图片获得焦点时） |
| 向上 / 下滚动一屏，章尾进入下一章 | PageUp / PageDown，Shift+Space / Space |
| 目录 | Ctrl+T；目录内 ↑ / ↓ 跳章 |
| 搜索全文 | Ctrl+F；Enter / F3 查找下一个 |
| 调整字号 | Ctrl+− / Ctrl++ |
| 关闭搜索 / 返回书架，再返回桌面 | Esc |
| 保存进度并返回桌面 | Ctrl+Q，或右上角“返回桌面” |
| 重新请求全屏 | F11 |

在文件选择器内，取消只关闭选择器。在后台打开过程中，Esc 或“取消”回到原来的阅读界面。

## 数据与资源边界

状态文件：`~/.local/share/typix-reader/state.json`（遵循 `XDG_DATA_HOME`），也可通过 `TYPIX_READER_STATE` 指定隔离测试路径。只保存本地图书路径、书名、内容 SHA-256、进度和字号，不联网、不上传。兼容 0.1 的最近列表和阅读进度；文件权限 0600，保留最多 200 份图书进度。

为了控制 CM4 的内存和解码负担：

- 图书文件最多 256 MB，独立文本最多 16 MB。
- ZIP 最多 5,000 个文件，声明的总展开大小最多 256 MB，单成员最多 32 MB；加密或重复成员名拒绝打开。
- EPUB 单份 markup 最多 8 MB，累计正文源文件最多 32 MB；单个显示段落最多 12,000 个字符。
- 所有格式最多 1,000 个显示章节 / 漫画页，章节标题最多 200 字符；Markdown 惰性遍历标题并在每次分章时检查取消，超限文件在进入 GTK 目录前拒绝。
- 漫画每次只解码一页，单页最多 6,400 万像素，并按视口缩放。
- XML 元数据禁止 DTD / 实体；EPUB 正文章节只能引用压缩包中的本地路径。ZIP 不解压到磁盘。

后台取消是协作式的：本地文件分块读取和章节之间会检查取消标记；系统文件 I/O 和当前解码器调用完成后才能结束正在执行的那一步。UI 可立即离开载入页，陈旧回调会被丢弃。

## 与 Launcher 集成

包安装标准入口 `/usr/share/applications/typix-reader.desktop`，由桌面快捷方式选择性加入 Launcher。包本身不遍历应用列表，也不自行创建用户桌面文件。

`Exec=/usr/bin/typix-reader %f` 保留文件参数；`GLib.set_prgname(ai.typixdeck.reader)` 固定 Wayland app ID。窗口在显示前请求全屏，并在首次 map 后补发一次，兼容 CM4 的 labwc 窗口创建时序。通过 `X-TypixDeck-FullscreenAppId=ai.typixdeck.reader` 配合 Launcher 的应用生命周期和桌面恢复。

旧 Evince / Samba wrapper 只读保存在 `reference/current-reader/`，新 Reader 不自动连接 NAS，不要求网络密码，也不上传图书。

## 构建与验证

```sh
cd reader
PYTHONPATH=src python3 -m unittest discover -s tests -v
./build-deb.sh
```

产物为 `dist/typix-reader_0.2.0-1_all.deb`。纯 Python 包声明 `Architecture: all`；依赖 `python3 (>= 3.11), python3-gi, gir1.2-gtk-3.0, gir1.2-gdkpixbuf-2.0`；control 字段 `X-Typix-Compatible-OS: raspios-bookworm,raspios-trixie` 用于 Registry 严格匹配。CM4 ARM64 是主验收目标，其它硬件不作型号假设。

在真机已登录的 GTK3 / Wayland 会话中执行隔离 QA（输出目录应为专用测试目录）：

```sh
PYTHONPATH=src READER_QA_SCREENSHOT_COMMAND=grim /usr/bin/python3 tests/gtk_smoke.py /tmp/typix-reader-qa
```

脚本使用独立 QA Application ID、临时图书和单独状态文件，验证全屏、书架、目录、跨章搜索、字号、滚动续读、CBZ 顺序 / 视口适配 / 续读、文件缺失及取消恢复；输出 `result.json` 和截图。未设置截图环境变量时不调用 `grim`。测试不会读取现有用户书架。

本次 CM4 验证通过 33 项单元测试与 21 项真实 GTK 检查，覆盖精确段落续读、CBZ 续读、错误和取消恢复。另从实际 Launcher 以键盘打开 Reader，验证全屏和正常关闭后桌面恢复；截图为物理 1024×768、缩放后逻辑约 801×601 的设备输出。不同布局、其它图书内容仍需随发布回归，见 [交付记录](docs/VALIDATION.md)。

本仓库只发布当前应用代码、构建文件和可公开文档；历史设备快照、凭据、设备采集记录及本地运行数据不在提交范围内。
