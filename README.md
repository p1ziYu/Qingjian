# Qingjian (轻拣)

> A keyboard-first, local-first media sorter for quickly reviewing and organizing photos and videos on Windows.

[![Latest release](https://img.shields.io/github/v/release/p1ziYu/Qingjian?display_name=tag&sort=semver)](https://github.com/p1ziYu/Qingjian/releases)
[![Platform](https://img.shields.io/badge/platform-Windows-0078D6)](https://github.com/p1ziYu/Qingjian)
[![Python](https://img.shields.io/badge/Python-3.12%20%7C%203.13-3776AB)](https://www.python.org/)
[![Status](https://img.shields.io/badge/status-tested-2EA44F)](https://github.com/p1ziYu/Qingjian/releases/tag/v2.0.5)

[English](#qingjian-轻拣) · [中文](#中文说明)

## Overview

Qingjian helps you turn an unsorted media folder into an organized library with a fast review loop: preview one item, press a configured key, and continue to the next item. It supports images and videos, configurable destinations, safe file transactions, duplicate review, and a complete undo/recovery workflow.

Version **2.0.5** is the current tested build. This maintenance release hardens file transactions and recovery, fixes selection, preview, queue, duplicate-review, metadata, dialog, and startup edge cases, and adds broad regression coverage.

## At a glance

| | |
| --- | --- |
| **Product** | Qingjian / 轻拣 |
| **Current release** | [v2.0.5](https://github.com/p1ziYu/Qingjian/releases/tag/v2.0.5) |
| **Platform** | Windows 10/11 |
| **Interface** | PySide6 desktop UI · English / 简体中文 |
| **Media** | Photos, RAW files, videos, and sidecar metadata |
| **Data model** | Local filesystem + SQLite state + durable transaction journal |
| **Network** | Not required; media stays on the local machine |

## Highlights

- **Fast keyboard workflow** — bind `1`–`0` (or other supported keys) to move, copy, favorite, rename, review later, recycle, reveal, or tag media.
- **Image and video support** — preview common image formats, RAW files, MP4/MOV media, and video metadata in one queue.
- **Configurable destinations** — assign a key to a folder, path template, and filename template; nested folders are created as needed.
- **Collision protection** — when a destination name already exists, choose Replace or keep both with an automatic numbered suffix.
- **Undo and recovery** — operations are journaled before files move. `Ctrl+Z` undoes; `Ctrl+Y` restores the previous step. Both actions are disabled until a source folder is selected and an applicable history entry exists.
- **Sidecar-aware transactions** — matching RAW, XMP, AAE, and Live Photo video sidecars can travel with the primary file and are undone as one unit.
- **Duplicate review** — exact duplicates, perceptual near-duplicates, and burst groups; opening a duplicate previews the actual media, and Ignore records a per-source exclusion without moving or deleting files.
- **Ratings and labels** — rate items and apply color labels while reviewing.
- **Review queue and filters** — search, filter, sort, browse as a grid or single item, and postpone items for later review.
- **Local-first and privacy-conscious** — no account, server, or media upload is required. Diagnostics exclude media files.

## Download and run (Windows)

1. Download the latest `Qingjian-2.0.5-Windows.zip` from the repository Releases page.
2. Extract the archive to a local folder.
3. Run `MediaSorter.exe` inside the extracted `MediaSorter` folder. Keep the folder together: the executable loads its libraries from beside it instead of unpacking them on every launch.
4. Select a source folder, configure the key bindings, and start reviewing.
5. Optional: turn on **Settings → General → Explorer folder menu** to right-click any folder and open it in Qingjian. If Qingjian is already open, it switches to that folder.

The executable intentionally keeps the ASCII name `MediaSorter.exe`: some Windows native-window configurations have issues with non-ASCII executable names. The UI is still named **轻拣 / Qingjian**.

## Build from source

Requirements: Windows 10/11 and Python 3.12 or 3.13.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py
```

Build the Windows program with PyInstaller. The result is the folder `dist\MediaSorter`; ship the whole folder:

```powershell
python -m PyInstaller --noconfirm --clean qingjian.spec
```

Run the built-in verification before distributing a build:

```powershell
python main.py --selfcheck
```

The self-check creates a temporary library, exercises core operations, drives the real Qt window offscreen, opens the dialogs, switches languages and views, performs classify/undo, and verifies that a fresh window has both Undo and Restore Previous Step disabled. It does not touch user media.

## Feature showcase

### 1. Review, decide, continue

Open a source folder, preview the current item, press a single configured key, and immediately continue. Single-item and grid views, search, filters, sorting, ratings, labels, and a review-later queue keep the workflow focused.

### 2. Destinations that match your workflow

Each binding can target a different folder and action. Path and filename templates support dates, metadata, the original name, and sequence numbers. When a destination already contains the same name, Qingjian asks whether to **Replace** or keep both with an automatic suffix.

### 3. Duplicate review without destructive defaults

Inspect exact duplicates, perceptual near-duplicates, and burst groups in a dedicated review flow. Double-clicking a result previews the actual media. **Ignore** writes a source-folder exclusion and never moves or deletes the file.

### 4. Transactions you can undo

Every mutating operation is journaled before file changes. The engine checks file identity, supports atomic same-volume moves, and can recover after interruption. `Ctrl+Z` undoes; `Ctrl+Y` restores the previous step. Both controls stay disabled until a source folder and applicable history exist.

### 5. Sidecars move as one unit

RAW, XMP, AAE, and Live Photo video sidecars can follow their primary media in one transaction. Renames and undo preserve the relationship instead of leaving metadata behind.

### 6. Keyboard-first, bilingual desktop UI

The interface is optimized for long review sessions: visible key bindings, clear enabled/disabled states, English/Chinese switching, and mouse access when needed.

## How the pieces fit

```text
Source folder
     │
     ▼
Scanner ── filters / sort / sidecar grouping ──► Review queue
     │                                             │
     ▼                                             ▼
Metadata + duplicate index                    PySide6 UI
     │                                             │
     └──────────────► Transaction engine ◄─────────┘
                         │
                         ├─ journal + recovery store
                         └─ SQLite state / ratings / labels / ignores
```

The `core` package is Qt-free and owns filesystem, metadata, duplicate, state, and transaction logic. The `ui` package renders the workflow and calls the engine rather than changing files directly.

## Keyboard shortcuts

| Shortcut | Action |
| --- | --- |
| `1`–`0` | Run the configured binding |
| `Left` / `Right` | Previous / next item; hold to flip through quickly |
| `S` | Send to review queue |
| `G` | Toggle single/grid view |
| `F2` | Rename |
| `Delete` | Recycle at once, no confirmation (`Ctrl+Z` brings it back) |
| `Ctrl+Z` | Undo |
| `Ctrl+Y` | Restore previous step |
| `Shift+1`–`5`, `Shift+0` | Rating / clear rating |
| `Alt+1`–`5`, `Alt+0` | Color label / clear label |
| `/`, `Ctrl+F` | Search destination folders |
| `Space`, `J`, `K`, `L`, `,`, `.` | Video playback and frame stepping |
| `Ctrl+D` | Duplicate review |
| `Ctrl+I` | Media information |
| `Ctrl+R` | Review queue |
| `F5` / `F11` / `Esc` | Rescan / fullscreen / leave fullscreen |

Click a binding card to run it; right-click it to change its folder. A binding cannot take a key the window already uses (such as `G`, `S`, `J`/`K`/`L` or `M`): the key editor refuses it, because two shortcuts on one key would both stop working.

## Templates and file safety

Each binding can define a destination and filename template. Supported values include dates, the original name and extension, source folder, camera/lens/ISO, rating, label, and a sequence number. For example:

```text
Destination: D:\Photos\Keepers\{YYYY}\{YYYY-MM}
Filename:    {YYYY-MM-DD}_{seq:4}_{name}
```

The transaction store provides:

- a durable journal written before file changes;
- pre-operation identity checks (size and modification time) to avoid overwriting externally changed files;
- atomic same-volume moves where possible, with content hashing reserved for bytes that are actually copied;
- recycling that moves the file into a hidden `.qingjian-trash` folder beside it: instant, no copy, undone with `Ctrl+Z`, and cleared by the retention policy (or sent to the Windows recycle bin, if chosen in Settings);
- bounded recovery snapshots and cleanup policies.

If the application is interrupted, use the startup recovery action before continuing. Do not delete journal or snapshot files manually.

## Data location

By default, application data is stored under:

```text
%APPDATA%\LocalMediaTools\Qingjian\
    settings.json
    state\state.db
    store\
    cache\hashes.db
    logs\
```

For a portable setup, create an empty `qingjian-portable` directory next to the executable, or pass `--data-dir <path>`.

## Project structure

```text
qingjian/
  core/       Qt-free scanning, metadata, templates, transactions, state, and duplicates
  ui/         PySide6 application, preview, dialogs, bindings, and duplicate review
tests/        unit and integrity tests
main.py       application and self-check entry point
qingjian.spec PyInstaller recipe
```

The core layer does not import Qt. The UI calls the engine layer instead of manipulating filesystem operations directly.

## Repository guide

- `qingjian/core/` — Qt-free scanning, metadata, templates, transactions, state, and duplicate logic.
- `qingjian/ui/` — PySide6 application, preview, dialogs, bindings, and duplicate review.
- `tests/` — unit, integrity, media, and scale tests.
- `docs/` — maintenance notes and reports, including `功能修复说明.md` (2.0.4 notes) and `打包测试报告.md` (build and verification report).

## Contributing

Bug reports and focused pull requests are welcome. Please include the Windows version, Python version (for source builds), reproduction steps, and the output of `python main.py --selfcheck`. Never attach personal media or application data directories; use synthetic files when possible.

## AI-assisted development disclosure

This project was developed with human direction, review, and testing, with assistance from multiple AI tools:

- **OpenAI Codex** — implementation, debugging, code review, regression checks, UI-state verification, and PyInstaller packaging.
- **Anthropic Claude** — earlier feature implementation and iteration of the application.
- **xAI Grok** — UI critique and visual direction suggestions.
- **Design guidance** — Impeccable, `frontend-design`, `web-design-guidelines`, `react-best-practices`, `shadcn`, and `ui-ux-pro-max` were consulted for interface and interaction decisions.

AI-generated suggestions were reviewed and integrated by the maintainer. The project is tested locally; AI assistance is not a claim of zero defects or a substitute for review.

## 中文说明

轻拣（Qingjian）是一款面向 Windows 的本地图片与视频快速分类工具。选择来源文件夹后，可以用 `1`–`0` 等快捷键把当前媒体移动、复制、收藏、重命名、回收或打标签，并自动进入下一项。

主要功能包括：图片/视频预览、可配置按键与目标目录、路径和命名模板、同名文件 Replace/自动编号、伴随文件事务处理、查重与忽略、评分和色标、待复查队列，以及可恢复的撤销流程。2.0.5 加固了文件事务与恢复，并修复了选区、预览、后台队列、查重、元数据、对话框和启动流程中的边界问题。

使用要点：长按 ← → 可连续翻页；Delete 直接删除、不再确认，文件移到同盘的隐藏文件夹 `.qingjian-trash`，Ctrl+Z 即可恢复；单击按键卡片执行动作，右键卡片更换目标文件夹；在“设置 → 通用”里打开资源管理器右键菜单后，可以在文件夹上右键“用轻拣打开”。发布包是一个 `MediaSorter` 文件夹，请整个解压后运行其中的 `MediaSorter.exe`。

本项目使用 AI 辅助开发：OpenAI Codex 负责当前版本的实现、测试和打包；Anthropic Claude 参与早期版本迭代；xAI Grok 提供 UI 评审建议；Impeccable、`frontend-design`、`web-design-guidelines`、`react-best-practices`、`shadcn` 和 `ui-ux-pro-max` 提供界面设计参考。所有改动均经过维护者审核和本地测试。

## License

Qingjian is released under the [MIT License](LICENSE). Copyright © 2026 p1ziYu.

Third-party dependencies remain under their respective licenses.
