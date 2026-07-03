#!/usr/bin/env python3
"""
Notion → 本地 Markdown 同步脚本

使用方法：
  1. pip install requests
  2. 复制 conf/config.example.json 为 conf/config.json，并填入 Notion Token
  3. python sync_notion.py

每次运行都会重新拉取当前可同步内容，并刷新 index.md 为本次同步索引。
"""

import os
import json
import re
import sys
import time
import argparse
import threading
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse

try:
    import requests
except ImportError:
    print("请先安装 requests: pip install requests")
    exit(1)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

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

# 图片下载失败后的重试次数
IMAGE_DOWNLOAD_RETRIES = 2

# Notion API 版本
NOTION_VERSION = "2022-06-28"

# 本地镜像清单，用于增量同步和过期文件归档
MANIFEST_NAME = ".sync_manifest.json"
MANIFEST_VERSION = 1

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
PATH_LOCK = threading.Lock()
RESERVED_PATHS = set()
REMOTE_OBJECT_LOCK = threading.Lock()
REMOTE_OBJECT_AVAILABLE = {}

LAYOUT_CONTAINER_BLOCKS = {
    "column_list",
    "column",
    "breadcrumb",
    "table_of_contents",
    "synced_block",
    "template",
    "link_preview",
}


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


def is_notion_trashed(obj):
    """判断 Notion 对象是否已归档或在回收站中。"""
    return bool(obj.get("archived") or obj.get("in_trash"))


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

def is_downloadable_image_url(url):
    """只下载完整 HTTP(S) 图片链接；相对路径保留原链接。"""
    parsed = urlparse(url or "")
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def download_image(url, output_dir, page_id, block_id):
    """下载图片到本地 pages/images/ 目录，返回相对路径"""
    if not is_downloadable_image_url(url):
        return None

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

    last_error = None
    attempts = max(1, int(IMAGE_DOWNLOAD_RETRIES) + 1)
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    }
    for attempt in range(1, attempts + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            with open(filepath, "wb") as f:
                f.write(resp.content)
            return filepath
        except Exception as e:
            last_error = e
            if attempt < attempts:
                time.sleep(min(2 * attempt, 5))

    print(f"  ⚠️ 图片下载失败，保留原链接 ({url[:60]}...): {last_error}")
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


def rich_text_to_plain_text(rich_text):
    """将 rich_text 数组转换为纯文本，用于标题和文件名。"""
    return normalize_text_whitespace("".join(rt.get("plain_text", "") for rt in rich_text))


def normalize_text_whitespace(text):
    """Collapse Notion title whitespace so Markdown links and file paths stay single-line."""
    return re.sub(r"\s+", " ", str(text or "")).strip()


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
    name = normalize_text_whitespace(name)
    result = "".join(c if c not in invalid_chars else "_" for c in name)
    result = result.strip(". ")
    return result or "untitled"


def truncate_filename(name, max_length):
    """按文件名长度截断，同时保留至少一个可用字符。"""
    return (name[:max_length]).strip(". ") or "untitled"


def reserve_unique_path(base_path, entity_id=None, manifest=None):
    """保留本次运行中的唯一路径；重名时追加编号。"""
    base_path = Path(base_path)

    with PATH_LOCK:
        index = 1
        while True:
            if index == 1:
                candidate = base_path
            else:
                candidate = base_path.with_name(f"{base_path.stem} ({index}){base_path.suffix}")

            resolved = str(candidate.resolve())
            if resolved not in RESERVED_PATHS:
                RESERVED_PATHS.add(resolved)
                return candidate

            index += 1


def load_sync_manifest(output_dir):
    """读取上一次镜像同步清单。"""
    manifest_path = output_dir / MANIFEST_NAME
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"  ⚠️ 同步清单读取失败，将执行较完整同步: {e}")
        return {}

    if isinstance(data, dict) and isinstance(data.get("items"), dict):
        return data["items"]
    if isinstance(data, dict):
        return data
    return {}


def save_sync_manifest(output_dir, items):
    """保存本次镜像同步清单。"""
    manifest_path = output_dir / MANIFEST_NAME
    payload = {
        "version": MANIFEST_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "items": items,
    }
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def file_exists_for_entry(entry):
    """判断 manifest 条目对应的文件或目录是否仍在本地。"""
    file_path = entry.get("file")
    return bool(file_path and Path(file_path).exists())


