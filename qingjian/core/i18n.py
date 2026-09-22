"""Bilingual string catalogue.

A plain dict catalogue rather than Qt Linguist ``.ts``/``.qm`` files: it needs
no build step, it is importable without Qt, and a test can prove that both
languages carry every key and the same format placeholders. ``tr()`` is a
module-level function bound to one process-wide translator so that call sites
stay short.
"""
from __future__ import annotations

import locale
import os
import re
from typing import Callable, Iterable

LANGUAGES: tuple[tuple[str, str], ...] = (("zh", "中文"), ("en", "English"))
LANGUAGE_CODES = tuple(code for code, _ in LANGUAGES)
DEFAULT_LANGUAGE = "zh"

_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

# key: (zh, en)
CATALOG: dict[str, tuple[str, str]] = {
    # ---- app ----------------------------------------------------------
    "app.name": ("轻拣", "Qingjian"),
    "app.tagline": ("图片与视频快速分类", "Fast photo and video sorting"),
    "app.subtitle": ("MEDIA SORTER", "MEDIA SORTER"),
    "app.open_with": ("用轻拣打开", "Open with Qingjian"),

    # ---- generic actions ----------------------------------------------
    "ok": ("确定", "OK"),
    "cancel": ("取消", "Cancel"),
    "close": ("关闭", "Close"),
    "apply": ("应用", "Apply"),
    "save": ("保存", "Save"),
    "delete": ("删除", "Delete"),
    "rename": ("重命名", "Rename"),
    "browse": ("浏览", "Browse"),
    "reset": ("恢复默认", "Reset to default"),
    "add": ("添加", "Add"),
    "remove": ("移除", "Remove"),
    "yes": ("是", "Yes"),
    "no": ("否", "No"),
    "select_all": ("全选", "Select all"),
    "invert_selection": ("反选", "Invert"),
    "clear_selection": ("取消选择", "Clear"),
    "retry": ("重试", "Retry"),
    "skip": ("跳过", "Skip"),
    "unknown": ("未知", "Unknown"),
    "none": ("无", "None"),

    # ---- header --------------------------------------------------------
    "header.choose_folder": ("选择文件夹", "Choose folder"),
    "header.rescan": ("重新扫描", "Rescan"),
    "header.rescan_tip": ("重新扫描（F5）", "Rescan (F5)"),
    "header.settings": ("设置", "Settings"),
    "header.no_folder": ("尚未选择待分类文件夹", "No folder chosen yet"),
    "header.item_count": ("{count} 项", "{count} items"),

    # ---- views ---------------------------------------------------------
    "view.single": ("单张", "Single"),
    "view.grid": ("网格", "Grid"),
    "view.compare": ("对比", "Compare"),

    # ---- filters -------------------------------------------------------
    "filter.label": ("筛选", "Filter"),
    "filter.all": ("全部媒体", "All media"),
    "filter.images": ("仅图片", "Images only"),
    "filter.videos": ("仅视频", "Videos only"),
    "filter.raw": ("仅 RAW", "RAW only"),
    "filter.landscape": ("横向", "Landscape"),
    "filter.portrait": ("纵向", "Portrait"),
    "filter.square": ("方形", "Square"),
    "filter.short_video": ("短视频 ≤ {seconds} 秒", "Short videos ≤ {seconds}s"),
    "filter.animated": ("动图", "Animated"),
    "filter.rated": ("已评分", "Rated"),
    "filter.unrated": ("未评分", "Unrated"),
    "filter.labelled": ("有色标", "Labelled"),

    # ---- sorting -------------------------------------------------------
    "sort.name": ("按名称", "By name"),
    "sort.date": ("按拍摄时间", "By capture date"),
    "sort.modified": ("按修改时间", "By modified date"),
    "sort.size": ("按文件大小", "By file size"),
    "sort.rating": ("按评分", "By rating"),
    "sort.random": ("随机顺序", "Shuffle"),
    "sort.reverse": ("倒序", "Reverse"),

    "scan.recursive": ("包含子文件夹", "Include subfolders"),
    "scan.scanning": ("正在扫描媒体文件…", "Scanning media…"),
    "scan.scanned_n": ("已扫描 {count} 项", "{count} scanned"),
    "scan.complete": ("扫描完成：{count} 项", "Scan complete: {count} items"),
    "scan.cancelled": ("扫描已取消", "Scan cancelled"),
    "scan.failed": ("扫描失败：{error}", "Scan failed: {error}"),
    "scan.filtering": ("正在筛选…", "Filtering…"),
    "scan.filter_cancelled": ("筛选已取消", "Filter cancelled"),
    "scan.no_media": ("当前筛选下没有待分类媒体", "Nothing to sort under this filter"),
    "scan.review_empty": ("待复查队列为空", "The review queue is empty"),
    "scan.hint_adjust": (
        "可调整筛选条件、选择其他文件夹或按 F5 重新扫描",
        "Adjust the filter, pick another folder, or press F5 to rescan",
    ),
    "scan.hint_start": (
        "选择一个文件夹，或把文件夹直接拖进这个窗口",
        "Choose a folder, or drag one straight into this window",
    ),

    # ---- toolbar -------------------------------------------------------
    "tool.duplicates": ("查重", "Duplicates"),
    "tool.history": ("操作历史", "History"),
    "tool.stats": ("统计 / 导出", "Stats / Export"),
    "tool.backups": ("备份管理", "Backups"),
    "tool.recover": ("恢复未完成操作", "Finish pending operation"),
    "recover.exit_text": (
        "未完成的操作没能恢复：\n{error}\n\n"
        "可以撤回这次操作已完成的部分，或者保留文件现在的样子并清除这条未完成记录。",
        "The pending operation could not be finished:\n{error}\n\n"
        "You can undo the part that already ran, or keep the files as they are now "
        "and clear the pending operation.",
    ),
    "recover.rollback": ("撤回已完成的部分", "Undo the part that ran"),
    "recover.keep": ("保留现状", "Keep files as they are"),
    "recover.later": ("稍后再试", "Try again later"),
    "tool.info": ("信息", "Info"),
    "tool.more": ("更多", "More"),
    "tool.rotate_left": ("预览向左转（不改动文件）", "Turn the preview left (the file is not changed)"),
    "tool.rotate_right": ("预览向右转（不改动文件）", "Turn the preview right (the file is not changed)"),
    "tool.previous": ("上一张（←）", "Previous (←)"),
    "tool.next": ("下一张（→）", "Next (→)"),
    "header.language_tip": ("切换界面语言", "Switch the interface language"),

    # ---- sidebar -------------------------------------------------------
    "side.bulk_subtitle": ("动作作用于 {count} 个选中项", "Applies to {count} selected items"),
    "side.preset": ("方案", "Preset"),
    "side.preset_new": ("新建当前方案的副本", "Duplicate this preset"),
    "side.preset_rename": ("重命名当前方案", "Rename this preset"),
    "side.preset_delete": ("删除当前方案", "Delete this preset"),
    "side.preset_name": ("方案名称：", "Preset name:"),
    "side.preset_exists": ("请使用不同的方案名称。", "That preset name is already taken."),
    "side.preset_last": ("至少需要保留一个分类方案。", "At least one preset must remain."),
    "side.search_targets": ("搜索目标文件夹…", "Search target folders…"),
    "side.search_short": ("搜索…", "Search…"),
    "side.review_queue": ("待复查（{count}）", "Review later ({count})"),
    "side.review_exit": ("退出待复查（{count}）", "Leave review queue ({count})"),
    "side.undo": ("撤销", "Undo"),
    "side.redo": ("恢复上一步", "Redo"),
    "side.undo_batch": ("撤销这一批", "Undo this batch"),
    "side.rating": ("评分", "Rating"),
    "side.colour_label": ("色标", "Label"),
    "side.keyhint": (
        "← → 浏览 · S 稍后 · G 网格 · Ctrl+Z / Ctrl+Y\n"
        "图片：滚轮缩放 / 拖动 / 双击适应 · 视频：J K L · , .",
        "← → browse · S later · G grid · Ctrl+Z / Ctrl+Y\n"
        "Image: wheel zoom / drag / dbl-click fit · Video: J K L · , .",
    ),

    # ---- actions -------------------------------------------------------
    "action.move": ("移动", "Move"),
    "action.move.desc": ("剪切到目标文件夹", "Cut to the target folder"),
    "action.copy": ("复制", "Copy"),
    "action.copy.desc": ("复制到目标文件夹", "Copy into the target folder"),
    "action.favorite": ("收藏", "Favorite"),
    "action.favorite.desc": ("复制到收藏文件夹", "Copy into the favorites folder"),
    "action.skip": ("稍后处理", "Review later"),
    "action.skip.desc": ("加入待复查队列", "Add to the review queue"),
    "action.rename": ("重命名", "Rename"),
    "action.rename.desc": ("输入新文件名后继续", "Type a new name, then continue"),
    "action.trash": ("回收站", "Recycle"),
    "action.trash.desc": ("移到回收站，可撤销", "Move to the recycle bin; undoable"),
    "action.reveal": ("定位文件", "Show in folder"),
    "action.reveal.desc": ("在文件管理器中显示", "Reveal in the file manager"),
    "action.tag": ("打标签", "Tag"),
    "action.tag.desc": ("只记评分与色标，不移动文件", "Records rating and label; moves nothing"),

    "action.badge.copy": ("复制", "COPY"),
    "action.badge.favorite": ("收藏", "FAVORITE"),
    "action.badge.undoable": ("可撤销", "UNDOABLE"),

    # ---- status --------------------------------------------------------
    "status.ready": ("就绪", "Ready"),
    "status.done_action": ("已{action}：{name}", "{action}: {name}"),
    "status.undo_done": ("撤销完成", "Undone"),
    "status.redo_done": ("已恢复上一步", "Redone"),
    "status.recover_done": ("恢复检查完成", "Recovery check complete"),
    "status.recover_rolled_back": ("已撤回未完成的操作", "The pending operation was undone"),
    "status.recover_kept": (
        "已保留现状，未完成记录已清除",
        "Files kept as they are; the pending operation was cleared",
    ),
    "status.session_progress": ("本次已处理 {done} / {total}", "{done} / {total} handled"),
    "status.snapshot_usage": ("快照 {used} / {cap}", "Snapshots {used} / {cap}"),
    "status.snapshot_manage": ("管理", "Manage"),
    "status.queue_pending": ("队列中 {count} 项", "{count} queued"),
    "status.queue_failed": ("{count} 项失败，已放回队列", "{count} failed and were put back"),
    "status.thumbs_cached": ("缩略图已缓存 {count} 项", "{count} thumbnails cached"),
    "status.enter_review": ("正在处理待复查队列", "Working through the review queue"),
    "status.leave_review": ("已返回普通分类队列", "Back to the normal queue"),
    "status.revealed": ("已在文件管理器中定位", "Revealed in the file manager"),
    "status.nothing_chosen": ("网格里没有选中项", "Nothing is selected in the grid"),
    "status.rename_one_only": ("改名一次只处理一项，请只选中一个文件",
                              "Renaming handles one file at a time; select just one"),
    "status.exported": ("记录已导出", "Records exported"),
    "status.preview_failed": ("无法预览 {name}：{error}", "Cannot preview {name}: {error}"),
    "status.video_failed": ("视频预览失败：{error}", "Video preview failed: {error}"),
    "status.reserved_key": (
        "按键 {keys} 与内置快捷键冲突，内置功能照常可用；请在按键设置里换一个键",
        "Key {keys} clashes with a built-in shortcut, which keeps working — pick another key",
    ),
    "status.hidden_handled": (
        "{count} 个复制或收藏过的文件已隐藏 · 重新显示",
        "{count} copied or favourited files hidden · show them again",
    ),
    "status.handled_revealed": ("已重新显示 {count} 个文件", "{count} files are back in the queue"),
    "card.tip": ("单击：执行这个键的动作 · 右键：更换目标文件夹",
                 "Click: run this key's action · Right-click: change its folder"),

    # ---- errors --------------------------------------------------------
    "error.title": ("操作未完成", "Operation not completed"),
    "error.folder_missing": ("文件夹不存在或无法访问", "That folder is missing or unreadable"),
    "error.target_is_source": (
        "目标是当前文件所在文件夹，请选择其他位置",
        "The target is the file's own folder — choose somewhere else",
    ),
    "error.group_name_collision": (
        "同组文件的目标名称冲突：{path}",
        "Files in this group would share a destination: {path}",
    ),
    "error.busy": ("请等待当前操作完成", "Wait for the current operation to finish"),
    "error.pending_first": ("请先恢复未完成操作", "Finish the pending operation first"),
    "error.pending_blocked": (
        "存在未完成操作，请先恢复后再继续",
        "A pending operation must be finished before anything else runs",
    ),
    "error.pending_block_write": (
        "存在待恢复操作，已阻止新的文件修改",
        "A pending operation is blocking new file changes",
    ),
    "error.external_change": (
        "文件已被外部修改，操作已停止：{path}",
        "Stopped: this file was changed outside the app: {path}",
    ),
    "error.recover_blocked": (
        "恢复被阻止：文件被外部修改，请保留当前文件：{path}",
        "Recovery blocked: the file was changed outside the app — keep it as it is: {path}",
    ),
    "error.snapshot_missing": (
        "恢复副本缺失或损坏：{path}",
        "The restore copy is missing or damaged: {path}",
    ),
    "error.changed_midway": ("文件在操作期间改变：{path}", "The file changed during the operation: {path}"),
    "error.plan_collision": (
        "同一次操作里有两个文件要用同一个位置，已停止，文件未改动：{path}",
        "Stopped before changing anything: two files in this operation need the same "
        "place: {path}",
    ),
    "error.journal_damaged": (
        "未完成操作的记录已损坏，无法继续恢复",
        "The record of the pending operation is damaged and cannot be replayed",
    ),
    "error.copy_verify": ("副本校验失败：{path}", "Copy verification failed: {path}"),
    "error.source_changed": ("复制期间源文件发生变化：{path}", "The source changed while copying: {path}"),
    "error.not_regular_file": ("仅处理普通文件：{path}", "Only regular files are handled: {path}"),
    "error.symlink": ("不支持修改符号链接", "Symbolic links are not modified"),
    "error.cancelled": ("已取消，原文件未改变", "Cancelled; nothing was changed"),
    "error.unfinished": (
        "操作未完成，恢复记录和原文件副本已保存。请点击“恢复未完成操作”。\n{error}",
        "The operation stopped part-way. The journal and restore copies are safe — "
        "use “Finish pending operation”.\n{error}",
    ),
    "error.single_instance": (
        "轻拣已在运行，请返回已有窗口。为避免同时修改文件，只允许一个实例。",
        "Qingjian is already running — switch to that window. "
        "Only one instance may run so that two copies never touch the same files.",
    ),
    "error.no_unique_name": ("无法生成不重名的文件名", "Could not build a name that is not already taken"),
    "error.name_invalid": ("文件名无效", "That filename is not valid"),
    "error.name_reserved": (
        "“{name}” 是 Windows 保留名，请换一个",
        "“{name}” is a reserved name on Windows — pick another",
    ),
    "error.name_too_long": ("路径过长（{length} 字符）", "The path is too long ({length} characters)"),
    "error.name_case_only": (
        "新旧名称仅大小写不同，请先使用一个不同名称",
        "The new name differs only by case — use a different name first",
    ),
    "error.decode_image": ("无法解码图片", "This image could not be decoded"),
    "error.no_frame": ("视频中没有可解码的画面", "No decodable frame in this video"),
    "error.disk_full": ("磁盘空间不足", "Not enough disk space"),
    "error.permission": ("没有权限访问：{path}", "Permission denied: {path}"),

    # ---- name conflict -------------------------------------------------
    "conflict.title": ("发现同名文件", "A file with that name already exists"),
    "conflict.text": ("“{name}”已经存在于目标文件夹中", "“{name}” already exists in the target folder"),
    "conflict.detail": (
        "{folder}\n\n现有文件：{existing}    待分类文件：{incoming}\n请选择处理方式。",
        "{folder}\n\nExisting: {existing}    Incoming: {incoming}\nChoose how to handle it.",
    ),
    "conflict.replace": ("替换现有文件", "Replace the existing file"),
    "conflict.sequence": ("自动添加序号", "Add a sequence number"),
    "conflict.skip": ("跳过这一个", "Skip this one"),
    "conflict.remember": ("对本次剩余的同名文件都这样处理", "Do this for the rest of this run"),
    "conflict.mode.replace": ("替换", "Replaced"),
    "conflict.mode.sequence": ("加序号", "Sequenced"),
    "conflict.mode.none": ("无", "None"),

    # ---- sidecar -------------------------------------------------------
    "sidecar.title": ("发现伴随文件", "Sidecar files found"),
    "sidecar.body": (
        "“{name}”还有 {count} 个同名文件。一起移动可以避免 RAW 和 JPG 分家。",
        "“{name}” has {count} files sharing its name. Moving them together keeps the RAW with its JPG.",
    ),
    "sidecar.target": ("目标", "Target"),
    "sidecar.kind.master": ("主文件", "Master"),
    "sidecar.kind.raw": ("RAW", "RAW"),
    "sidecar.kind.metadata": ("编辑参数", "Edit metadata"),
    "sidecar.kind.live": ("实况照片", "Live Photo"),
    "sidecar.kind.other": ("同名文件", "Same name"),
    "sidecar.atomic": (
        "{count} 个文件在同一个事务里移动。任意一个失败，整组都会回滚到原位，"
        "不会出现只搬走一半的情况。Ctrl+Z 也按整组撤销。",
        "All {count} move inside one transaction. If any part fails the whole group rolls back — "
        "never half-moved. Ctrl+Z undoes the group as one.",
    ),
    "sidecar.remember": (
        "以后不再询问，始终联动同名文件",
        "Don’t ask again — always move files that share a name",
    ),
    "sidecar.edit_rules": ("在设置中调整规则", "Edit the rules in Settings"),
    "sidecar.move_all": ("一起移动 {count} 个文件", "Move all {count} files"),
    "sidecar.master_only": ("仅移动主文件", "Move master only"),
    "sidecar.badge": ("{count} 个伴随文件", "{count} sidecars"),
    "sidecar.selection_note": (
        "选中项含 {count} 个伴随文件，将一并移动",
        "The selection carries {count} sidecars — they move too",
    ),

    # ---- duplicates ----------------------------------------------------
    "dup.title": ("重复与相似", "Duplicates & near-matches"),
    "dup.subtitle": (
        "同组同底色 · 单击预览 · 双击放大 · Ctrl / Shift 多选 · 忽略不会移动或删除原文件",
        "One tint per group · click to preview · double-click to enlarge · "
        "Ctrl / Shift to multi-select · ignoring never moves or deletes a file",
    ),
    "dup.tab.exact": ("精确重复", "Exact copies"),
    "dup.tab.similar": ("相似图片", "Near-matches"),
    "dup.tab.burst": ("连拍组", "Bursts"),
    "dup.threshold": ("相似阈值", "Threshold"),
    "dup.summary": ("{groups} 组 · {files} 个文件", "{groups} groups · {files} files"),
    "dup.reclaimable": ("可释放 {size}", "{size} reclaimable"),
    "dup.ignored_count": ("已忽略 {count} 条", "{count} ignored"),
    "dup.none_found": ("未发现内容完全相同的文件", "No byte-identical files found"),
    "dup.none_similar": ("未发现相似图片", "No near-matching images found"),
    "dup.col.file": ("文件名", "File"),
    "dup.col.folder": ("所在文件夹", "Folder"),
    "dup.col.resolution": ("分辨率", "Resolution"),
    "dup.col.size": ("大小", "File size"),
    "dup.col.match": ("相似", "Match"),
    "dup.col.sharpness": ("清晰度", "Sharpness"),
    "dup.mark.keep": ("推荐保留", "Keep"),
    "dup.mark.keep_sharp": ("推荐保留 · 最清晰", "Keep · sharpest"),
    "dup.mark.extra": ("多余", "Extra"),
    "dup.mark.lower_res": ("低分辨率", "Lower res"),
    "dup.mark.softer": ("略糊", "Softer"),
    "dup.ignore_selected": ("忽略选中文件", "Ignore selected"),
    "dup.ignore_group": ("忽略选中整组", "Ignore whole group"),
    "dup.restore_ignored": ("恢复已忽略项（{count}）", "Restore ignored ({count})"),
    "dup.auto_keep_best": ("自动保留每组最佳", "Auto-keep best of each group"),
    "dup.extras_to_review": ("多余项加入待复查", "Send extras to review"),
    "dup.searching": ("查找重复内容", "Looking for duplicates"),
    "dup.hashing": ("校验 {name}", "Hashing {name}"),
    "dup.cancelled": ("已取消查重", "Duplicate scan cancelled"),
    "dup.scanning": ("正在扫描 {done}/{total}", "Scanning {done}/{total}"),
    "dup.stop": ("停止", "Stop"),
    "dup.rescan": ("重新扫描", "Rescan"),
    "dup.scan_failed": ("扫描失败：{error}", "Scan failed: {error}"),
    "dup.first_scan_note": (
        "首次扫描需要读取每张照片，之后会记住结果，再次打开很快。",
        "The first scan reads every photo; the results are remembered, so "
        "opening this again is quick."),
    "dup.loading_rows": ("正在载入 {done}/{total} 行", "Loading {done}/{total} rows"),
    "scan.reading": ("正在读取这张大图…", "Reading this large image…"),
    "dup.ignore_note": (
        "“忽略”只记录在本源目录的忽略清单里，重启后仍然有效，不会移动或删除任何文件。"
        "文件内容改变后会重新参与查重。",
        "Ignoring only writes to this folder’s ignore list — it survives restarts and never moves "
        "or deletes a file. A file re-enters matching once its content changes.",
    ),
    "dup.burst_found": (
        "发现 {groups} 组连拍（共 {frames} 张），推荐按清晰度保留每组最佳",
        "{groups} bursts found ({frames} frames) — keep the sharpest of each",
    ),
    "dup.burst_badge": ("连拍 {count} 张", "Burst · {count}"),
    "dup.pick": ("推荐", "Pick"),
    "dup.auto_pick": ("自动选出最佳", "Auto-pick best"),
    "dup.rest_to_review": ("其余加入待复查", "Rest to review"),

    # ---- grid ----------------------------------------------------------
    "grid.thumb_size": ("缩略图大小", "Thumbnail size"),
    "grid.show_filename": ("文件名", "Filename"),
    "grid.show_rating": ("评分与色标", "Rating & label"),
    "grid.group_bursts": ("连拍成组", "Group bursts"),
    "grid.selected": ("{count} 项已选中", "{count} selected"),
    "grid.bulk_hint": ("按 1–0 批量分类", "Press 1–0 to sort in bulk"),
    "grid.bulk_rate": ("评分", "Rate"),
    "grid.bulk_label": ("色标", "Label"),
    "grid.bulk_review": ("待复查", "Review later"),

    # ---- info ----------------------------------------------------------
    "info.title": ("媒体信息", "Media info"),
    "info.property": ("属性", "Property"),
    "info.value": ("值", "Value"),
    "info.filename": ("文件名", "Filename"),
    "info.path": ("路径", "Path"),
    "info.size": ("大小", "Size"),
    "info.modified": ("修改时间", "Modified"),
    "info.captured": ("拍摄时间", "Captured"),
    "info.dimensions": ("尺寸", "Dimensions"),
    "info.format": ("格式", "Format"),
    "info.codec": ("编码", "Codec"),
    "info.framerate": ("帧率", "Frame rate"),
    "info.duration": ("时长", "Duration"),
    "info.camera": ("相机", "Camera"),
    "info.lens": ("镜头", "Lens"),
    "info.iso": ("ISO", "ISO"),
    "info.aperture": ("光圈", "Aperture"),
    "info.shutter": ("快门", "Shutter"),
    "info.focal": ("焦距", "Focal length"),
    "info.sharpness": ("清晰度", "Sharpness"),
    "info.sidecars": ("伴随文件", "Sidecars"),
    "info.rating": ("评分", "Rating"),
    "info.label": ("色标", "Label"),

    # ---- history & stats -----------------------------------------------
    "history.title": ("历史记录", "History"),
    "history.time": ("时间", "Time"),
    "history.action": ("动作", "Action"),
    "history.original": ("原文件", "Original"),
    "history.destination": ("目标", "Target"),
    "history.conflict": ("同名处理", "Name clash"),
    "history.state": ("状态", "State"),
    "history.state.done": ("已执行", "Applied"),
    "history.state.undone": ("已撤销，可重做", "Undone — can be redone"),
    "history.undo_last": ("撤销最后操作", "Undo the last operation"),
    "history.legacy": ("查看旧版记录", "Show legacy records"),
    "history.legacy_title": (
        "旧版记录（只读；旧备份保留，未自动转换为新事务）",
        "Legacy records (read-only; old backups kept, not converted to new transactions)",
    ),
    "history.legacy_backup": ("旧备份位置", "Legacy backup location"),

    "stats.title": ("本次统计（已扣除撤销）", "This session (undos deducted)"),
    "stats.item": ("项目", "Item"),
    "stats.count": ("数量", "Count"),
    "stats.net_handled": ("本次净处理", "Net handled"),
    "stats.remaining": ("当前剩余", "Remaining"),
    "stats.in_review": ("待复查", "In review"),
    "stats.undo_count": ("撤销次数", "Undos"),
    "stats.redo_count": ("重做次数", "Redos"),
    "stats.bytes_moved": ("移动字节数", "Bytes moved"),
    "stats.target_prefix": ("目标：{folder}", "Target: {folder}"),
    "stats.export": ("导出记录", "Export records"),
    "stats.export_dialog": ("导出记录", "Export records"),
    "stats.export_name": ("轻拣记录.csv", "qingjian-records.csv"),

    # ---- backups -------------------------------------------------------
    "backup.title": ("备份管理", "Backup management"),
    "backup.usage": ("恢复副本占用", "Restore copies use"),
    "backup.location": ("目录", "Location"),
    "backup.policy": ("保留策略", "Retention"),
    "backup.policy_text": (
        "超出任一上限时，最旧的记录会被标为不可撤销并回收其恢复副本",
        "Past any limit, the oldest records become non-undoable and their restore copies are reclaimed",
    ),
    "backup.reclaim_now": ("立即回收", "Reclaim now"),
    "backup.reclaimed": ("已回收 {size}，{count} 条记录不再可撤销",
                         "Reclaimed {size}; {count} records are no longer undoable"),
    "backup.clear_all": ("清理全部历史和备份", "Clear all history and backups"),
    "backup.clear_confirm": (
        "这会清空全部撤销/重做记录，并将恢复副本移入系统回收站。继续？",
        "This clears every undo/redo record and sends the restore copies to the recycle bin. Continue?",
    ),
    "backup.keep_last": ("保留最近", "Keep last"),
    "backup.disk_cap": ("磁盘上限", "Disk cap"),
    "backup.keep_days": ("保留天数", "Keep for"),
    "backup.operations": ("{count} 次", "{count} ops"),
    "backup.days": ("{count} 天", "{count} days"),

    # ---- settings ------------------------------------------------------
    "settings.title": ("设置", "Settings"),
    "settings.subtitle": (
        "全部设置随方案保存，可导出为便携配置",
        "Every setting is saved with the preset and can be exported as a portable config",
    ),
    "settings.import_export": ("导入 / 导出配置", "Import / export config"),
    "settings.nav.general": ("通用", "General"),
    "settings.nav.sidecar": ("伴随文件", "Sidecar files"),
    "settings.nav.rules": ("分类规则", "Sorting rules"),
    "settings.nav.safety": ("安全与备份", "Safety & backups"),
    "settings.nav.performance": ("性能", "Performance"),
    "settings.nav.shortcuts": ("快捷键", "Shortcuts"),
    "settings.nav.about": ("关于", "About"),

    "settings.language": ("界面语言", "Interface language"),
    "settings.language.desc": (
        "所有界面、对话框、状态提示与导出表头都会切换",
        "Switches every screen, dialog, status line and export header",
    ),
    "settings.language.system": ("跟随系统", "Follow system"),
    "settings.density": ("界面密度", "Density"),
    "settings.density.desc": (
        "英文字符串平均比中文长 60%，宽松模式为其预留空间",
        "English strings run about 60% longer than Chinese — roomy leaves space for them",
    ),
    "settings.density.compact": ("紧凑", "Compact"),
    "settings.density.standard": ("标准", "Standard"),
    "settings.density.roomy": ("宽松", "Roomy"),
    "settings.restore_position": ("启动时恢复上次位置", "Restore last position on start"),
    "settings.restore_position.desc": (
        "记住上次的源文件夹、筛选、排序和看到第几张",
        "Remembers the last folder, filter, sort and which item you were on",
    ),
    "settings.default_view": ("默认视图", "Default view"),

    "settings.sidecar.enable": ("联动同名伴随文件", "Move sidecars with their master"),
    "settings.sidecar.enable.desc": (
        "移动 IMG_0001.JPG 时，同名的 IMG_0001.CR2 与 .XMP 一起搬到同一目标，绝不落单",
        "Moving IMG_0001.JPG carries IMG_0001.CR2 and .XMP to the same target — nothing gets left behind",
    ),
    "settings.sidecar.prompt": ("联动方式", "Prompt behaviour"),
    "settings.sidecar.prompt.desc": (
        "“询问一次”会在首次遇到伴随文件时弹出确认，选择后记住",
        "“Ask once” confirms the first time and remembers your answer",
    ),
    "settings.sidecar.ask_each": ("每次询问", "Ask each time"),
    "settings.sidecar.ask_once": ("询问一次", "Ask once"),
    "settings.sidecar.always": ("始终联动", "Always"),
    "settings.sidecar.never": ("从不", "Never"),
    "settings.sidecar.hide": ("伴随文件不单独进入分类队列", "Hide sidecars from the sorting queue"),
    "settings.sidecar.hide.desc": (
        "RAW 与 JPG 只出现一次，避免同一张照片被看两遍",
        "A photo appears once, not twice as RAW and JPG",
    ),
    "settings.sidecar.groups.raw": ("RAW", "RAW"),
    "settings.sidecar.groups.metadata": ("元数据", "Metadata"),
    "settings.sidecar.groups.live": ("实况照片", "Live Photo"),

    "settings.verification": ("校验强度", "Verification"),
    "settings.verification.desc": (
        "完整校验对每份副本重算 SHA-256；快速模式只比对大小与修改时间，明显更快但保护更弱",
        "Full re-hashes every copy with SHA-256; Fast only compares size and mtime — quicker, weaker",
    ),
    "settings.verification.full": ("完整校验", "Full"),
    "settings.verification.fast": ("快速", "Fast"),
    "settings.fastpath": ("同卷移动走快速路径", "Fast path for same-volume moves"),
    "settings.fastpath.desc": (
        "不复制、不回读，直接改目录项；撤销靠反向移动而不是快照。"
        "移动 5 GB 视频从约 15 GB 读写降到几乎为零",
        "Renames in place instead of copy + verify; undo is a reverse move, not a snapshot. "
        "A 5 GB video drops from about 15 GB of I/O to almost none",
    ),
    "settings.quota": ("快照配额与自动回收", "Snapshot quota & auto-reclaim"),
    "settings.recycle": ("回收站行为", "Recycle behaviour"),
    "settings.recycle.soft": ("同盘隐藏文件夹", "Hidden folder on the same drive"),
    "settings.recycle.soft.desc": (
        "默认：移到文件旁边的隐藏文件夹，瞬间完成、不复制，Ctrl+Z 可恢复，按保留策略自动清理。"
        "Windows 回收站：从回收站手动还原，Ctrl+Z 不能撤销",
        "Default: moved into a hidden folder beside the file — instant, nothing copied, "
        "Ctrl+Z restores it, cleared by the retention policy. "
        "Windows recycle bin: restore it from the bin; Ctrl+Z cannot undo it",
    ),
    "settings.recycle.system": ("Windows 回收站", "Windows recycle bin"),
    "settings.recycle.system.desc": (
        "从系统回收站手动还原，Ctrl+Z 不能撤销",
        "Restore it from the recycle bin; Ctrl+Z cannot undo it",
    ),
    "settings.folder_menu": ("资源管理器右键菜单", "Explorer folder menu"),
    "settings.folder_menu.desc": (
        "在文件夹上右键即可“用轻拣打开”；轻拣已经开着时会直接切到那个文件夹。只写当前用户的注册表，不需要管理员权限",
        "Right-click a folder to open it here; if Qingjian is already open it switches to "
        "that folder. Written for this user only, no administrator rights needed",
    ),
    "settings.logging": ("保留运行日志", "Keep a run log"),
    "settings.logging.desc": (
        "出错时可导出诊断包，含日志、事务记录与环境信息，不含你的媒体文件",
        "Export a diagnostic bundle with logs, transaction records and environment — never your media",
    ),
    "settings.export_bundle": ("导出诊断包", "Export bundle"),
    "settings.background_queue": ("后台执行分类操作", "Run sorting in the background"),
    "settings.background_queue.desc": (
        "按下数字键立刻翻到下一张，文件操作排队在后台完成；失败会回插到队列并红字提示",
        "A number key advances immediately; the file operation queues behind it. "
        "A failure re-inserts the item and says so",
    ),
    "settings.workers": ("并发线程", "Worker threads"),
    "settings.thumb_cache": ("缩略图缓存上限", "Thumbnail cache cap"),
    "settings.hash_cache": ("缓存查重哈希", "Cache duplicate hashes"),
    "settings.hash_cache.desc": (
        "按路径 + 大小 + 修改时间缓存 SHA-256，重复查重不再整盘重算",
        "Keys SHA-256 by path + size + mtime, so a re-scan does not re-read the disk",
    ),
    "settings.header_only_orientation": ("方向筛选只读文件头", "Read headers only for orientation filters"),
    "settings.header_only_orientation.desc": (
        "横向 / 纵向筛选不再整张解码，几千张图从分钟级降到秒级",
        "Landscape / portrait filters stop fully decoding each image — minutes become seconds",
    ),
    "settings.version": ("版本", "Version"),
    "settings.up_to_date": ("已是最新", "Up to date"),

    # ---- templates -----------------------------------------------------
    "tpl.title": ("目标路径与命名模板", "Target path & naming template"),
    "tpl.applies_to": (
        "应用于方案「{preset}」· 按键 {key} · {target}",
        "Applies to preset “{preset}” · key {key} · {target}",
    ),
    "tpl.path": ("目标路径模板", "Target path template"),
    "tpl.name": ("文件命名模板", "Filename template"),
    "tpl.seq_start": ("序号起始", "Start at"),
    "tpl.seq_digits": ("位数", "Digits"),
    "tpl.on_clash": ("同名冲突", "On name clash"),
    "tpl.date_fallback": ("日期缺失时回退到修改时间", "Fall back to mtime when no date"),
    "tpl.preview": ("实时预览", "Live preview"),
    "tpl.preview_note": ("取自当前队列的前 {count} 项", "First {count} items of the current queue"),
    "tpl.valid": ("模板有效 · 无非法字符", "Template valid · no illegal characters"),
    "tpl.invalid": ("模板无效：{error}", "Template invalid: {error}"),
    "tpl.badge.sidecar": ("伴随联动", "Sidecar"),
    "tpl.badge.fallback": ("回退", "Fallback"),
    "tpl.badge.sequenced": ("加序号", "Sequenced"),
    "tpl.apply_to_preset": ("应用到方案", "Apply to preset"),
    "tpl.unknown_token": ("未知占位符 {token}", "Unknown placeholder {token}"),
    "tpl.unbalanced": ("花括号不匹配", "Unbalanced braces"),
    "tpl.absolute_not_allowed": ("模板不能是绝对路径", "A template cannot be an absolute path"),
    "tpl.escapes_root": ("模板不能跳出目标目录", "A template cannot escape the target folder"),

    # ---- bindings dialog -----------------------------------------------
    "bind.title": ("快捷键、动作与目标文件夹", "Keys, actions and target folders"),
    "bind.subtitle": (
        "每个键都能绑定移动、复制、收藏、稍后处理、重命名、回收站、定位文件或打标签。",
        "Each key can be bound to move, copy, favorite, review later, rename, recycle, reveal or tag.",
    ),
    "bind.target_folder": ("目标文件夹", "Target folder"),
    "bind.choose_for": ("为 {key} 选择目标文件夹", "Choose a target folder for {key}"),
    "bind.duplicate_key": ("快捷键 {key} 重复，请改成不同的键。", "Key {key} is used twice — pick another."),
    "bind.reserved_key": (
        "快捷键 {key} 已被窗口占用（例如 G 网格、S 稍后、J K L 视频、M 静音），请换一个键。",
        "Key {key} is already used by the window (G grid, S later, J K L video, M mute) — pick another.",
    ),
    "bind.reset_keys": ("重置为 1–0", "Reset to 1–0"),
    "bind.edit_template": ("编辑模板", "Edit template"),

    # ---- queue ----------------------------------------------------------
    "queue.title": ("后台队列", "Background queue"),
    "queue.pending": ("等待中", "Pending"),
    "queue.running": ("执行中", "Running"),
    "queue.failed": ("失败", "Failed"),
    "queue.retry_all": ("全部重试", "Retry all"),
    "queue.drain": ("等待队列完成…", "Waiting for the queue to drain…"),
    "queue.item_failed": ("{name} 未完成：{error}", "{name} did not complete: {error}"),
}


