# -*- coding: utf-8 -*-
"""
CSV 复核报告生成
================

职责：把保留 / 删除决策导出为 CSV 文件，供**人工复核**后决定是否执行删除。

报告内容（每个视频一行，覆盖全部扫描到的视频）：
    - 身份信息：路径、大小、时长、分辨率、码率、修改时间；
    - 相似度：pHash 初筛 / dHash 精筛 / 融合相似度（与组内其他成员的平均值）；
    - 保留评分：内容差异 / 码率 / 分辨率 / 入库时间 四个分项 + 保留总分
      （文件级快速判重的副本件同样列出评分，人工复核时两边分数都看得到）；
    - 处置决定：KEEP（保留）/ DELETE（待删除）/ UNIQUE（唯一）
      / SUSPECT（疑似重复：疑似线与判重线之间，人工判断）/ CORRUPT（无法解码，人工检查）；
    - 人工复核：留空 —— 复核者填「保留 / 待删除 / 唯一」即可改判个别行，
      配合 --reviewed-report 传回程序生效；
    - 判定来源：帧级比对 / 文件级快速判重 / 未比对（区分快判与精判）；
    - 判定依据：人类可读的完整推理链（含比对阶段与分项分数）。

输出两个文件：
    1. CSV（REPORT_PATH）：明细复核报告，人工改判走这个文件；
    2. XLSX（同名 .xlsx）：双 sheet —— 「明细报告」与「分组报告」。
       分组报告按「待删除视频组 N → 保留行（保留依据）→ 删除行（删除依据）」
       的分组版式排布，并汇总疑似 / 无法解码清单，便于快速过目。

编码使用 utf-8-sig，双击用 Excel 打开不乱码。
"""

import csv
import logging
import os
import time
from typing import Dict, List, Optional

from models import RetentionDecision, VideoMetadata

logger = logging.getLogger("VideoDeduplication")

CSV_HEADERS = [
    "组ID", "视频路径", "文件大小(MB)", "时长(秒)", "分辨率", "码率(kbps)", "修改时间",
    "pHash相似度", "dHash相似度", "综合相似度",
    "内容得分", "码率得分", "分辨率得分", "时间得分", "字幕得分", "保留总分",
    "处置决定", "人工复核", "判定来源", "判定依据",
]

_ACTION_LABELS = {
    "KEEP": "保留",
    "DELETE": "待删除",
    "UNIQUE": "唯一",
    "SUSPECT": "疑似重复",
    "CORRUPT": "无法解码",
}

# 人工复核允许填写的目标动作（只开放三类主结论，防止误填）
_REVIEW_LABELS = {"保留": "KEEP", "待删除": "DELETE", "唯一": "UNIQUE"}


def _fmt_float(value: Optional[float], digits: int = 1) -> str:
    """数值格式化：None 输出空字符串，否则保留指定位小数。"""
    if value is None:
        return ""
    return f"{value:.{digits}f}"


def _fmt_mtime(timestamp: float) -> str:
    import time
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))


def _build_row(decision: RetentionDecision, meta: Optional[VideoMetadata]) -> List[str]:
    """把一条决策 + 元数据拼成报告行（CSV 与 xlsx 明细 sheet 共用）。"""
    return [
        decision.group_id if decision.group_id >= 0 else "",
        decision.video_path,
        _fmt_float(meta.file_size_mb) if meta else "",
        _fmt_float(meta.duration_sec) if meta else "",
        meta.resolution_text if meta else "",
        _fmt_float(meta.bitrate_kbps) if meta and meta.bitrate_kbps else "",
        _fmt_mtime(meta.file_mtime) if meta else "",
        _fmt_float(decision.phash_similarity),
        _fmt_float(decision.dhash_similarity),
        _fmt_float(decision.final_similarity),
        _fmt_float(decision.content_score),
        _fmt_float(decision.bitrate_score),
        _fmt_float(decision.resolution_score),
        _fmt_float(decision.creation_time_score),
        _fmt_float(decision.subtitle_score),
        _fmt_float(decision.total_score),
        _ACTION_LABELS.get(decision.action, decision.action),
        "",  # 人工复核：留空待填（保留/待删除/唯一），配合 --reviewed-report 生效
        decision.decision_source,
        decision.reason,
    ]