def remote_object_available(entry):
    """Check that an unchanged page/database still exists and is not in trash."""
    obj_type = entry.get("object_type")
    obj_id = entry.get("id")
    if not obj_id or obj_type not in ("page", "database"):
        return True

    cache_key = (obj_type, obj_id)
    with REMOTE_OBJECT_LOCK:
        if cache_key in REMOTE_OBJECT_AVAILABLE:
            return REMOTE_OBJECT_AVAILABLE[cache_key]

    path = f"databases/{obj_id}" if obj_type == "database" else f"pages/{obj_id}"
    obj = notion_get(path)
    available = bool(obj and not is_notion_trashed(obj))

    with REMOTE_OBJECT_LOCK:
        REMOTE_OBJECT_AVAILABLE[cache_key] = available
    return available


def can_reuse_entry(entry, old_items, force_rewrite_ids, verify_remote=False):
    """判断本次是否可以复用旧 Markdown，不重新拉取 block 内容。"""
    old_entry = (old_items or {}).get(entry["id"])
    if not old_entry or entry["id"] in force_rewrite_ids:
        return False
    if old_entry.get("last_edited") != entry.get("last_edited"):
        return False
    old_file = old_entry.get("file")
    if not old_file:
        return False
    if Path(old_file).resolve() != Path(entry["file"]).resolve():
        return False
    if not Path(old_file).exists():
        return False
    if verify_remote and not remote_object_available(entry):
        return False
    return True


def object_parent_info(obj):
    """Return (parent_type, parent_id) from a Notion object."""
    parent = obj.get("parent") or {}
    parent_type = parent.get("type")
    if not parent_type:
        return None, None
    return parent_type, parent.get(parent_type)


def resolve_block_parent(block_id, block_parent_cache):
    """Resolve a block parent chain to the nearest page/database/workspace parent."""
    current_id = block_id
    seen = []

    while current_id:
        if current_id in block_parent_cache:
            resolved = block_parent_cache[current_id]
            for seen_id in seen:
                block_parent_cache[seen_id] = resolved
            return resolved

        if current_id in seen:
            resolved = (None, None)
            break

        seen.append(current_id)
        block = notion_get(f"blocks/{current_id}")
        if not block:
            resolved = (None, None)
            break

        parent_type, parent_id = object_parent_info(block)
        if parent_type == "block_id" and parent_id:
            current_id = parent_id
            continue

        resolved = (parent_type, parent_id)
        break
    else:
        resolved = (None, None)

    for seen_id in seen:
        block_parent_cache[seen_id] = resolved
    return resolved


def search_item_info(item, block_parent_cache=None, from_search=True):
    """把 Notion search 结果规范化为路径规划所需的元数据。"""
    if block_parent_cache is None:
        block_parent_cache = {}

    obj_id = item["id"]
    obj_type = item.get("object", "")
    raw_parent_type, raw_parent_id = object_parent_info(item)
    parent_type = raw_parent_type
    parent_id = raw_parent_id

    if parent_type == "block_id" and parent_id:
        resolved_type, resolved_id = resolve_block_parent(parent_id, block_parent_cache)
        if resolved_type in ("page_id", "database_id", "workspace"):
            parent_type = resolved_type
            parent_id = resolved_id
    title = get_database_title(item) if obj_type == "database" else get_page_title(item)
    return {
        "id": obj_id,
        "object_type": obj_type,
        "title": title,
        "parent_id": parent_id,
        "parent_type": parent_type,
        "raw_parent_id": raw_parent_id,
        "raw_parent_type": raw_parent_type,
        "from_search": from_search,
        "last_edited": item.get("last_edited_time", ""),
        "url": item.get("url", ""),
    }


