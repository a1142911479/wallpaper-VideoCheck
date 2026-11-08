# -*- coding: utf-8 -*-
"""
相似度计算（两级筛选）
======================

职责：给定一对视频的指纹，计算相似度并判定是否重复。

两级筛选（先便宜后昂贵，先粗后细）：
    1. 初筛（pHash）：用「固定间隔帧」的感知哈希，按帧序号对齐比对。
       时间轴规整（0, 30, 60 ...），两视频第 i 帧直接成对；
       短视频对则按「首/中/尾」序号语义对齐。
       相似度 < PHASH_SCREENING_THRESHOLD → 直接判不重复，省掉精筛。
    2. 精筛（dHash）：用「场景切换帧」的差异哈希。
       两视频关键帧时间戳不严格对齐，按时间戳就近匹配（容差 3s）后比对。

融合判定（三级结论）：
    final = pHash × PHASH_WEIGHT + dHash × DHASH_WEIGHT
    final ≥ FINAL_SIMILARITY_THRESHOLD   → 判定为重复（进删除流程）；
    SUSPECT ≤ final < FINAL              → 判定为疑似重复（不自动删，交人工判断）；
    final < SUSPECT                      → 判定为不重复。

统一采用 0-100 分制（旧代码 0-1 与 0-100 混用的 bug 已修复）。
"""

import logging
from typing import Dict, List, Optional, Tuple

from config import Config
from models import PairComparison, VideoFingerprint
from utils import PipelineCancelled, hash_similarity

logger = logging.getLogger("VideoDeduplication")


# =====================================================================
# 一、关键帧对齐
# =====================================================================

def match_frame_pairs(
    frames_a: list,
    frames_b: list,
    tolerance_sec: float,
    align_by_index: bool = False,
) -> List[Tuple[object, object]]:
    """
    把两组关键帧配成一一对应的「帧对」。

    align_by_index=True（短视频对）：
        按序号对齐 —— 首对首、中对中、尾对尾（两视频抽帧语义一致）。
    否则（普通视频对）：
        按时间戳就近匹配，且距离不超过 tolerance_sec，每个帧最多参与一对。
    """
    if align_by_index:
        return list(zip(frames_a, frames_b))

    pairs: List[Tuple[object, object]] = []
    used_indices = set()
    for frame_a in frames_a:
        best_index = None
        best_delta = tolerance_sec
        for index_b, frame_b in enumerate(frames_b):
            if index_b in used_indices:
                continue
            delta = abs(frame_a.timestamp_sec - frame_b.timestamp_sec)
            if delta <= best_delta:
                best_delta = delta
                best_index = index_b
        if best_index is not None:
            used_indices.add(best_index)
            pairs.append((frame_a, frames_b[best_index]))
    return pairs


def average_similarity(frame_pairs: List[Tuple[object, object]], hash_attr: str) -> Optional[float]:
    """对帧对列表计算平均汉明相似度（0-100）；没有有效帧对时返回 None。"""
    if not frame_pairs:
        return None
    total = 0.0
    for frame_a, frame_b in frame_pairs:
        total += hash_similarity(getattr(frame_a, hash_attr), getattr(frame_b, hash_attr))
    return total / len(frame_pairs)


# =====================================================================
# 二、两级相似度判定
# =====================================================================

