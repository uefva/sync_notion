# Notion Sync

[English](README_EN.md)

把 Notion 工作区内容同步到本地 Markdown 文件，适合用来做个人知识库备份、Git 版本管理、静态站点内容源或离线检索。

## 功能特性

- 同步 Notion 页面为 Markdown 文件
- 同步 Notion Database 为同名入口 Markdown，并将数据库条目导出为同名目录下的独立 Markdown 文件
- 支持递归导出子页面、嵌套块、Toggle、表格、代码块、引用、待办、图片等常见内容
- 支持图片下载到本地 `images/` 目录
- 使用 `.sync_manifest.json` 做镜像式增量同步，未变化页面会复用本地 Markdown
- 每次运行都会刷新 `index.md` 为最新镜像索引
- 自动跳过 Notion 已归档或回收站中的页面、数据库和数据库条目
- 旧清单中存在但本次 Notion 不再出现的内容会移动到 `_stale/`，不会直接删除
- 自动规范化文件名：空白字符会替换为 `_`，常见中文/全角标点会转换为英文标点或安全字符
- 支持并发同步，可通过 `--workers` 调整速度
- 自动生成 `index.md` 索引文件
- 遇到无权限、已删除或 API 不支持的对象时跳过，不中断整体同步

## 环境要求

- Python 3.8+
- requests
- Notion Integration Token

安装依赖：

```bash
pip install requests
```

推荐使用虚拟环境：

```bash
python3 -m venv .venv
.venv/bin/pip install requests
```

## 配置文件

复制示例配置：

```bash
cp conf/config.example.json conf/config.json
```

编辑 `conf/config.json`，填入自己的 Notion Token：

```json
{
  "notion_token": "ntn_你的Token",
  "output_dir": "./notion_sync",
  "request_delay": 0.1,
  "request_timeout": 30,
  "max_workers": 4,
  "download_images": true,
  "image_download_retries": 2
}
```

配置说明：

| 字段 | 说明 |
| --- | --- |
| `notion_token` | Notion Integration Token |
| `output_dir` | Markdown 输出目录 |
| `request_delay` | 请求间隔，单位秒 |
| `request_timeout` | 请求超时，单位秒 |
| `max_workers` | 并发任务数量 |
| `download_images` | 是否下载图片到本地 |
| `image_download_retries` | 图片下载失败后的重试次数 |

`conf/config.json` 包含私密 Token，已经被 `.gitignore` 忽略。发布到 GitHub 时只提交 `conf/config.example.json`。

## Notion 配置