def build_path_plan(output_dir, search_items):
    """基于当前 Notion 可见对象全集，计算本次同步的目标路径。"""
    RESERVED_PATHS.clear()
    now = datetime.now().isoformat(timespec="seconds")
    block_parent_cache = {}
    item_map = {
        item["id"]: search_item_info(item, block_parent_cache)
        for item in search_items
    }
    planned = {}
    resolving = set()
    missing_parent_cache = set()
    trashed_parent_ids = set()

    def load_missing_parent(parent_type, parent_id):
        if not parent_id or parent_id in item_map:
            return
        if parent_type not in ("page_id", "database_id"):
            return
        cache_key = (parent_type, parent_id)
        if cache_key in missing_parent_cache:
            return
        missing_parent_cache.add(cache_key)

        path = f"pages/{parent_id}" if parent_type == "page_id" else f"databases/{parent_id}"
        parent_obj = notion_get(path)
        if parent_obj and is_notion_trashed(parent_obj):
            trashed_parent_ids.add(parent_id)
            return
        if not parent_obj:
            return
        item_map[parent_id] = search_item_info(parent_obj, block_parent_cache, from_search=False)

    def planned_entry(obj_id):
        if obj_id in planned:
            return planned[obj_id]
        info = item_map.get(obj_id)
        if not info:
            return None
        if obj_id in resolving:
            return None

        resolving.add(obj_id)
        title = info["title"]
        parent_id = info.get("parent_id")
        parent_type = info.get("parent_type")
        object_type = info.get("object_type")

        load_missing_parent(parent_type, parent_id)
        if parent_id in trashed_parent_ids:
            resolving.remove(obj_id)
            return None

        if object_type == "database":
            safe_title = truncate_filename(sanitize_filename(title), 60)
            if parent_type == "page_id" and parent_id in item_map:
                parent_entry = planned_entry(parent_id)
                parent_dir = Path(parent_entry["file"]).with_suffix("") if parent_entry else output_dir / "_orphans"
                base_path = parent_dir / safe_title
            elif parent_type == "workspace":
                base_path = output_dir / "databases" / safe_title
            else:
                base_path = output_dir / "_orphans" / safe_title
            target_path = reserve_unique_path(base_path)
        else:
            max_length = 50 if parent_type == "database_id" else 60
            safe_title = truncate_filename(sanitize_filename(title), max_length)
            if parent_type == "page_id" and parent_id in item_map:
                parent_entry = planned_entry(parent_id)
                parent_dir = Path(parent_entry["file"]).with_suffix("") if parent_entry else output_dir / "_orphans"
                base_path = parent_dir / f"{safe_title}.md"
            elif parent_type == "database_id" and parent_id in item_map:
                parent_entry = planned_entry(parent_id)
                parent_dir = Path(parent_entry["file"]) if parent_entry else output_dir / "_orphans"
                base_path = parent_dir / f"{safe_title}.md"
            elif parent_type == "workspace":
                base_path = output_dir / "pages" / f"{safe_title}.md"
            else:
                base_path = output_dir / "_orphans" / f"{safe_title}.md"
            target_path = reserve_unique_path(base_path)

        resolving.remove(obj_id)
        rel_path = markdown_path(os.path.relpath(str(target_path), str(output_dir)))
        entry = {
            "id": obj_id,
            "object_type": object_type,
            "title": title,
            "parent_id": parent_id,
            "parent_type": parent_type,
            "raw_parent_id": info.get("raw_parent_id"),
            "raw_parent_type": info.get("raw_parent_type"),
            "from_search": info.get("from_search", True),
            "last_edited": info.get("last_edited", ""),
            "file": str(target_path.resolve()),
            "path": rel_path,
            "url": info.get("url", ""),
            "seen_at": now,
        }
        planned[obj_id] = entry
        return entry

    for obj_id in sorted(item_map):
        planned_entry(obj_id)

    return planned


def planned_child_entry(obj_id, title, object_type, parent_id, parent_type, output_dir, planned, parent_dir):
    """为 search 未返回但递归发现的子对象创建兜底路径规划。"""
    if obj_id in planned:
        return planned[obj_id]

    now = datetime.now().isoformat(timespec="seconds")
    if object_type == "database":
        safe_title = truncate_filename(sanitize_filename(title), 60)
        target_path = reserve_unique_path(Path(parent_dir) / safe_title)
    else:
        max_length = 50 if parent_type == "database_id" else 60
        safe_title = truncate_filename(sanitize_filename(title), max_length)
        target_path = reserve_unique_path(Path(parent_dir) / f"{safe_title}.md")

    entry = {
        "id": obj_id,
        "object_type": object_type,
        "title": title,
        "parent_id": parent_id,
        "parent_type": parent_type,
        "last_edited": "",
        "file": str(target_path.resolve()),
        "path": markdown_path(os.path.relpath(str(target_path), str(output_dir))),
        "url": "",
        "seen_at": now,
    }
    planned[obj_id] = entry
    return entry


