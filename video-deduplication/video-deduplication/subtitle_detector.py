# -*- coding: utf-8 -*-
"""
字幕检测与评分
==============

职责：为视频计算「字幕得分」（0-100），作为保留评分的第五维权重 ——
同组内自动优先保留有字幕的版本（仅对时长 > SUBTITLE_CHECK_MIN_SEC 的视频计算）。

两条检测路径（成本从低到高）：
1. 软字幕（字幕轨）检测 —— 零解码：
   读文件头部 + 尾部各 2MB 原始字节，搜索容器的字幕流标记：
       MP4/M4V : tx3g（3GPP 定时文本）/ sbtl / subp（字幕轨 handler）/ c608 / c708（CEA 闭路字幕）
       MKV/WebM: S_TEXT/（UTF8/ASS/SSA）/ S_HDMV/（蓝光 PGS）/ S_VOBSUB / S_DVBSUB
   命中任一标记 ⇒ 判定「有软字幕轨」→ 直接 100 分。
   （moov 原子可能在文件头也可能在文件尾，所以头尾都扫。）

2. 硬字幕（烧录字幕）启发式 —— 用已抽取的关键帧，零额外解码：
   烧录字幕混在画面里，元数据看不出来，但有明显视觉特征 ——
   画面**底部 20% 条带**内出现高密度的文字边缘。
   该条带转灰度 → Canny 边缘检测 → 统计边缘像素占比：
       字幕文字的边缘占比通常 2%~8%；普通画面底部多为平滑背景/地面/字幕条以下区域，< 2%。
   得分 = min(100, 边缘占比 / SUBTITLE_EDGE_DENSITY_CAP × 100)，多帧取平均。

组合规则：软字幕命中 → 100 分；否则用硬字幕启发式得分。

注意：这是**启发式评分**而非精确识别（复杂底部纹理如滚动条/字幕栏可能虚高），
因此只用作保留评分的一个维度（默认权重 15%），不单独做删留决定。
"""

import logging
import os
from typing import List

import cv2
import numpy as np

from config import Config

logger = logging.getLogger("VideoDeduplication")

# 软字幕容器标记（字节串）。故意不用裸 b"text" —— 在 MP4 元数据里过于常见，易误报。
_MP4_SUBTITLE_MARKERS = (b"tx3g", b"sbtl", b"subp", b"c608", b"c708")
_MKV_SUBTITLE_MARKERS = (b"S_TEXT/", b"S_HDMV/", b"S_VOBSUB", b"S_DVBSUB")


def detect_soft_subtitle(video_path: str, probe_bytes: int = 2 * 1024 * 1024) -> bool:
    """
    零解码检测「软字幕轨」：读文件头 + 尾各 probe_bytes 字节，搜索容器字幕流标记。
    头尾都扫是因为 MP4 的 moov 原子位置不确定（faststart 在头，普通导出在尾）。
    文件太小 / 读失败时返回 False（不阻塞主流程）。
    """
    file_size = os.path.getsize(video_path)
    markers = _MP4_SUBTITLE_MARKERS + _MKV_SUBTITLE_MARKERS
    try:
        with open(video_path, "rb") as file:
            head = file.read(min(probe_bytes, file_size))
            tail = b""
            if file_size > probe_bytes:
                file.seek(max(0, file_size - probe_bytes))
                tail = file.read(probe_bytes)
    except OSError as error:
        logger.warning("软字幕检测读文件失败（跳过）: %s（%s）", video_path, error)
        return False

    blob = head + tail
    return any(marker in blob for marker in markers)


def hard_subtitle_score(frame, config: Config) -> float:
    """
    硬字幕启发式得分（0-100）：画面底部 20% 条带的文字边缘密度。

    - 裁底部 20% → 灰度 → Canny → 边缘像素占比；
    - 占比 / SUBTITLE_EDGE_DENSITY_CAP 归一化到 0-100（CAP 即满分线，默认 5%）；
    - 帧太小 / 灰度失败时返回 0。
    """
    height, width = frame.shape[:2]
    if height < 10 or width < 10:
        return 0.0
    band = frame[int(height * 0.8):, :]
    gray = cv2.cvtColor(band, cv2.COLOR_BGR2GRAY) if band.ndim == 3 else band
    if gray.size == 0:
        return 0.0
    edges = cv2.Canny(gray, 50, 150)
    edge_ratio = float(np.count_nonzero(edges)) / float(edges.size)
    cap = max(1e-6, config.SUBTITLE_EDGE_DENSITY_CAP)
    return min(100.0, edge_ratio / cap * 100.0)


def compute_subtitle_score(
    video_path: str,
    hard_scores: List[float],
    config: Config,
) -> float:
    """
    组合字幕得分（0-100）：
        软字幕轨命中 → 100；
        否则 → 硬字幕启发式得分的帧平均（无采样帧时 0）。
    """
    if detect_soft_subtitle(video_path):
        logger.debug("检测到软字幕轨: %s", video_path)
        return 100.0
    if not hard_scores:
        return 0.0
    return sum(hard_scores) / len(hard_scores)