1. 打开 [Notion Integrations](https://www.notion.so/my-integrations)
2. 创建一个 Internal Integration
3. 复制 Integration Token
4. 在 Notion 页面右上角点击 `...`
5. 选择 `Add connections`
6. 添加刚创建的 Integration

如果要同步整个工作区，建议把 Integration 添加到你希望作为根目录的顶级页面上。Notion 的权限是逐层控制的，没有授权的页面或数据库无法通过 API 读取。

## 使用方法

基础运行：

```bash
python sync_notion.py
```

指定输出目录：

```bash
python sync_notion.py --output ./notion_sync
```

使用并发同步：

```bash
python sync_notion.py --workers 4
```

如果触发 Notion 限频，可以降低并发或增加请求间隔：

```bash
python sync_notion.py --workers 3 --delay 0.2
```

临时覆盖配置文件里的 Token：

```bash
python sync_notion.py --token "你的_Notion_Token"
```

查看参数：

```bash
python sync_notion.py --help
```

## 参数说明

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--output` | 配置文件中的 `output_dir` | Markdown 输出目录 |
| `--token` | 配置文件中的 `notion_token` | 临时覆盖 Notion API Token |
| `--delay` | 配置文件中的 `request_delay` | 请求间隔，单位秒 |
| `--workers` | 配置文件中的 `max_workers` | 并发任务数量 |

## 输出结构

```text
notion_sync/
├── index.md
├── .sync_manifest.json
├── images/
├── pages/
├── databases/
├── _orphans/
└── _stale/
```

- `index.md`：本次同步的页面索引
- `.sync_manifest.json`：镜像式增量同步清单，用于判断哪些页面可以复用
- `images/`：下载到本地的图片
- `pages/`：普通页面导出的 Markdown；如果页面下有数据库，会生成数据库同名 `.md` 入口文件，并用同名文件夹保存数据库条目
- `databases/`：工作区根级数据库的入口 Markdown 和同名条目目录
- `_orphans/`：父级无法通过 API 递归定位时的兜底输出目录
- `_stale/`：旧清单中存在但本次不再同步到的 Markdown 文件或数据库目录归档

页面和数据库使用一致的本地组织方式：

```text
pages/个人文件.md
pages/个人文件/日记·周记·随缘记.md
pages/个人文件/日记·周记·随缘记/第一篇.md
```

其中 `日记·周记·随缘记.md` 是数据库入口文件，里面包含数据库标题、Notion 元数据和条目链接；`日记·周记·随缘记/` 目录保存该数据库中的每条记录。

## 文件命名规则

同步脚本会对新建文件和文件夹名称做安全清洗：

- 空格、全角空格、换行等空白字符统一替换为 `_`
- 常见中文/全角标点会转换为英文标点，例如 `（` 和 `）` 转为 `(` 和 `)`，`，` 转为 `,`，`。` 转为 `.`
- Windows 文件名不允许的字符会替换为 `_`，例如 `:`、`?`、`"`、`/`、`\`
- 同名文件会追加 `_2`、`_3` 这样的后缀，不再使用带空格的 ` (2)`

示例：

```text
我的 文件 名 -> 我的_文件_名
日记（周记），随缘记。 -> 日记(周记),随缘记
“标题”，测试：同步？ -> '标题',测试_同步_
```

脚本不会主动清空 `output_dir`，避免误删其他文件。`index.md` 只记录本次最新同步到的页面；旧清单管理过但本次没有同步到的文件会移动到 `_stale/`。不在 `.sync_manifest.json` 里的手写文件不会被移动或删除。

## 定时同步

Linux 服务器可以使用 `cron` 定时执行。

编辑定时任务：

```bash
crontab -e
```

每 6 小时同步一次：

```bash
0 */6 * * * cd /home/software/notion_sync && .venv/bin/python3 sync_notion.py --workers 4 --delay 0.1 >> sync.log 2>&1
```

每天凌晨 3 点同步一次：

```bash
0 3 * * * cd /home/software/notion_sync && .venv/bin/python3 sync_notion.py --workers 4 --delay 0.1 >> sync.log 2>&1
```

查看日志：

```bash
tail -f /home/software/notion_sync/sync.log
```

## 常见日志说明

### 400: database does not contain any data sources accessible

通常表示 Notion API 找到了一个 database 块，但当前 Integration 不能读取它的数据源。

可能原因：

- database 没有授权给 Integration
- database 是 Notion 新版 data source 结构，旧接口无法读取
- 页面里只是一个数据库视图或链接，并不是可直接读取的完整数据库

脚本会跳过该对象并继续同步。

### 404: pages/databases

通常表示页面或数据库不存在、已删除、已归档，或者当前 Integration 没有权限读取。

脚本会跳过该对象并继续同步。

### 图片下载失败: Invalid URL `assets/...`

这通常来自从 Word、Markdown 或网页导入到 Notion 的内容。图片地址是相对路径，不是完整的 `https://...` 链接，因此无法通过网络下载。

脚本会保留原始图片链接，不会因为单张图片失败而中断。完整的 `http://` 或 `https://` 图片会按 `image_download_retries` 配置重试；如果仍然失败，也会保留外链继续同步。

### 429: 触发限频

Notion API 有请求频率限制。出现 429 时脚本会等待后重试。

可以降低并发：

```bash
python sync_notion.py --workers 2 --delay 0.2
```

## 安全提醒

不要把真实 Notion Token 提交到 GitHub。仓库里应该只提交 `conf/config.example.json`，不要提交本地的 `conf/config.json`。

如果 Token 已经提交到公开仓库，请立即到 Notion Integration 后台重新生成 Token。

## License

MIT