def relayout_planned_child(obj_id, title, object_type, parent_id, parent_type, output_dir, planned, parent_dir):
    """用递归上下文修正 search 阶段无法定位的子对象路径。"""
    entry = planned.get(obj_id)
    if entry:
        entry_path = Path(entry.get("file", "")).resolve() if entry.get("file") else None
        parent_path = Path(parent_dir).resolve()
        if entry_path and "_orphans/" not in entry.get("path", "") and path_is_relative_to(entry_path, parent_path):
            return entry

    if entry:
        old_file = entry.get("file")
        if old_file:
            RESERVED_PATHS.discard(str(Path(old_file).resolve()))
    else:
        entry = {}

    now = datetime.now().isoformat(timespec="seconds")
    if object_type == "database":
        safe_title = truncate_filename(sanitize_filename(entry.get("title") or title), 60)
        target_path = reserve_unique_path(Path(parent_dir) / safe_title)
    else:
        max_length = 50 if parent_type == "database_id" else 60
        safe_title = truncate_filename(sanitize_filename(entry.get("title") or title), max_length)
        target_path = reserve_unique_path(Path(parent_dir) / f"{safe_title}.md")

    entry.update({
        "id": obj_id,
        "object_type": object_type,
        "title": entry.get("title") or title,
        "parent_id": parent_id,
        "parent_type": parent_type,
        "file": str(target_path.resolve()),
        "path": markdown_path(os.path.relpath(str(target_path), str(output_dir))),
        "seen_at": now,
    })
    planned[obj_id] = entry
    return entry


def changed_or_moved_ids(planned, old_items):
    """找出需要重写的对象，以及因子节点变动需刷新链接的祖先页面。"""
    changed = set()
    for obj_id, entry in planned.items():
        old_entry = (old_items or {}).get(obj_id)
        if not old_entry:
            changed.add(obj_id)
            continue
        if old_entry.get("last_edited") != entry.get("last_edited"):
            changed.add(obj_id)
            continue
        old_file = old_entry.get("file")
        if not old_file or Path(old_file).resolve() != Path(entry["file"]).resolve():
            changed.add(obj_id)
            continue
        if not file_exists_for_entry(old_entry):
            changed.add(obj_id)

    force = set(changed)
    for obj_id in list(changed):
        parent_id = planned.get(obj_id, {}).get("parent_id")
        while parent_id and parent_id in planned:
            force.add(parent_id)
            parent_id = planned[parent_id].get("parent_id")
    return force


def unique_stale_destination(dest):
    """避免 _stale 归档路径撞名。"""
    dest = Path(dest)
    if not dest.exists():
        return dest
    index = 2
    while True:
        candidate = dest.with_name(f"{dest.stem} ({index}){dest.suffix}")
        if not candidate.exists():
            return candidate
        index += 1


def path_is_relative_to(path, parent):
    """兼容 Python 3.8 的 Path.is_relative_to。"""
    try:
        Path(path).resolve().relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False


def archive_stale_entries(output_dir, old_items, current_items):
    """把旧 manifest 管理但本次不再使用的文件移入 _stale。"""
    if not old_items:
        return

    current_paths = {
        str(Path(entry["file"]).resolve())
        for entry in current_items.values()
        if entry.get("file")
    }
    candidates = []
    for obj_id, old_entry in old_items.items():
        old_file = old_entry.get("file")
        if not old_file:
            continue
        old_path = Path(old_file).resolve()
        if not old_path.exists():
            continue
        current_entry = current_items.get(obj_id)
        if current_entry and Path(current_entry["file"]).resolve() == old_path:
            continue
        if str(old_path) in current_paths:
            continue
        candidates.append(old_path)

    if not candidates:
        return

    managed_paths = {
        Path(entry["file"]).resolve()
        for entry in old_items.values()
        if entry.get("file")
    }
    current_path_objs = {Path(path) for path in current_paths}

    def directory_is_fully_managed(path):
        for child in path.rglob("*"):
            if child.is_file() and child.resolve() not in managed_paths:
                return False
        return True

    stale_root = output_dir / "_stale" / datetime.now().strftime("%Y%m%d-%H%M%S")
    moved_roots = []
    for old_path in sorted(set(candidates), key=lambda p: len(p.parts)):
        if any(old_path == root or path_is_relative_to(old_path, root) for root in moved_roots):
            continue
        if any(path_is_relative_to(current_path, old_path) for current_path in current_path_objs):
            continue
        if old_path.is_dir() and not directory_is_fully_managed(old_path):
            continue
        try:
            rel_path = old_path.relative_to(output_dir)
        except ValueError:
            print(f"  ⚠️ 跳过输出目录外的旧文件: {old_path}")
            continue
        dest = unique_stale_destination(stale_root / rel_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(old_path), str(dest))
            moved_roots.append(old_path)
            print(f"  🗄️  过期归档: {old_path} -> {dest}")
        except Exception as e:
            print(f"  ⚠️ 过期文件归档失败: {old_path} ({e})")


