# Notion Sync

[中文](README.md)

Sync Notion workspace content to local Markdown files while preserving the same note structure as in Notion. Ideal for personal knowledge base backups, Git version control, static site content sources, or offline search.

## Features

- Sync Notion pages as Markdown files
- Support exporting sub-pages, nested blocks, toggles, tables, code blocks, quotes, to-dos, images, and other common content types
- Download images to a local `images/` directory
- Mirror-style incremental sync using `.sync_manifest.json` — unchanged pages reuse local Markdown files
- Refreshes `index.md` with the latest mirror index on every run
- Automatically skips archived or trashed pages, databases, and database entries
- Automatically sanitizes filenames: whitespace replaced with `_`, common CJK/fullwidth punctuation converted to English punctuation
- Concurrent sync support with adjustable `--workers`
- Auto-generates `index.md` index file
- Gracefully skips objects with insufficient permissions, deleted items, or unsupported API objects without interrupting the overall sync

## Requirements

- Python 3.8+
- requests
- Notion Integration Token

Install dependencies:

```bash
pip install requests
```

Using a virtual environment is recommended:

```bash
python3 -m venv .venv
.venv/bin/pip install requests
```

## Configuration

Copy the example configuration:

```bash
cp conf/config.example.json conf/config.json
```

Edit `conf/config.json` and fill in your Notion Token:

```json
{
  "notion_token": "ntn_YourToken",
  "output_dir": "./notion_sync",
  "request_delay": 0.1,
  "request_timeout": 30,
  "max_workers": 4,
  "download_images": true,
  "image_download_retries": 2
}
```

Configuration fields:

| Field | Description |
| --- | --- |
| `notion_token` | Notion Integration Token |
| `output_dir` | Markdown output directory |
| `request_delay` | Delay between requests, in seconds |
| `request_timeout` | Request timeout, in seconds |
| `max_workers` | Number of concurrent workers |
| `download_images` | Whether to download images locally |
| `image_download_retries` | Retry count for failed image downloads |

`conf/config.json` contains your private token and is already ignored by `.gitignore`. Only commit `conf/config.example.json` when publishing to GitHub.

## Notion Setup