def write_csv_report(
    report_path: str,
    metadatas: Dict[str, VideoMetadata],
    decisions: List[RetentionDecision],
) -> str:
    """
    生成 CSV 复核报告，返回报告文件路径。
    报告在删除执行**之前**生成 —— 它就是人工复核的依据。
    """
    os.makedirs(os.path.dirname(os.path.abspath(report_path)) or ".", exist_ok=True)

    with open(report_path, "w", newline="", encoding="utf-8-sig") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(CSV_HEADERS)

        for decision in decisions:
            writer.writerow(_build_row(decision, metadatas.get(decision.video_path)))

    delete_count = sum(1 for d in decisions if d.action == "DELETE")
    logger.info(
        "CSV 复核报告已生成: %s（共 %d 行，其中待删除 %d 行）",
        report_path, len(decisions), delete_count,
    )
    return report_path


def load_review_overrides(report_path: str) -> Dict[str, str]:
    """
    读取人工复核后的 CSV 报告，提取改判决定。

    「人工复核」列填 保留 / 待删除 / 唯一 的行 → {视频路径: 目标动作(KEEP/DELETE/UNIQUE)}；
    空白行不干预（维持程序自动判定）。列名找不到或文件不存在时返回空字典。
    """
    label_to_action = dict(_REVIEW_LABELS)
    overrides: Dict[str, str] = {}
    if not report_path or not os.path.isfile(report_path):
        logger.warning("人工复核报告不存在，跳过改判: %s", report_path)
        return overrides

    with open(report_path, "r", newline="", encoding="utf-8-sig") as csv_file:
        reader = csv.reader(csv_file)
        headers = next(reader, [])
        try:
            path_index = headers.index("视频路径")
            review_index = headers.index("人工复核")
        except ValueError:
            logger.warning("人工复核报告缺少「视频路径 / 人工复核」列，跳过改判: %s", report_path)
            return overrides

        for row in reader:
            if len(row) <= max(path_index, review_index):
                continue
            label = row[review_index].strip()
            if label in label_to_action:
                overrides[row[path_index]] = label_to_action[label]

    return overrides


# =====================================================================
# XLSX 双 sheet 报告（明细 + 分组版式）
# =====================================================================

_GROUP_SHEET_HEADERS = [
    "处置", "视频路径", "文件大小(MB)", "时长(秒)", "分辨率", "码率(kbps)",
    "修改时间", "综合相似度", "保留总分", "判定来源", "保留/删除依据",
]

# 分组 sheet 内每行的处置文字 →（行样式提示：字体色）
_GROUP_ROW_STYLES = {
    "保留": "1B5E20",
    "待删除": "B71C1C",
    "疑似重复": "E65100",
    "无法解码": "6A1B9A",
}


def _group_sheet_row(decision: RetentionDecision, meta: Optional[VideoMetadata], action_text: str) -> List[str]:
    """分组 sheet 的一行（列比明细少，突出依据）。"""
    return [
        action_text,
        decision.video_path,
        _fmt_float(meta.file_size_mb) if meta else "",
        _fmt_float(meta.duration_sec) if meta else "",
        meta.resolution_text if meta else "",
        _fmt_float(meta.bitrate_kbps) if meta and meta.bitrate_kbps else "",
        _fmt_mtime(meta.file_mtime) if meta else "",
        _fmt_float(decision.final_similarity),
        _fmt_float(decision.total_score),
        decision.decision_source,
        decision.reason,
    ]