def _fmt_keys(text: str) -> frozenset[str]:
    return frozenset(_PLACEHOLDER.findall(text))


class Translator:
    """Holds the active language and resolves keys against :data:`CATALOG`."""

    def __init__(self, language: str = DEFAULT_LANGUAGE, catalog: dict | None = None) -> None:
        self._catalog = catalog if catalog is not None else CATALOG
        self._index = 0
        self._listeners: list[Callable[[str], None]] = []
        self.missing: set[str] = set()
        self.set_language(language)

    # -- language ------------------------------------------------------
    @property
    def language(self) -> str:
        return LANGUAGE_CODES[self._index]

    def set_language(self, language: str) -> str:
        code = self.resolve(language)
        self._index = LANGUAGE_CODES.index(code)
        for listener in list(self._listeners):
            listener(code)
        return code

    @staticmethod
    def resolve(language: str | None) -> str:
        """Map a user setting (including ``system``/``auto``) to a real code."""
        value = (language or "").strip().lower()
        if value in LANGUAGE_CODES:
            return value
        if value in ("", "system", "auto"):
            tag = ""
            for name in ("QINGJIAN_LANG", "LC_ALL", "LC_MESSAGES", "LANG"):
                tag = os.environ.get(name) or ""
                if tag:
                    break
            if not tag:
                try:
                    tag = locale.getlocale()[0] or ""
                except (ValueError, TypeError):  # pragma: no cover - platform dependent
                    tag = ""
            tag = tag.replace("-", "_").lower()
            if tag.startswith("zh"):
                return "zh"
            if tag:
                return "en"
            return DEFAULT_LANGUAGE
        # A specific but unsupported tag: fall back to English rather than Chinese.
        return "en" if not value.startswith("zh") else "zh"

    def on_change(self, listener: Callable[[str], None]) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener) if listener in self._listeners else None

    # -- lookup --------------------------------------------------------
    def tr(self, key: str, /, **fields: object) -> str:
        entry = self._catalog.get(key)
        if entry is None:
            self.missing.add(key)
            return key
        text = entry[self._index]
        if not fields and "{" not in text:
            return text
        try:
            return text.format(**fields)
        except (KeyError, IndexError, ValueError):
            # A missing or misspelled field must never take the window down;
            # record it so the catalogue test can catch it instead.
            self.missing.add(key + " (format)")
            return text

    def has(self, key: str) -> bool:
        return key in self._catalog

    def keys(self) -> Iterable[str]:
        return self._catalog.keys()