1. Open [Notion Integrations](https://www.notion.so/my-integrations)
2. Create an Internal Integration
3. Copy the Integration Token
4. Click `...` in the top-right corner of your Notion page
5. Select `Add connections`
6. Add the Integration you just created

To sync your entire workspace, add the Integration to the top-level page you want to use as the root. Notion permissions are hierarchical — pages or databases without explicit authorization cannot be read via the API.

## Usage

#### Start syncing:

![image-20260711160432073](E:\MyPriveCloud\mySoftware\sync_notion\README\image-20260711160432073.png)

#### Sync complete:

The tool automatically detects and skips notes that are identical to the local copies. Notes deleted in Notion are archived to a separate local folder.

![image-20260711160545215](E:\MyPriveCloud\mySoftware\sync_notion\README\image-20260711160545215.png)

Basic usage:

```bash
python sync_notion.py
```

Specify an output directory:

```bash
python sync_notion.py --output ./notion_sync
```

Use concurrent sync:

```bash
python sync_notion.py --workers 4
```

If you hit Notion's rate limit, reduce concurrency or increase the request delay:

```bash
python sync_notion.py --workers 3 --delay 0.2
```

Temporarily override the token from the config file:

```bash
python sync_notion.py --token "Your_Notion_Token"
```

View all options:

```bash
python sync_notion.py --help
```

## CLI Arguments

| Argument | Default | Description |
| --- | --- | --- |
| `--output` | `output_dir` from config | Markdown output directory |
| `--token` | `notion_token` from config | Temporarily override Notion API Token |
| `--delay` | `request_delay` from config | Delay between requests, in seconds |
| `--workers` | `max_workers` from config | Number of concurrent workers |

## Output Structure

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

- `index.md` — Page index for the current sync
- `.sync_manifest.json` — Mirror-style incremental sync manifest used to determine which pages can be reused
- `images/` — Locally downloaded images
- `pages/` — Markdown files exported from regular pages. If a page contains a database, a same-named `.md` entry file is generated with database entries stored in a same-named subdirectory
- `databases/` — Entry Markdown files and entry subdirectories for workspace root-level databases
- `_orphans/` — Fallback output directory when the parent cannot be located via API recursion
- `_stale/` — Archive for Markdown files or database directories from the previous manifest that are no longer synced in this run

Pages and databases use a consistent local organization:

```text
pages/Personal_Files.md
pages/Personal_Files/Journal·Weekly·Notes.md
pages/Personal_Files/Journal·Weekly·Notes/First_Entry.md
```

Here `Journal·Weekly·Notes.md` is the database entry file containing the database title, Notion metadata, and links to entries; the `Journal·Weekly·Notes/` directory holds each record within that database.

## Filename Sanitization

The sync script sanitizes new file and folder names:

- Spaces, fullwidth spaces, newlines, and other whitespace characters are replaced with `_`
- Common CJK/fullwidth punctuation is converted to ASCII equivalents, e.g. `（` and `）` become `(` and `)`, `，` becomes `,`, `。` becomes `.`
- Characters forbidden in Windows filenames are replaced with `_`, such as `:`, `?`, `"`, `/`, `\`
- Name collisions are resolved by appending `_2`, `_3`, etc., instead of the space-padded ` (2)` style

Examples:

```text
My File Name -> My_File_Name
Journal（Weekly），Notes。 -> Journal(Weekly),Notes
"Title"，Test：Sync？ -> 'Title',Test_Sync_
```

The script does not proactively clear `output_dir` to avoid accidentally deleting other files. `index.md` only records pages synced in the latest run; files managed by the previous manifest but no longer present in this sync are moved to `_stale/`. Files not tracked in `.sync_manifest.json` are never moved or deleted.

## Scheduled Sync

On Linux servers you can use `cron` for scheduled execution.

Edit cron jobs:

```bash
crontab -e
```

Sync every 6 hours:

```bash
0 */6 * * * cd /home/software/notion_sync && .venv/bin/python3 sync_notion.py --workers 4 --delay 0.1 >> sync.log 2>&1
```

Sync daily at 3 AM:

```bash
0 3 * * * cd /home/software/notion_sync && .venv/bin/python3 sync_notion.py --workers 4 --delay 0.1 >> sync.log 2>&1
```

View logs:

```bash
tail -f /home/software/notion_sync/sync.log
```

## Common Log Messages

### 400: database does not contain any data sources accessible

Usually means the Notion API found a database block, but the current Integration cannot read its data source.

Possible causes:

- The database is not authorized for the Integration
- The database uses Notion's newer data source structure, which the older API cannot read
- The page contains only a database view or link, not a directly readable full database

The script skips this object and continues syncing.

### 404: pages/databases

Usually means the page or database does not exist, has been deleted, has been archived, or the current Integration lacks read permission.

The script skips this object and continues syncing.

### Image download failed: Invalid URL `assets/...`

This typically comes from content imported into Notion from Word, Markdown, or web pages. The image URL is a relative path rather than a full `https://...` link and therefore cannot be downloaded over the network.

The script retains the original image link and will not be interrupted by a single image failure. Full `http://` or `https://` image URLs are retried according to the `image_download_retries` setting; if they still fail, the external link is preserved and syncing continues.

### 429: Rate limited

The Notion API enforces request rate limits. When a 429 is encountered, the script waits and retries.

You can reduce concurrency:

```bash
python sync_notion.py --workers 2 --delay 0.2
```

## Security Advisory

Do not commit your real Notion Token to GitHub. The repository should only contain `conf/config.example.json` — never commit your local `conf/config.json`.

If your token has already been committed to a public repository, regenerate it immediately from the Notion Integration dashboard.

## License

MIT