def get_page_title(page):
    """从 page 对象中提取标题"""
    properties = page.get("properties", {})

    # 尝试几种常见的 title 字段名
    for title_field in ["title", "Name", "名前", "名称"]:
        prop = properties.get(title_field, {})
        if prop:
            title_data = prop.get("title", [])
            if title_data:
                return rich_text_to_plain_text(title_data)

    # 对于 database item，尝试第一个 title 类型的属性
    for prop_name, prop_data in properties.items():
        if prop_data.get("type") == "title":
            title_list = prop_data.get("title", [])
            if title_list:
                return rich_text_to_plain_text(title_list)

    return "Untitled"


def get_database_title(database):
    """从 database 对象中提取标题"""
    title_data = database.get("title", [])
    if title_data:
        return rich_text_to_plain_text(title_data)
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


def render_blocks(blocks, output_dir, page_id, current_dir, indent=0, visited=None, depth=0, old_items=None, planned=None, force_rewrite_ids=None, collected=None, child_parent_dir=None):
    """递归渲染一组 Notion blocks。"""
    md = []
    for block in blocks:
        md.append(render_block(block, output_dir, page_id, current_dir, indent, visited, depth, old_items, planned, force_rewrite_ids, collected, child_parent_dir))
    return "".join(md)


def render_block(block, output_dir, page_id, current_dir, indent=0, visited=None, depth=0, old_items=None, planned=None, force_rewrite_ids=None, collected=None, child_parent_dir=None):
    """递归渲染单个 Notion block。"""
    block_type = block.get("type")
    b_data = block.get(block_type, {})
    indent_str = "  " * indent
    child_parent_dir = child_parent_dir or current_dir
    planned = planned if planned is not None else {}
    force_rewrite_ids = force_rewrite_ids or set()

    if block_type == "child_page":
        child_id = block["id"]
        title = normalize_text_whitespace(b_data.get("title", "untitled")) or "untitled"
        relayout_planned_child(child_id, title, "page", page_id, "page_id", output_dir, planned, child_parent_dir)
        child_result = sync_page(child_id, output_dir, visited, old_items, planned, force_rewrite_ids, depth + 1)
        if collected is not None:
            collected.update(child_result)
        child_info = child_result.get(child_id) or planned.get(child_id)
        if child_info:
            title = child_info.get("title", title)
            link = relative_markdown_link(child_info["file"], current_dir)
        else:
            safe_title = sanitize_filename(title)
            safe_title = truncate_filename(safe_title, 60)
            expected_path = child_parent_dir / f"{safe_title}.md"
            link = relative_markdown_link(expected_path, current_dir)
        return f"{indent_str}- [{title}]({link})\n"

    if block_type == "child_database":
        db_id = block["id"]
        title = normalize_text_whitespace(b_data.get("title", "untitled")) or "untitled"
        relayout_planned_child(db_id, title, "database", page_id, "page_id", output_dir, planned, child_parent_dir)
        db_result = sync_database(db_id, output_dir, visited, old_items, planned, force_rewrite_ids, depth + 1)
        if collected is not None:
            collected.update(db_result)
        db_info = db_result.get(db_id) or planned.get(db_id)
        if db_info:
            link = relative_markdown_link(db_info["file"], current_dir)
            return f"{indent_str}- [Database: {title}]({link})\n"
        return f"{indent_str}- Database: {title}\n"

    if block_type == "table":
        return render_table(block, indent)

    if block_type == "toggle":
        text = rich_text_to_md(b_data.get("rich_text", []))
        children = get_block_children(block["id"]) if block.get("has_children") else []
        inner = render_blocks(children, output_dir, page_id, current_dir, indent + 1, visited, depth, old_items, planned, force_rewrite_ids, collected, child_parent_dir)
        return f"{indent_str}<details>\n{indent_str}<summary>{text}</summary>\n\n{inner}{indent_str}</details>\n\n"

    md = block_to_md(block, indent=indent, output_dir=output_dir, page_id=page_id, current_dir=current_dir)

    if block.get("has_children"):
        children = get_block_children(block["id"])
        child_indent = indent if block_type in LAYOUT_CONTAINER_BLOCKS else indent + 1
        md += render_blocks(children, output_dir, page_id, current_dir, child_indent, visited, depth, old_items, planned, force_rewrite_ids, collected, child_parent_dir)

    return md