def write_xlsx_report(
    report_path: str,
    metadatas: Dict[str, VideoMetadata],
    decisions: List[RetentionDecision],
) -> Optional[str]:
    """
    生成双 sheet 的 Excel 报告（与 CSV 同名、扩展名 .xlsx），返回文件路径。

    sheet「明细报告」：与 CSV 完全一致的逐视频明细；
    sheet「分组报告」：按「待删除视频组 N → 保留行（保留依据）→ 删除行（删除依据）」
        的分组版式排布，随后附「疑似重复」与「无法解码」清单，末尾唯一视频计数。
    未安装 openpyxl 时跳过并告警（CSV 不受影响）。
    """
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError:
        logger.warning("未安装 openpyxl，跳过 Excel 报告（pip install openpyxl 后重跑即可生成）")
        return None

    xlsx_path = os.path.splitext(report_path)[0] + ".xlsx"
    workbook = Workbook()

    # ---------- sheet 1：明细报告（与 CSV 同构） ----------
    detail_sheet = workbook.active
    detail_sheet.title = "明细报告"
    detail_sheet.append(CSV_HEADERS)
    for decision in decisions:
        detail_sheet.append(_build_row(decision, metadatas.get(decision.video_path)))
    for cell in detail_sheet[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="EFEFEF")
    detail_sheet.freeze_panes = "A2"

    # ---------- sheet 2：分组报告（用户约定的分组版式） ----------
    group_sheet = workbook.create_sheet("分组报告")
    delete_decisions = [d for d in decisions if d.action == "DELETE"]
    delete_bytes = sum(
        metadatas[d.video_path].file_size for d in delete_decisions if d.video_path in metadatas
    )
    counts = {label: 0 for label in _ACTION_LABELS.values()}
    for d in decisions:
        counts[_ACTION_LABELS.get(d.action, d.action)] += 1
    group_ids = sorted({d.group_id for d in decisions if d.group_id >= 0})

    # 首行统计摘要
    summary = (
        f"生成时间 {time.strftime('%Y-%m-%d %H:%M:%S')}｜视频总数 {len(decisions)}"
        f"｜重复组 {len(group_ids)} 组｜待删除 {counts['待删除']} 个（约释放 {delete_bytes / 1024 / 1024:.1f} MB）"
        f"｜疑似重复 {counts['疑似重复']} 个｜无法解码 {counts['无法解码']} 个｜唯一 {counts['唯一']} 个"
    )
    group_sheet.append(["视频去重分组报告"])
    group_sheet["A1"].font = Font(bold=True, size=13)
    group_sheet.append([summary])
    group_sheet.append([])

    def _write_block(title: str, rows: List[List[str]]) -> None:
        group_sheet.append([title])
        title_cell = group_sheet.cell(row=group_sheet.max_row, column=1)
        title_cell.font = Font(bold=True, size=11)
        title_cell.fill = PatternFill("solid", fgColor="FFF3E0")
        group_sheet.append(_GROUP_SHEET_HEADERS)
        for cell in group_sheet[group_sheet.max_row]:
            cell.font = Font(bold=True)
        for row in rows:
            group_sheet.append(row)
            action_text = row[0]
            color = _GROUP_ROW_STYLES.get(action_text)
            if color:
                group_sheet.cell(row=group_sheet.max_row, column=1).font = Font(bold=True, color=color)
        group_sheet.append([])  # 块间空行

    # 逐组：保留行在前，删除行在后（用户约定的阅读顺序）
    for group_id in group_ids:
        members = [d for d in decisions if d.group_id == group_id]
        members.sort(key=lambda d: (0 if d.action == "KEEP" else 1, d.video_path))
        rows = [
            _group_sheet_row(d, metadatas.get(d.video_path), _ACTION_LABELS.get(d.action, d.action))
            for d in members
        ]
        _write_block(f"待删除视频组 {group_id}（{len(members)} 个成员）", rows)

    # 疑似重复 / 无法解码 清单（不自动删，交人工）
    suspect_decisions = [d for d in decisions if d.action == "SUSPECT"]
    if suspect_decisions:
        rows = [
            _group_sheet_row(d, metadatas.get(d.video_path), "疑似重复")
            for d in suspect_decisions
        ]
        _write_block("疑似重复（未自动删除，请人工判断）", rows)

    corrupt_decisions = [d for d in decisions if d.action == "CORRUPT"]
    if corrupt_decisions:
        rows = [
            _group_sheet_row(d, metadatas.get(d.video_path), "无法解码")
            for d in corrupt_decisions
        ]
        _write_block("无法解码（请人工检查文件完整性）", rows)

    group_sheet.append(
        [f"唯一视频 {counts['唯一']} 个：未达疑似线，无需处理（明细见「明细报告」sheet）"]
    )

    # 列宽微调
    for sheet, widths in (
        (detail_sheet, {2: 60, 20: 80}),
        (group_sheet, {2: 60, 11: 80}),
    ):
        for col_index, width in widths.items():
            sheet.column_dimensions[get_column_letter(col_index)].width = width
        for row in sheet.iter_rows():
            for cell in row:
                cell.alignment = Alignment(vertical="top", wrap_text=False)

    workbook.save(xlsx_path)
    logger.info("Excel 分组报告已生成: %s（sheet：明细报告 / 分组报告）", xlsx_path)
    return xlsx_path