def evaluate_pair(
    fingerprint_a: VideoFingerprint,
    fingerprint_b: VideoFingerprint,
    config: Config,
) -> PairComparison:
    """
    计算一对视频的相似度并判定是否重复。

    流程：pHash 初筛 →（通过后）dHash 精筛 → 加权融合 → 阈值判定。
    """
    path_a = fingerprint_a.video_path
    path_b = fingerprint_b.video_path

    # 短视频对：初筛帧与精筛帧都是「首/中/尾」，按序号语义对齐
    align_by_index = fingerprint_a.is_short_video and fingerprint_b.is_short_video

    # ---------- 第 1 级：pHash 初筛 ----------
    fixed_pairs = match_frame_pairs(
        fingerprint_a.fixed_frames,
        fingerprint_b.fixed_frames,
        tolerance_sec=config.KEYFRAME_MATCH_TOLERANCE_SEC,
        align_by_index=align_by_index,
    )
    phash_similarity = average_similarity(fixed_pairs, "phash")

    if phash_similarity is None:
        return PairComparison(
            path_a=path_a, path_b=path_b,
            phash_similarity=None, dhash_similarity=None, final_similarity=None,
            is_duplicate=False,
            stage_note="初筛帧缺失，无法比对",
        )

    if phash_similarity < config.PHASH_SCREENING_THRESHOLD:
        return PairComparison(
            path_a=path_a, path_b=path_b,
            phash_similarity=phash_similarity,
            dhash_similarity=None, final_similarity=None,
            is_duplicate=False,
            stage_note=f"初筛未通过（pHash {phash_similarity:.1f} < {config.PHASH_SCREENING_THRESHOLD:.1f}）",
        )

    # ---------- 第 2 级：dHash 精筛 ----------
    scene_pairs = match_frame_pairs(
        fingerprint_a.scene_frames,
        fingerprint_b.scene_frames,
        tolerance_sec=config.KEYFRAME_MATCH_TOLERANCE_SEC,
        align_by_index=align_by_index,
    )
    dhash_similarity = average_similarity(scene_pairs, "dhash")

    if dhash_similarity is None:
        # 精筛帧缺失（异常情况）：退回仅用初筛相似度判定
        final_similarity = phash_similarity
        is_duplicate = final_similarity >= config.FINAL_SIMILARITY_THRESHOLD
        is_suspect = (
            not is_duplicate
            and final_similarity >= config.SUSPECT_SIMILARITY_THRESHOLD
        )
        stage_note = f"精筛帧缺失，按初筛判定（pHash {phash_similarity:.1f}）"
    else:
        final_similarity = (
            phash_similarity * config.PHASH_WEIGHT
            + dhash_similarity * config.DHASH_WEIGHT
        )
        is_duplicate = final_similarity >= config.FINAL_SIMILARITY_THRESHOLD
        is_suspect = (
            not is_duplicate
            and final_similarity >= config.SUSPECT_SIMILARITY_THRESHOLD
        )
        stage_note = (
            f"初筛通过（pHash {phash_similarity:.1f}）→ 精筛（dHash {dhash_similarity:.1f}）"
            f"→ 融合 {final_similarity:.1f} {'≥' if is_duplicate else '<'} {config.FINAL_SIMILARITY_THRESHOLD:.1f}"
        )

    if is_suspect:
        stage_note += (
            f"｜疑似重复（{config.SUSPECT_SIMILARITY_THRESHOLD:.1f} ≤ 融合 < "
            f"{config.FINAL_SIMILARITY_THRESHOLD:.1f}），需人工确认"
        )

    return PairComparison(
        path_a=path_a, path_b=path_b,
        phash_similarity=phash_similarity,
        dhash_similarity=dhash_similarity,
        final_similarity=final_similarity,
        is_duplicate=is_duplicate,
        is_suspect=is_suspect,
        stage_note=stage_note,
    )


def find_duplicate_pairs(
    candidate_pairs: List[Tuple[str, str]],
    fingerprints: Dict[str, VideoFingerprint],
    config: Config,
    cancel_check=None,
) -> List[PairComparison]:
    """
    对全部候选视频对执行两级比对，返回判定为「重复」的结果列表。
    所有结果（含未通过的）也会返回，供报告展示判定依据。
    cancel_check：协作式取消探针（GUI「停止」按钮），返回 True 时中断流水线。
    """
    comparisons: List[PairComparison] = []
    duplicate_count = 0
    suspect_count = 0
    for path_a, path_b in candidate_pairs:
        if cancel_check is not None and cancel_check():
            raise PipelineCancelled("比对阶段被用户取消")
        fp_a = fingerprints.get(path_a)
        fp_b = fingerprints.get(path_b)
        if fp_a is None or fp_b is None:
            logger.warning("缺少指纹，跳过比对: %s ↔ %s", path_a, path_b)
            continue
        comparison = evaluate_pair(fp_a, fp_b, config)
        comparisons.append(comparison)
        if comparison.is_duplicate:
            duplicate_count += 1
        elif comparison.is_suspect:
            suspect_count += 1

    logger.info(
        "比对完成：%d 对候选中 %d 对判定为重复（判重线 %.1f 分），%d 对疑似重复（疑似线 %.1f 分）",
        len(comparisons), duplicate_count, config.FINAL_SIMILARITY_THRESHOLD,
        suspect_count, config.SUSPECT_SIMILARITY_THRESHOLD,
    )
    return comparisons
