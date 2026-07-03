#!/usr/bin/env python3
"""
Notion → 本地 Markdown 同步脚本

使用方法：
  1. pip install requests
  2. 复制 conf/config.example.json 为 conf/config.json，并填入 Notion Token
  3. python sync_notion.py

第一次运行会全量拉取，之后会跳过未修改的页面。
"""

import os
import json
import time
import argparse
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse

try:
    import requests
except ImportError:
    print("请先安装 requests: pip install requests")
    exit(1)

# ============================================================
# ★ 配置区域 ★
# ============================================================

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = SCRIPT_DIR / "conf" / "config.json"

# 默认配置，可在 conf/config.json 中覆盖
NOTION_TOKEN = ""

# 本地同步目录（不存在会自动创建）
OUTPUT_DIR = "./notion_sync"

# 请求间隔（秒），避免触发 Notion API 限频
REQUEST_DELAY = 0.1

# 请求超时（秒），避免网络异常时一直卡住
REQUEST_TIMEOUT = 30

# 并发数量。Notion 接口有限频，太高会触发更多 429 重试
MAX_WORKERS = 4

# 是否将图片下载到本地（否则只保留外链）
DOWNLOAD_IMAGES = True

# Notion API 版本
NOTION_VERSION = "2022-06-28"

# 同步状态文件，用来跳过未修改页面
MANIFEST_NAME = ".sync_state.json"

# ============================================================
# Notion API 封装
# ============================================================

HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Content-Type": "application/json",
    "Notion-Version": NOTION_VERSION,
}

BASE = "https://api.notion.com/v1"

RATE_LOCK = threading.Lock()
LAST_REQUEST_AT = 0.0
VISITED_LOCK = threading.Lock()


def update_headers():
    """根据当前 Token 刷新请求头。"""
    HEADERS["Authorization"] = f"Bearer {NOTION_TOKEN}"


def load_config(config_file=CONFIG_FILE):
    """读取本地配置文件。"""
    try:
        with open(config_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            print(f"  ⚠️ 配置文件格式不正确，已忽略: {config_file}")
            return {}
        return data
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"  ⚠️ 配置文件读取失败，已忽略: {config_file} ({e})")
        return {}


def wait_for_rate_slot():
    """控制请求发起间隔，避免并发时瞬间打满 Notion API。"""
    global LAST_REQUEST_AT
    if REQUEST_DELAY <= 0:
        return

    with RATE_LOCK:
        now = time.monotonic()
        wait_time = LAST_REQUEST_AT + REQUEST_DELAY - now
        if wait_time > 0:
            time.sleep(wait_time)
        LAST_REQUEST_AT = time.monotonic()


def error_detail(resp):
    """提取 Notion 错误信息，避免日志过长。"""
    try:
        data = resp.json()
        return data.get("message") or resp.text[:200]
    except Exception:
        return resp.text[:200]


