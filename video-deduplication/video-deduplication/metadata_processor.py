# -*- coding: utf-8 -*-
"""
元数据处理与分组
================

职责：
1. collect_video_paths：收集视频路径并**按路径排序** ——
   NAS 机械硬盘顺序读的关键（避免随机寻道）；
2. group_videos_by_size：按文件大小分组 ——
   大小完全相同的文件是「完全拷贝」的强信号，组成快速候选对；
3. group_videos_by_duration：按时长区间分组 ——
   只在组内两两比对，把 O(N²) 全量比对压缩到组内比对。
   其中时长 ≤ SHORT_VIDEO_MAX_SEC 的视频全部归入同一个「短视频组」（用户约定）。

候选对 = 「同大小组的配对」∪「同时长组的配对」：
   - 同大小：抓「完全相同的拷贝」；
   - 同时长：抓「转码 / 压缩 / 轻微修改」的近似重复（哈希相似度判定）。
"""

import logging
import os
from collections import defaultdict
from typing import Dict, Iterable, List, Set, Tuple

from config import Config
from models import VideoMetadata

logger = logging.getLogger("VideoDeduplication")


def collect_video_paths(video_dir: str, extensions: Iterable[str]) -> List[str]:
    """
    递归收集目录下的视频文件路径，并**按路径字典序排序**。

    排序原因：视频存放在 NAS 机械硬盘上，按序访问可显著减少磁头寻道，
    提高顺序读吞吐（配合后端单线程顺序解码，见 main.py）。
    """
    if not os.path.isdir(video_dir):
        raise FileNotFoundError(f"视频目录不存在: {video_dir}")

    ext_set = {ext.lower() for ext in extensions}
    paths: List[str] = []
    for root, dir_names, file_names in os.walk(video_dir):
        dir_names.sort()          # 子目录也排序，保证遍历顺序稳定
        for file_name in sorted(file_names):
            if os.path.splitext(file_name)[1].lower() in ext_set:
                paths.append(os.path.join(root, file_name))

    paths.sort()
    logger.info(
        "共发现 %d 个视频文件（已按路径排序，顺序读取 NAS）: %s",
        len(paths), video_dir,
    )
    return paths


def group_videos_by_size(metadatas: Dict[str, VideoMetadata]) -> Dict[int, List[str]]:
    """按文件大小（字节）分组，返回 {大小: [路径列表]}，只保留 ≥2 个成员的组。
    无法解码的视频（decode_error 非空）不参与分组比对。"""
    groups: Dict[int, List[str]] = defaultdict(list)
    for path, meta in metadatas.items():
        if meta.decode_error:
            continue
        groups[meta.file_size].append(path)
    return {size: sorted(paths) for size, paths in groups.items() if len(paths) >= 2}


def group_videos_by_duration(
    metadatas: Dict[str, VideoMetadata], config: Config
) -> Dict[str, List[str]]:
    """
    按时长区间分组，返回 {组名: [路径列表]}，只保留 ≥2 个成员的组。

    区间定义见 config.DURATION_BUCKETS。时长 ≤ SHORT_VIDEO_MAX_SEC 的视频
    全部落入第一个桶（短视频组）—— 一个组、统一用首/中/尾 3 帧兜底比对。
    无法解码的视频（decode_error 非空）不参与分组比对。
    """
    groups: Dict[str, List[str]] = defaultdict(list)
    bucket_names = [name for _, name in config.DURATION_BUCKETS]

    for path, meta in metadatas.items():
        if meta.decode_error:
            continue
        for upper_bound, bucket_name in config.DURATION_BUCKETS:
            if meta.duration_sec <= upper_bound:
                groups[bucket_name].append(path)
                break
        else:
            groups[bucket_names[-1]].append(path)

    result = {}
    for bucket_name in bucket_names:
        if bucket_name in groups and len(groups[bucket_name]) >= 2:
            result[bucket_name] = sorted(groups[bucket_name])
    logger.info(
        "按时长分组完成：%d 个组内存在可比对视频（%s）",
        len(result),
        ", ".join(f"{name}×{len(paths)}" for name, paths in result.items()) or "无",
    )
    return result


def collect_candidate_pairs(
    duration_groups: Dict[str, List[str]],
    size_groups: Dict[int, List[str]],
) -> List[Tuple[str, str]]:
    """
    汇总「候选比对视频对」并去重：
    同时长组内的两两组合 ∪ 同大小组内的两两组合。
    组内单元素视频（既不同时长也不同时大小）无需抽指纹、无需比对。
    """
    pair_set: Set[Tuple[str, str]] = set()

    def _add_pairs(paths: List[str]) -> None:
        ordered = sorted(paths)
        for i in range(len(ordered)):
            for j in range(i + 1, len(ordered)):
                pair_set.add((ordered[i], ordered[j]))

    for paths in duration_groups.values():
        _add_pairs(paths)
    for paths in size_groups.values():
        _add_pairs(paths)

    pairs = sorted(pair_set)
    logger.info("候选比对视频对: %d 对（时长组 ∪ 大小组）", len(pairs))
    return pairs


def paths_in_pairs(pairs: List[Tuple[str, str]]) -> Set[str]:
    """从候选对中提取所有涉及的视频路径（需要抽指纹的视频集合）。"""
    involved: Set[str] = set()
    for path_a, path_b in pairs:
        involved.add(path_a)
        involved.add(path_b)
    return involved