# ============================================================
# 页面同步核心逻辑
# ============================================================

def sync_page(page_id, output_dir, visited=None, old_items=None, planned=None, force_rewrite_ids=None, depth=0):
    """
    递归同步一个 Notion 页面及其所有子页面

    返回: dict {page_id: {"file": 文件路径, "title": 标题, "last_edited": 时间戳}}
    """
    if visited is None:
        visited = set()
    old_items = old_items or {}
    planned = planned if planned is not None else {}
    force_rewrite_ids = force_rewrite_ids or set()

    with VISITED_LOCK:
        if page_id in visited:
            return {}
        visited.add(page_id)

    result = {}
    entry = planned.get(page_id)
    if not entry:
        entry = planned_child_entry(page_id, "Untitled", "page", None, None, output_dir, planned, output_dir / "_orphans")

    if can_reuse_entry(entry, old_items, force_rewrite_ids, verify_remote=True):
        print(f"{'  ' * depth}⏭️  {entry['title']}（未修改）")
        return {page_id: entry}

    # 获取页面信息
    page = notion_get(f"pages/{page_id}")
    if not page:
        return result
    if is_notion_trashed(page):
        print(f"{'  ' * depth}🚫 跳过回收站页面: {page_id}")
        return result

    title = entry.get("title") or get_page_title(page)
    filepath = Path(entry["file"])
    child_dir = filepath.with_suffix("")

    last_edited = entry.get("last_edited") or page.get("last_edited_time", "")

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
        md_lines.append(render_blocks(blocks, output_dir, page_id, filepath.parent, visited=visited, depth=depth, old_items=old_items, planned=planned, force_rewrite_ids=force_rewrite_ids, collected=result, child_parent_dir=child_dir))

    # 写文件
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        f.writelines(md_lines)

    entry.update({
        "title": title,
        "last_edited": last_edited,
        "url": page.get("url", entry.get("url", "")),
        "file": str(filepath.resolve()),
        "path": markdown_path(os.path.relpath(str(filepath), str(output_dir))),
    })
    result[page_id] = entry

    return result


def sync_database_row(row, db_title, db_dir, output_dir, visited=None, old_items=None, planned=None, force_rewrite_ids=None, depth=0):
    """同步 database 中的一条记录。"""
    result = {}
    indent = "  " * depth
    old_items = old_items or {}
    planned = planned if planned is not None else {}
    force_rewrite_ids = force_rewrite_ids or set()
    visited = visited if visited is not None else set()
    row_id = row["id"]
    if is_notion_trashed(row):
        print(f"{indent}  🚫 跳过回收站条目: {row_id}")
        return result

    row_title = get_page_title(row)
    parent_id = search_item_parent_id(row)
    entry = relayout_planned_child(row_id, row_title, "page", parent_id, "database_id", output_dir, planned, db_dir)
    filepath = Path(entry["file"])
    child_dir = filepath.with_suffix("")

    if can_reuse_entry(entry, old_items, force_rewrite_ids, verify_remote=True):
        print(f"{indent}  ⏭️  {entry['title']}（未修改）")
        return {row_id: entry}

    md_lines = []
    last_edited = entry.get("last_edited") or row.get("last_edited_time", "")
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
        md_lines.append(render_blocks(blocks, output_dir, row_id, db_dir, visited=visited, depth=depth, old_items=old_items, planned=planned, force_rewrite_ids=force_rewrite_ids, collected=result, child_parent_dir=child_dir))

    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        f.writelines(md_lines)

    entry.update({
        "title": row_title,
        "last_edited": last_edited,
        "url": row.get("url", entry.get("url", "")),
        "file": str(filepath.resolve()),
        "path": markdown_path(os.path.relpath(str(filepath), str(output_dir))),
    })
    result[row_id] = entry

    return result