def notion_get(path, params=None):
    """带限频的 GET 请求"""
    url = f"{BASE}/{path}"
    wait_for_rate_slot()
    try:
        resp = requests.get(url, headers=HEADERS, params=params, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        print(f"  ⚠️ 请求失败: {url} ({e})")
        return None
    if resp.status_code == 429:
        retry = int(resp.headers.get("Retry-After", 3))
        print(f"  ⏳ 触发限频，等待 {retry}s ...")
        time.sleep(retry)
        return notion_get(path, params)
    if resp.status_code == 401:
        print("❌ Token 无效或没有权限。检查你的 Integration Token。")
        print("   注意：你需要把 Integration 分享到你的工作区顶级页面。")
        print("   参考第二步：页面 ··· → Add connections → 选你的 Integration")
        exit(1)
    if resp.status_code == 404:
        # 可能是页面被删除或无权访问
        print(f"  ⚠️ 404: {url} （跳过）")
        return None
    if resp.status_code in (400, 403):
        print(f"  ⚠️ {resp.status_code}: {url} （跳过: {error_detail(resp)}）")
        return None
    resp.raise_for_status()
    return resp.json()


def notion_post(path, body=None):
    url = f"{BASE}/{path}"
    wait_for_rate_slot()
    try:
        resp = requests.post(url, headers=HEADERS, json=body or {}, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        print(f"  ⚠️ 请求失败: {url} ({e})")
        return None
    if resp.status_code == 429:
        retry = int(resp.headers.get("Retry-After", 3))
        print(f"  ⏳ 触发限频，等待 {retry}s ...")
        time.sleep(retry)
        return notion_post(path, body)
    if resp.status_code == 401:
        print("❌ Token 无效或没有权限。检查你的 Integration Token。")
        print("   注意：你需要把 Integration 分享到你的工作区顶级页面。")
        print("   参考第二步：页面 ··· → Add connections → 选你的 Integration")
        exit(1)
    if resp.status_code == 404:
        print(f"  ⚠️ 404: {url} （跳过）")
        return None
    if resp.status_code in (400, 403):
        print(f"  ⚠️ {resp.status_code}: {url} （跳过: {error_detail(resp)}）")
        return None
    resp.raise_for_status()
    return resp.json()


# ============================================================
# 获取工作区所有页面的递归遍历
# ============================================================

def get_all_pages(cursor=None):
    """获取所有顶级页面（Block Children），包含分页"""
    pages = []
    has_more = True
    start_cursor = cursor

    while has_more:
        params = {"page_size": 100}
        if start_cursor:
            params["start_cursor"] = start_cursor

        # 搜索所有页面
        body = {
            "query": "",
            "sort": {"direction": "descending", "timestamp": "last_edited_time"},
            "page_size": 100,
        }
        if start_cursor:
            body["start_cursor"] = start_cursor

        data = notion_post("search", body)
        if not data:
            break

        results = data.get("results", [])
        pages.extend(results)
        has_more = data.get("has_more", False)
        start_cursor = data.get("next_cursor")

        # 进度提示
        print(f"  📋 已获取 {len(pages)} 个页面...")

    return pages


def get_block_children(block_id, cursor=None):
    """获取某个 block 的所有子 block"""
    blocks = []
    has_more = True
    start_cursor = cursor

    while has_more:
        params = {"page_size": 100}
        if start_cursor:
            params["start_cursor"] = start_cursor

        data = notion_get(f"blocks/{block_id}/children", params)
        if not data:
            break

        results = data.get("results", [])
        blocks.extend(results)
        has_more = data.get("has_more", False)
        start_cursor = data.get("next_cursor")

    return blocks


# ============================================================
# 图片下载
# ============================================================

def download_image(url, output_dir, page_id, block_id):
    """下载图片到本地 pages/images/ 目录，返回相对路径"""
    images_dir = output_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    # 用 URL 的路径部分 + block_id 生成唯一文件名
    parsed = urlparse(url)
    path_part = parsed.path
    ext = os.path.splitext(path_part)[1]
    if not ext or ext not in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp"):
        ext = ".png"  # 默认

    safe_name = f"{page_id[:8]}_{block_id[:8]}{ext}"
    filepath = images_dir / safe_name

    # 已下载过则跳过
    if filepath.exists():
        return filepath

    try:
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        with open(filepath, "wb") as f:
            f.write(resp.content)
        return filepath
    except Exception as e:
        print(f"  ⚠️ 图片下载失败 ({url[:60]}...): {e}")
        return None


# ============================================================
# Notion 区块 → Markdown 转换
# ============================================================

def rich_text_to_md(rich_text):
    """将 rich_text 数组转换为 Markdown 字符串"""
    parts = []
    for rt in rich_text:
        text = rt.get("plain_text", "")
        annotations = rt.get("annotations", {})

        # 转义 Markdown 特殊字符
        if annotations.get("code"):
            text = f"`{text}`"
        else:
            text = text.replace("*", "\\*").replace("_", "\\_")

        if annotations.get("bold"):
            text = f"**{text}**"
        if annotations.get("italic"):
            text = f"*{text}*"
        if annotations.get("strikethrough"):
            text = f"~~{text}~~"

        href = rt.get("href")
        if href:
            text = f"[{text}]({href})"

        parts.append(text)

    return "".join(parts)


def notion_color_to_md(color):
    """Notion 颜色 → 没有直接 Markdown 对应，记录提示"""
    if color and color != "default":
        return f"*[{color}]* "
    return ""


def block_to_md(block, indent=0, output_dir=None, page_id="", current_dir=None):
    """将单个 Notion block 转换为 Markdown 行"""
    block_type = block.get("type", "unsupported")
    b_data = block.get(block_type, {})
    indent_str = "  " * indent
    md = []

    if block_type == "paragraph":
        text = rich_text_to_md(b_data.get("rich_text", []))
        if text.strip():
            md.append(f"{indent_str}{text}\n")

    elif block_type == "heading_1":
        text = rich_text_to_md(b_data.get("rich_text", []))
        md.append(f"\n{indent_str}# {text}\n")

    elif block_type == "heading_2":
        text = rich_text_to_md(b_data.get("rich_text", []))
        md.append(f"\n{indent_str}## {text}\n")

    elif block_type == "heading_3":
        text = rich_text_to_md(b_data.get("rich_text", []))
        md.append(f"\n{indent_str}### {text}\n")

    elif block_type == "bulleted_list_item":
        text = rich_text_to_md(b_data.get("rich_text", []))
        md.append(f"{indent_str}- {text}\n")

    elif block_type == "numbered_list_item":
        text = rich_text_to_md(b_data.get("rich_text", []))
        md.append(f"{indent_str}1. {text}\n")

    elif block_type == "to_do":
        text = rich_text_to_md(b_data.get("rich_text", []))
        checked = b_data.get("checked", False)
        checkbox = "[x]" if checked else "[ ]"
        md.append(f"{indent_str}- {checkbox} {text}\n")

    elif block_type == "toggle":
        text = rich_text_to_md(b_data.get("rich_text", []))
        md.append(f"{indent_str}<details>\n{indent_str}<summary>{text}</summary>\n\n")
        # toggle 的子元素在后面递归处理
        md.append(f"{indent_str}</details>\n")

    elif block_type == "code":
        text = rich_text_to_md(b_data.get("rich_text", []))
        lang = b_data.get("language", "")
        md.append(f"{indent_str}```{lang}\n{text}\n```\n")

    elif block_type == "quote":
        text = rich_text_to_md(b_data.get("rich_text", []))
        md.append(f"{indent_str}> {text}\n")

    elif block_type == "divider":
        md.append(f"\n{indent_str}---\n")

    elif block_type == "callout":
        text = rich_text_to_md(b_data.get("rich_text", []))
        emoji = ""
        icon = b_data.get("icon")
        if icon and icon.get("type") == "emoji":
            emoji = icon["emoji"]
        md.append(f"{indent_str}> {emoji} {text}\n")

    elif block_type == "image":
        caption = rich_text_to_md(b_data.get("caption", []))
        # 图片链接
        image_url = None
        if b_data.get("type") == "external":
            image_url = b_data["external"].get("url")
        elif b_data.get("type") == "file":
            image_url = b_data["file"].get("url")

        if image_url:
            alt = caption or "image"

            # 尝试下载到本地
            local_path = None
            if DOWNLOAD_IMAGES and output_dir:
                local_path = download_image(image_url, output_dir, page_id, block["id"])

            if local_path:
                link_path = relative_markdown_link(local_path, current_dir) if current_dir else str(local_path)
                md.append(f"{indent_str}![{alt}]({link_path})\n")
            else:
                md.append(f"{indent_str}![{alt}]({image_url})\n")

    elif block_type == "bookmark":
        url = b_data.get("url", "")
        caption = rich_text_to_md(b_data.get("caption", []))
        text = caption or url
        md.append(f"{indent_str}- [{text}]({url})\n")

    elif block_type == "embed":
        url = b_data.get("url", "")
        md.append(f"{indent_str}[Embed: {url}]({url})\n")

    elif block_type == "equation":
        expression = b_data.get("expression", "")
        md.append(f"{indent_str}$$ {expression} $$\n")

    elif block_type == "table":
        md.append(f"\n{indent_str}<!-- table (rows rendered below) -->\n")

    elif block_type == "table_row":
        cells = b_data.get("cells", [])
        cell_texts = [rich_text_to_md(cell) for cell in cells]
        md.append(f"{indent_str}| {' | '.join(cell_texts)} |\n")

    elif block_type == "unsupported":
        md.append(f"{indent_str}<!-- ⚠️ unsupported block type -->\n")

    elif block_type == "child_page":
        # 子页面在递归中处理，这里只生成链接
        title = b_data.get("title", "untitled")
        page_id = block["id"]
        md.append(f"{indent_str}- [{title}](pages/{page_id}.md)\n")

    elif block_type == "child_database":
        title = b_data.get("title", "untitled")
        db_id = block["id"]
        md.append(f"{indent_str}- [Database: {title}](databases/{db_id}.md)\n")

    return "".join(md)


def sanitize_filename(name):
    """清理非法文件名字符"""
    invalid_chars = '<>:"/\\|?*'
    result = "".join(c if c not in invalid_chars else "_" for c in name)
    result = result.strip(". ")
    return result or "untitled"


def get_page_title(page):
    """从 page 对象中提取标题"""
    properties = page.get("properties", {})

    # 尝试几种常见的 title 字段名
    for title_field in ["title", "Name", "名前", "名称"]:
        prop = properties.get(title_field, {})
        if prop:
            title_data = prop.get("title", [])
            if title_data:
                return rich_text_to_md(title_data)

    # 对于 database item，尝试第一个 title 类型的属性
    for prop_name, prop_data in properties.items():
        if prop_data.get("type") == "title":
            title_list = prop_data.get("title", [])
            if title_list:
                return rich_text_to_md(title_list)

    return "Untitled"


def get_database_title(database):
    """从 database 对象中提取标题"""
    title_data = database.get("title", [])
    if title_data:
        return rich_text_to_md(title_data)
    return "Untitled"


def yaml_value(value):
    """生成安全的 YAML 字符串标量。"""
    return json.dumps("" if value is None else str(value), ensure_ascii=False)


def markdown_path(path):
    """统一 Markdown 链接里的路径分隔符。"""
    return str(path).replace(os.sep, "/")


def relative_markdown_link(target, current_dir):
    """生成相对当前 Markdown 文件目录的链接。"""
    return markdown_path(os.path.relpath(str(target), str(current_dir)))


def load_manifest(output_dir):
    """读取上次同步状态。"""
    manifest_path = output_dir / MANIFEST_NAME
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"  ⚠️ 同步状态读取失败，将重新同步: {e}")
        return {}


def save_manifest(output_dir, all_pages):
    """保存本次同步状态。"""
    manifest_path = output_dir / MANIFEST_NAME
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(all_pages, f, ensure_ascii=False, indent=2)


def unchanged_page_info(page_id, last_edited, manifest):
    """如果页面未变化且本地文件仍存在，返回旧记录。"""
    info = (manifest or {}).get(page_id)
    if not info:
        return None
    if info.get("last_edited") != last_edited:
        return None
    file_path = info.get("file")
    if not file_path or not Path(file_path).exists():
        return None
    return info


def escape_table_cell(text):
    """避免表格单元格中的换行和竖线破坏 Markdown 表格。"""
    return text.replace("\n", "<br>").replace("|", "\\|")


def render_table(block, indent=0):
    """渲染 Notion table block。"""
    indent_str = "  " * indent
    table_rows = get_block_children(block["id"])
    if not table_rows:
        return "\n"

    md = []
    header = table_rows[0]
    header_cells = header.get("table_row", {}).get("cells", [])
    header_texts = [escape_table_cell(rich_text_to_md(c)) for c in header_cells]
    md.append(f"{indent_str}| {' | '.join(header_texts)} |\n")
    md.append(f"{indent_str}|{'|'.join([' --- '] * len(header_texts))}|\n")

    for row in table_rows[1:]:
        cells = row.get("table_row", {}).get("cells", [])
        cell_texts = [escape_table_cell(rich_text_to_md(c)) for c in cells]
        md.append(f"{indent_str}| {' | '.join(cell_texts)} |\n")

    md.append("\n")
    return "".join(md)


def render_blocks(blocks, output_dir, page_id, current_dir, indent=0, visited=None, depth=0, manifest=None, collected=None):
    """递归渲染一组 Notion blocks。"""
    md = []
    for block in blocks:
        md.append(render_block(block, output_dir, page_id, current_dir, indent, visited, depth, manifest, collected))
    return "".join(md)


def render_block(block, output_dir, page_id, current_dir, indent=0, visited=None, depth=0, manifest=None, collected=None):
    """递归渲染单个 Notion block。"""
    block_type = block.get("type")
    b_data = block.get(block_type, {})
    indent_str = "  " * indent

    if block_type == "child_page":
        child_id = block["id"]
        child_result = sync_page(child_id, output_dir, visited, depth + 1, manifest)
        if collected is not None:
            collected.update(child_result)
        child_info = child_result.get(child_id) or (manifest or {}).get(child_id)
        title = b_data.get("title", "untitled")
        if child_info:
            title = child_info.get("title", title)
            link = relative_markdown_link(child_info["file"], current_dir)
        else:
            safe_title = sanitize_filename(title)
            if len(safe_title) > 60:
                safe_title = safe_title[:60]
            expected_path = output_dir / "pages" / f"{safe_title}_{child_id[:8]}.md"
            link = relative_markdown_link(expected_path, current_dir)
        return f"{indent_str}- [{title}]({link})\n"

    if block_type == "child_database":
        db_id = block["id"]
        title = b_data.get("title", "untitled")
        db_result = sync_database(db_id, output_dir, depth + 1, manifest)
        if collected is not None:
            collected.update(db_result)
        return f"{indent_str}- Database: {title}\n"

    if block_type == "table":
        return render_table(block, indent)

    if block_type == "toggle":
        text = rich_text_to_md(b_data.get("rich_text", []))
        children = get_block_children(block["id"]) if block.get("has_children") else []
        inner = render_blocks(children, output_dir, page_id, current_dir, indent + 1, visited, depth, manifest, collected)
        return f"{indent_str}<details>\n{indent_str}<summary>{text}</summary>\n\n{inner}{indent_str}</details>\n\n"

    md = block_to_md(block, indent=indent, output_dir=output_dir, page_id=page_id, current_dir=current_dir)

    if block.get("has_children"):
        children = get_block_children(block["id"])
        md += render_blocks(children, output_dir, page_id, current_dir, indent + 1, visited, depth, manifest, collected)

    return md


# ============================================================
# 页面同步核心逻辑
# ============================================================

def sync_page(page_id, output_dir, visited=None, depth=0, manifest=None):
    """
    递归同步一个 Notion 页面及其所有子页面

    返回: dict {page_id: {"file": 文件路径, "title": 标题, "last_edited": 时间戳}}
    """
    if visited is None:
        visited = set()

    with VISITED_LOCK:
        if page_id in visited:
            return {}
        visited.add(page_id)

    result = {}

    # 获取页面信息
    page = notion_get(f"pages/{page_id}")
    if not page:
        return result

    title = get_page_title(page)
    safe_title = sanitize_filename(title)

    # 截断过长文件名
    if len(safe_title) > 60:
        safe_title = safe_title[:60]

    # 用 page_id 作为文件名核心（避免重名冲突）
    filename = f"{safe_title}_{page_id[:8]}.md"
    filepath = output_dir / "pages" / filename

    last_edited = page.get("last_edited_time", "")
    old_info = unchanged_page_info(page_id, last_edited, manifest)
    if old_info:
        print(f"{'  ' * depth}⏭️  {title}（未修改）")
        return {page_id: old_info}

    print(f"{'  ' * depth}📄 {title}")

    # 获取页面 icon/cover
    icon = page.get("icon") or {}
    icon_text = ""
    if icon.get("type") == "emoji":
        icon_text = icon["emoji"]

    # 构建 Markdown
    md_lines = []

    # YAML frontmatter
    created_time = page.get("created_time", "")
    url = page.get("url", "")
    md_lines.append("---\n")
    md_lines.append(f"title: {yaml_value(title)}\n")

    if icon_text:
        md_lines.append(f"icon: {yaml_value(icon_text)}\n")

    md_lines.append(f"notion_id: {yaml_value(page_id)}\n")
    md_lines.append(f"created: {yaml_value(created_time)}\n")
    md_lines.append(f"last_edited: {yaml_value(last_edited)}\n")
    md_lines.append(f"url: {yaml_value(url)}\n")
    md_lines.append("---\n\n")

    # 大标题
    md_lines.append(f"# {icon_text} {title}\n\n")

    # 获取子 block 内容
    blocks = get_block_children(page_id)
    if blocks:
        md_lines.append("\n")
        md_lines.append(render_blocks(blocks, output_dir, page_id, filepath.parent, visited=visited, depth=depth, manifest=manifest, collected=result))

    # 写文件
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        f.writelines(md_lines)

    result[page_id] = {
        "file": str(filepath.resolve()),
        "title": title,
        "last_edited": last_edited,
    }

    return result


def sync_database_row(row, db_title, db_dir, output_dir, depth=0, manifest=None):
    """同步 database 中的一条记录。"""
    result = {}
    indent = "  " * depth
    row_id = row["id"]
    row_title = get_page_title(row)
    safe_row_title = sanitize_filename(row_title)

    if len(safe_row_title) > 50:
        safe_row_title = safe_row_title[:50]

    filename = f"{safe_row_title}_{row_id[:8]}.md"
    filepath = db_dir / filename

    old_info = unchanged_page_info(row_id, row.get("last_edited_time", ""), manifest)
    if old_info:
        print(f"{indent}  ⏭️  {row_title}（未修改）")
        return {row_id: old_info}

    md_lines = []
    last_edited = row.get("last_edited_time", "")
    created = row.get("created_time", "")
    url = row.get("url", "")

    md_lines.append("---\n")
    md_lines.append(f"title: {yaml_value(row_title)}\n")
    md_lines.append(f"notion_id: {yaml_value(row_id)}\n")
    md_lines.append(f"database: {yaml_value(db_title)}\n")
    md_lines.append(f"created: {yaml_value(created)}\n")
    md_lines.append(f"last_edited: {yaml_value(last_edited)}\n")
    md_lines.append(f"url: {yaml_value(url)}\n")
    md_lines.append("---\n\n")

    md_lines.append(f"# {row_title}\n\n")

    properties = row.get("properties", {})
    md_lines.append("## 属性\n\n")
    for prop_name, prop_value in properties.items():
        prop_type = prop_value.get("type", "unknown")
        value = ""

        if prop_type == "title":
            value = rich_text_to_md(prop_value.get("title", []))
        elif prop_type == "rich_text":
            value = rich_text_to_md(prop_value.get("rich_text", []))
        elif prop_type == "number":
            value = str(prop_value.get("number", ""))
        elif prop_type == "select":
            select = prop_value.get("select")
            value = select["name"] if select else ""
        elif prop_type == "multi_select":
            items = prop_value.get("multi_select", [])
            value = ", ".join(s["name"] for s in items)
        elif prop_type == "date":
            date = prop_value.get("date")
            if date:
                value = f"{date.get('start', '')} → {date.get('end', '')}"
        elif prop_type == "checkbox":
            value = "✅" if prop_value.get("checkbox") else ""
        elif prop_type == "url":
            value = prop_value.get("url", "")
        elif prop_type == "email":
            value = prop_value.get("email", "")
        elif prop_type == "phone_number":
            value = prop_value.get("phone_number", "")
        elif prop_type == "status":
            status = prop_value.get("status")
            value = status["name"] if status else ""
        elif prop_type == "people":
            people = prop_value.get("people", [])
            value = ", ".join(
                p.get("name", p["id"][:8]) for p in people
            )
        elif prop_type == "files":
            files = prop_value.get("files", [])
            file_links = []
            for f in files:
                name = f.get("name", "file")
                if f.get("type") == "external":
                    file_links.append(f"[{name}]({f['external']['url']})")
                elif f.get("type") == "file":
                    file_links.append(f"[{name}]({f['file']['url']})")
            value = ", ".join(file_links)
        elif prop_type == "relation":
            relations = prop_value.get("relation", [])
            value = ", ".join(r["id"][:8] for r in relations)
        elif prop_type == "rollup":
            value = f"[rollup]"
        elif prop_type == "formula":
            formula = prop_value.get("formula", {})
            formula_type = formula.get("type")
            value = str(formula.get(formula_type, ""))

        if value:
            md_lines.append(f"- **{prop_name}**: {value}\n")

    blocks = get_block_children(row_id)
    if blocks:
        md_lines.append("\n## 内容\n\n")
        md_lines.append(render_blocks(blocks, output_dir, row_id, db_dir, visited=set(), depth=depth, manifest=manifest, collected=result))

    with open(filepath, "w", encoding="utf-8") as f:
        f.writelines(md_lines)

    result[row_id] = {
        "file": str(filepath.resolve()),
        "title": row_title,
        "last_edited": last_edited,
    }

    return result


def sync_database(db_id, output_dir, depth=0, manifest=None):
    """
    同步一个 Database 中的所有条目为独立的 Markdown 文件
    """
    result = {}
    indent = "  " * depth

    # 获取 Database 信息
    db = notion_get(f"databases/{db_id}")
    if not db:
        return result

    db_title = get_database_title(db)
    safe_db_title = sanitize_filename(db_title)
    db_dir = output_dir / "databases" / f"{safe_db_title}_{db_id[:8]}"
    db_dir.mkdir(parents=True, exist_ok=True)

    print(f"{indent}🗄️  Database: {db_title}")

    # 查询所有条目
    has_more = True
    start_cursor = None

    while has_more:
        body = {"page_size": 100}
        if start_cursor:
            body["start_cursor"] = start_cursor

        data = notion_post(f"databases/{db_id}/query", body)
        if not data:
            break

        rows = data.get("results", [])
        if rows:
            workers = max(1, min(MAX_WORKERS, len(rows)))
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [
                    executor.submit(sync_database_row, row, db_title, db_dir, output_dir, depth, manifest)
                    for row in rows
                ]
                for future in as_completed(futures):
                    try:
                        result.update(future.result())
                    except Exception as e:
                        print(f"{indent}  ⚠️ 数据库条目同步失败: {e}")

        has_more = data.get("has_more", False)
        start_cursor = data.get("next_cursor")

    return result


def sync_search_item(item, output_dir, visited, manifest=None):
    """同步 search 返回的一个页面或数据库。"""
    obj_type = item.get("object", "")
    obj_id = item["id"]

    if obj_type == "page":
        return sync_page(obj_id, output_dir, visited, manifest=manifest)

    if obj_type == "database":
        result = sync_database(obj_id, output_dir, manifest=manifest)
        with VISITED_LOCK:
            for rid in result:
                visited.add(rid)
        return result

    return {}


def get_index_page(output_dir, all_pages):
    """生成索引页 index.md"""
    md_lines = ["# Notion 同步索引\n\n"]
    md_lines.append(f"*同步时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*\n\n")
    md_lines.append(f"*页面总数: {len(all_pages)}*\n\n")

    md_lines.append("## 页面列表\n\n")

    # 按最后编辑时间排序（倒序）
    sorted_pages = sorted(
        all_pages.items(),
        key=lambda x: x[1].get("last_edited", ""),
        reverse=True,
    )

    for page_id, info in sorted_pages:
        rel_path = markdown_path(os.path.relpath(info["file"], str(output_dir)))
        md_lines.append(f"- [{info['title']}]({rel_path})  \n")

    index_path = output_dir / "index.md"
    with open(index_path, "w", encoding="utf-8") as f:
        f.writelines(md_lines)

    print(f"\n📑 索引文件: {index_path}")


# ============================================================
# 执行入口
# ============================================================

def main():
    global NOTION_TOKEN, OUTPUT_DIR, REQUEST_DELAY, REQUEST_TIMEOUT, MAX_WORKERS, DOWNLOAD_IMAGES

    config = load_config()

    NOTION_TOKEN = config.get("notion_token", NOTION_TOKEN)
    OUTPUT_DIR = config.get("output_dir", OUTPUT_DIR)
    REQUEST_DELAY = float(config.get("request_delay", REQUEST_DELAY))
    REQUEST_TIMEOUT = float(config.get("request_timeout", REQUEST_TIMEOUT))
    MAX_WORKERS = int(config.get("max_workers", MAX_WORKERS))
    DOWNLOAD_IMAGES = bool(config.get("download_images", DOWNLOAD_IMAGES))

    parser = argparse.ArgumentParser(description="Notion → 本地 Markdown 同步")
    parser.add_argument("--output", default=OUTPUT_DIR, help="输出目录")
    parser.add_argument("--token", default=NOTION_TOKEN, help="Notion API Token（覆盖配置文件）")
    parser.add_argument("--delay", type=float, default=REQUEST_DELAY, help="请求间隔（秒）")
    parser.add_argument("--workers", type=int, default=MAX_WORKERS, help="并发任务数量")
    args = parser.parse_args()

    NOTION_TOKEN = args.token
    update_headers()
    OUTPUT_DIR = args.output
    REQUEST_DELAY = args.delay
    MAX_WORKERS = max(1, args.workers)

    if not NOTION_TOKEN or NOTION_TOKEN == "ntn_你的Token":
        print("=" * 60)
        print("❌ 请先设置 Notion API Token！")
        print()
        print(f"  方式一：复制 {CONFIG_FILE.with_name('config.example.json')} 为 {CONFIG_FILE} 并填写 notion_token")
        print("  方式二：运行命令参数 --token \"你的Token\"")
        print()
        print("  获取 Token: https://www.notion.so/my-integrations")
        print("=" * 60)
        exit(1)

    output_dir = Path(OUTPUT_DIR).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest(output_dir)

    print("🚀 开始同步 Notion 工作区...")
    print(f"📂 输出目录: {output_dir.resolve()}")
    print(f"🧵 并发任务: {MAX_WORKERS}")
    print()

    # 搜索所有页面
    print("🔍 搜索工作区页面...")
    all_pages_data = get_all_pages()
    print(f"✅ 搜索到 {len(all_pages_data)} 个页面/数据库\n")

    # 不再区分顶级/非顶级，搜到什么就同步什么
    # 用全局 visited 避免同一条目被搜到又递归到
    all_results = {}
    visited = set()

    workers = max(1, min(MAX_WORKERS, len(all_pages_data) or 1))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(sync_search_item, item, output_dir, visited, manifest)
            for item in all_pages_data
        ]
        for future in as_completed(futures):
            try:
                all_results.update(future.result())
            except Exception as e:
                print(f"  ⚠️ 同步任务失败: {e}")

    print(f"\n{'=' * 40}")
    print(f"✅ 同步完成!")
    print(f"📄 页面/条目: {len(all_results)}")
    print(f"📁 输出目录: {output_dir.resolve()}")

    # 生成索引
    get_index_page(output_dir, all_results)
    save_manifest(output_dir, all_results)


if __name__ == "__main__":
    main()