_translator = Translator(DEFAULT_LANGUAGE)


def translator() -> Translator:
    return _translator


def set_language(language: str) -> str:
    return _translator.set_language(language)


def get_language() -> str:
    return _translator.language


def on_language_change(listener: Callable[[str], None]) -> Callable[[], None]:
    return _translator.on_change(listener)


def tr(key: str, /, **fields: object) -> str:
    return _translator.tr(key, **fields)


def catalog_problems(catalog: dict[str, tuple[str, str]] | None = None) -> list[str]:
    """Return a list of catalogue defects. Empty means the catalogue is sound."""
    data = catalog if catalog is not None else CATALOG
    problems: list[str] = []
    for key, entry in sorted(data.items()):
        if not isinstance(entry, tuple) or len(entry) != len(LANGUAGE_CODES):
            problems.append(f"{key}: expected {len(LANGUAGE_CODES)} translations")
            continue
        for code, text in zip(LANGUAGE_CODES, entry):
            if not isinstance(text, str) or not text.strip():
                problems.append(f"{key}[{code}]: empty")
        if len(entry) == 2 and all(isinstance(t, str) for t in entry):
            zh_keys, en_keys = _fmt_keys(entry[0]), _fmt_keys(entry[1])
            if zh_keys != en_keys:
                problems.append(
                    f"{key}: placeholders differ zh={sorted(zh_keys)} en={sorted(en_keys)}"
                )
    return problems