def sync_database(db_id, output_dir, visited=None, old_items=None, planned=None, force_rewrite_ids=None, depth=0):
    """
    同步一个 Database 中的所有条目为独立的 Markdown 文件
    """
    result = {}
    indent = "  " * depth
    old_items = old_items or {}
    planned = planned if planned is not None else {}
    force_rewrite_ids = force_rewrite_ids or set()
    visited = visited if visited is not None else set()

    with VISITED_LOCK:
        if db_id in visited:
            return {}
        visited.add(db_id)

    entry = planned.get(db_id)
    if not entry:
        db = notion_get(f"databases/{db_id}")
        if not db or is_notion_trashed(db):
            return result
        db_title = get_database_title(db)
        entry = planned_child_entry(db_id, db_title, "database", None, None, output_dir, planned, output_dir / "_orphans")
        entry["last_edited"] = db.get("last_edited_time", "")
        entry["url"] = db.get("url", "")
    db_title = entry["title"]
    db_dir = Path(entry["file"])
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
                    executor.submit(sync_database_row, row, db_title, db_dir, output_dir, visited, old_items, planned, force_rewrite_ids, depth)
                    for row in rows
                ]
                for future in as_completed(futures):
                    try:
                        result.update(future.result())
                    except Exception as e:
                        print(f"{indent}  ⚠️ 数据库条目同步失败: {e}")

        has_more = data.get("has_more", False)
        start_cursor = data.get("next_cursor")

    result[db_id] = entry

    return result


def sync_search_item(item, output_dir, visited, old_items=None, planned=None, force_rewrite_ids=None):
    """同步 search 返回的一个页面或数据库。"""
    obj_type = item.get("object", "")
    obj_id = item["id"]

    if obj_type == "page":
        return sync_page(obj_id, output_dir, visited, old_items, planned, force_rewrite_ids)

    if obj_type == "database":
        result = sync_database(obj_id, output_dir, visited, old_items, planned, force_rewrite_ids)
        with VISITED_LOCK:
            for rid in result:
                visited.add(rid)
        return result

    return {}


def should_sync_planned_entry(entry, planned, planned_databases):
    """Return True when this planned object must be an explicit sync entry."""
    obj_type = entry.get("object_type")
    parent_type = entry.get("parent_type")
    parent_id = entry.get("parent_id")
    parent_entry = planned.get(parent_id)

    if obj_type == "database":
        return entry.get("from_search", True)
    if parent_type == "page_id":
        if parent_entry and not parent_entry.get("from_search", True):
            return True
        return parent_id not in planned
    if parent_type == "database_id":
        if parent_entry and not parent_entry.get("from_search", True):
            return True
        return parent_id not in planned_databases
    return True


def sync_planned_entry(entry, output_dir, visited, old_items=None, planned=None, force_rewrite_ids=None):
    """Sync a planned object without relying on raw search parent metadata."""
    obj_type = entry.get("object_type", "")
    obj_id = entry["id"]

    if obj_type == "page":
        return sync_page(obj_id, output_dir, visited, old_items, planned, force_rewrite_ids)

    if obj_type == "database":
        result = sync_database(obj_id, output_dir, visited, old_items, planned, force_rewrite_ids)
        with VISITED_LOCK:
            for rid in result:
                visited.add(rid)
        return result

    return {}


def search_item_parent_id(item):
    """从 Notion search 结果中提取父对象 ID。"""
    parent = item.get("parent") or {}
    parent_type = parent.get("type")
    if not parent_type:
        return None
    return parent.get(parent_type)


def is_nested_search_item(item, search_ids):
    """判断 search 结果是否已经会被父页面或父数据库递归同步。"""
    parent = item.get("parent") or {}
    parent_type = parent.get("type")
    if parent_type in ("page_id", "database_id"):
        return True

    parent_id = search_item_parent_id(item)
    return bool(parent_id and parent_id in search_ids)


def build_current_items(planned, all_results, old_items, force_rewrite_ids):
    """Build the manifest/index set, excluding trashed objects and children of skipped parents."""
    current_items = {}
    resolving = set()

    def include_entry(obj_id):
        if obj_id in current_items:
            return True
        if obj_id in resolving:
            return False

        entry = planned.get(obj_id)
        if not entry:
            return False

        resolving.add(obj_id)
        parent_type = entry.get("parent_type")
        parent_id = entry.get("parent_id")
        if parent_type in ("page_id", "database_id") and parent_id in planned:
            if not include_entry(parent_id):
                resolving.remove(obj_id)
                return False

        is_current = (
            obj_id in all_results
            or (
                not entry.get("from_search", True)
                and remote_object_available(entry)
            )
            or can_reuse_entry(entry, old_items, force_rewrite_ids, verify_remote=True)
        )
        if is_current:
            current_items[obj_id] = entry

        resolving.remove(obj_id)
        return is_current

    for obj_id in planned:
        include_entry(obj_id)

    return current_items


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
    global NOTION_TOKEN, OUTPUT_DIR, REQUEST_DELAY, REQUEST_TIMEOUT, MAX_WORKERS, DOWNLOAD_IMAGES, IMAGE_DOWNLOAD_RETRIES

    config = load_config()

    NOTION_TOKEN = config.get("notion_token", NOTION_TOKEN)
    OUTPUT_DIR = config.get("output_dir", OUTPUT_DIR)
    REQUEST_DELAY = float(config.get("request_delay", REQUEST_DELAY))
    REQUEST_TIMEOUT = float(config.get("request_timeout", REQUEST_TIMEOUT))
    MAX_WORKERS = int(config.get("max_workers", MAX_WORKERS))
    DOWNLOAD_IMAGES = bool(config.get("download_images", DOWNLOAD_IMAGES))
    IMAGE_DOWNLOAD_RETRIES = int(config.get("image_download_retries", IMAGE_DOWNLOAD_RETRIES))

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
    old_items = load_sync_manifest(output_dir)

    print("🚀 开始同步 Notion 工作区...")
    print(f"📂 输出目录: {output_dir.resolve()}")
    print(f"🧵 并发任务: {MAX_WORKERS}")
    print()

    # 搜索所有页面
    print("🔍 搜索工作区页面...")
    all_pages_data = get_all_pages()
    trashed_count = sum(1 for item in all_pages_data if is_notion_trashed(item))
    all_pages_data = [
        item for item in all_pages_data
        if not is_notion_trashed(item)
    ]
    if trashed_count:
        print(f"🚫 已过滤 {trashed_count} 个回收站/归档对象")
    print(f"✅ 搜索到 {len(all_pages_data)} 个可同步页面/数据库\n")

    planned = build_path_plan(output_dir, all_pages_data)
    force_rewrite_ids = changed_or_moved_ids(planned, old_items)
    print(f"🧭 路径规划: {len(planned)} 个对象，需刷新: {len(force_rewrite_ids)}")

    all_results = {}
    visited = set()
    planned_databases = {
        obj_id for obj_id, entry in planned.items()
        if entry.get("object_type") == "database"
    }
    sync_items = [
        entry for entry in planned.values()
        if should_sync_planned_entry(entry, planned, planned_databases)
    ]

    print(f"🌳 本次调度: {len(sync_items)} 个入口对象")

    workers = max(1, min(MAX_WORKERS, len(sync_items) or 1))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(sync_planned_entry, item, output_dir, visited, old_items, planned, force_rewrite_ids)
            for item in sync_items
        ]
        for future in as_completed(futures):
            try:
                result = future.result()
                all_results.update(result)
                with VISITED_LOCK:
                    visited.update(result.keys())
            except Exception as e:
                print(f"  ⚠️ 同步任务失败: {e}")

    current_items = build_current_items(planned, all_results, old_items, force_rewrite_ids)
    archive_stale_entries(output_dir, old_items, current_items)
    save_sync_manifest(output_dir, current_items)

    print(f"\n{'=' * 40}")
    print(f"✅ 同步完成!")
    print(f"📄 页面/条目: {len(current_items)}")
    print(f"📁 输出目录: {output_dir.resolve()}")

    # 生成索引
    get_index_page(output_dir, current_items)


if __name__ == "__main__":
    main()
