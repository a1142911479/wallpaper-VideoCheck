# -*- coding: utf-8 -*-
"""
文件级快速判重（增量快通道）—— 功能 1 / 功能 2
==============================================

增量场景（库里已有历史跑批结果，同目录又新增视频）下，用便宜的
「文件级字节比对」替代昂贵的「解码 + 帧级比对」：

功能 1（文件级快速判重）：
    新视频与已在盘上存在的视频满足「字节大小 + 采样指纹 + 时长」三项一致时，
    用**全文件 MD5 终审**确认字节级完全相同 → 直接判为重复（新增件待删除），
    不解码视频、不做帧级比对。

功能 2（内容指纹缓存复用）：
    文件名 + 字节大小 + 采样指纹 与缓存库中某条已完成指纹的记录一致时，
    认定是「之前处理过的同一视频」（复制 / 移动 / 换了路径），直接复用历史
    dHash/pHash，零解码。（在指纹提取阶段由 find_content_reuse 供数。）

判据为什么是「采样指纹 + 全文件 MD5」而不是「前 128 字节 MD5」：
    MP4 文件头部多为 ftyp/moov，同一编码器批量导出的视频头部高度雷同，
    只比对 128 字节有真实碰撞风险（不同内容 → 误判重复 → 误删不可逆）。
    本模块三级判据，任何一级不过即退回正常帧级比对：
        1. 粗筛：(大小, 时长, 采样指纹) 三项一致 —— 采样指纹为
           头/中/尾各 SAMPLE_HASH_BLOCK_SIZE 字节 + 文件大小的 MD5；
        2. 终审：全文件 MD5 一致 —— 字节级相同，碰撞概率可忽略；
        3. 兜底：任何不确定 → 走原有帧级两级比对（最慢但最稳）。

安全阀（与系统设计红线一致）：
    - 快速判重结果同样进 CSV 复核报告，判定依据明确标注
      「文件级快速判重（全量 MD5 一致，未经帧级比对）」，复核者可区分快判/精判；
    - 默认 dry-run，人工复核后才 --execute；
    - config.FAST_DEDUP_ENABLED = False 可整体关闭快通道，退回全帧级比对；
    - 只有「盘上仍存在的视频」才会被判重复 —— 缓存库里有记录但文件已消失
      （被移走/删除）时，新出现的同内容文件是「唯一副本」，不会被误判删除。
"""

import logging
import os
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from config import Config
from models import PairComparison, VideoFingerprint, VideoMetadata
from utils import (
    PipelineCancelled,
    compute_file_md5,
    compute_sample_fingerprint,
    load_cached_row,
    row_to_fingerprint,
    run_compute_jobs,
    save_full_md5,
    save_sample_hash,
)

logger = logging.getLogger("VideoDeduplication")

# 文件级判定的统一标注（写入 PairComparison.stage_note 与报告判定依据）
FILE_LEVEL_NOTE = "文件级快速判重（全量 MD5 一致，未经帧级比对）"


@dataclass
class FastPathResult:
    """快通道分析结果，供主流水线（提取 / 比对 / 决策）消费。"""
    clusters: List[List[str]] = field(default_factory=list)          # 字节级相同的簇（每簇 ≥2，已排序）
    alias_map: Dict[str, str] = field(default_factory=dict)          # 孪生件 → 簇代表（指纹别名，零解码）
    forced_deletes: Dict[str, str] = field(default_factory=dict)     # 预判待删除路径 → 判定依据
    fast_pairs: List[PairComparison] = field(default_factory=list)   # 文件级合成比对（供聚组 / 报告）


# =====================================================================
# 一、内容采样指纹（功能 1/2 的共同粗筛钥匙）
# =====================================================================

def compute_sample_hashes(
    conn,
    video_paths: List[str],
    metadatas: Dict[str, VideoMetadata],
    config: Config,
    cancel_check=None,
) -> Dict[str, str]:
    """
    为全部视频计算 / 读取「采样指纹」，返回 {路径: sample_hash}。

    缓存优先：命中缓存（元数据阶段已确认文件未变化）直接复用；
    未命中才读 3 个小块算哈希并回写缓存 —— 增量运行时只有新文件付出这点代价。
    遍历顺序 = 路径排序顺序（顺序读 NAS）；多线程模式下未命中文件并发读取。
    """
    sample_hashes: Dict[str, str] = {}
    cache_hits = 0
    miss_paths: List[str] = []

    for path in video_paths:
        if cancel_check is not None and cancel_check():
            raise PipelineCancelled("内容采样指纹阶段被用户取消")
        if metadatas[path].decode_error:
            continue   # 无法解码的视频不进快通道（由报告单列「无法解码」交人工检查）
        row = load_cached_row(conn, path)
        cached_hash = row.get("sample_hash") if row else None
        if cached_hash:
            sample_hashes[path] = cached_hash
            cache_hits += 1
        else:
            miss_paths.append(path)

    def _sample_one(path: str) -> Tuple[str, str]:
        return path, compute_sample_fingerprint(
            path, metadatas[path].file_size, config.SAMPLE_HASH_BLOCK_SIZE
        )

    results = run_compute_jobs(
        miss_paths, _sample_one,
        workers=config.effective_workers, cancel_check=cancel_check, label="内容采样指纹阶段",
    )
    for path, sample_hash in results:
        sample_hashes[path] = sample_hash
        save_sample_hash(conn, path, sample_hash)

    conn.commit()
    logger.info(
        "内容采样指纹就绪：%d 个视频（缓存命中 %d 个，新计算 %d 个）",
        len(sample_hashes), cache_hits, len(sample_hashes) - cache_hits,
    )
    return sample_hashes


# =====================================================================
# 二、功能 1：文件级快速判重（粗筛 → 全量 MD5 终审 → 定责）
# =====================================================================

def _load_or_compute_full_md5(conn, path: str, config: Config) -> str:
    """全文件 MD5：缓存有就复用，没有才分块顺序读整文件计算并回写缓存。
    分块大小由 config.MD5_CHUNK_SIZE 控制（万兆 SSD 可调大到 4MB 提升吞吐）。"""
    row = load_cached_row(conn, path)
    cached_md5 = row.get("full_md5") if row else None
    if cached_md5:
        return cached_md5
    full_md5 = compute_file_md5(path, chunk_size=config.MD5_CHUNK_SIZE)
    save_full_md5(conn, path, full_md5)
    return full_md5


def _pick_survivor(conn, cluster: List[str], metadatas: Dict[str, VideoMetadata]) -> str:
    """
    簇内选出「保留代表」，其余成员预判待删除（符合：新增件删除、老件保留）。

    优先级：历史指纹缓存（上次跑批处理过 = 老视频）> 修改时间更早 > 路径字典序。
    """
    def _sort_key(path: str):
        row = load_cached_row(conn, path)
        processed_before = 1 if (row and row.get("fingerprints_ready")) else 0
        return (-processed_before, metadatas[path].file_mtime, path)

    return sorted(cluster, key=_sort_key)[0]


def analyze_content_clusters(
    conn,
    metadatas: Dict[str, VideoMetadata],
    sample_hashes: Dict[str, str],
    config: Config,
    cancel_check=None,
) -> FastPathResult:
    """
    功能 1 主入口：找出「字节级完全相同」的视频簇并预判删除。

    流程：(大小, 时长, 采样指纹) 粗筛分组 → 组内全文件 MD5 终审 →
    MD5 相同者成簇 → 簇内选保留代表，其余预判待删除（零解码零比对）。
    仅在 FAST_DEDUP_ENABLED 时调用（main 控制开关）。
    """
    # ---- 1. 粗筛：(字节大小, 时长, 采样指纹) 三项一致才可能是同一内容 ----
    buckets: Dict[Tuple[int, float, str], List[str]] = defaultdict(list)
    for path, meta in metadatas.items():
        if meta.decode_error:
            continue   # 无法解码的视频不参与文件级判重
        key = (meta.file_size, round(meta.duration_sec, 3), sample_hashes.get(path, ""))
        buckets[key].append(path)
    candidate_groups = [sorted(paths) for paths in buckets.values() if len(paths) >= 2]

    # ---- 2. 终审：组内全文件 MD5，相同者聚为字节级簇 ----
    clusters: List[List[str]] = []
    md5_computed = 0
    for paths in candidate_groups:
        if cancel_check is not None and cancel_check():
            raise PipelineCancelled("文件级快速判重阶段被用户取消")
        md5_buckets: Dict[str, List[str]] = defaultdict(list)
        for path in paths:
            row = load_cached_row(conn, path)
            had_cached_md5 = bool(row and row.get("full_md5"))
            md5_buckets[_load_or_compute_full_md5(conn, path, config)].append(path)
            if not had_cached_md5:
                md5_computed += 1
        clusters.extend(sorted(members) for members in md5_buckets.values() if len(members) >= 2)
    conn.commit()

    # ---- 3. 簇内定责：代表保留，孪生件预判删除；合成文件级比对 ----
    result = FastPathResult()
    for cluster in clusters:
        survivor = _pick_survivor(conn, cluster, metadatas)
        result.clusters.append(cluster)
        for path in cluster:
            if path == survivor:
                continue
            result.alias_map[path] = survivor
            result.forced_deletes[path] = (
                f"{FILE_LEVEL_NOTE}｜与保留件 {survivor} 字节级相同（MD5 一致）"
            )
        # 簇内完全图合成比对：融合相似度记 100，供并查集聚组与报告均值统计
        for i in range(len(cluster)):
            for j in range(i + 1, len(cluster)):
                result.fast_pairs.append(PairComparison(
                    path_a=cluster[i], path_b=cluster[j],
                    phash_similarity=None, dhash_similarity=None,
                    final_similarity=100.0,
                    is_duplicate=True,
                    stage_note=FILE_LEVEL_NOTE,
                    is_file_level=True,
                ))

    if clusters:
        logger.info(
            "文件级快速判重：%d 组候选经全量 MD5 终审（新算 %d 个），"
            "确认 %d 个字节级重复簇，%d 个新增/副本件预判待删除",
            len(candidate_groups), md5_computed, len(clusters), len(result.forced_deletes),
        )
    else:
        logger.info("文件级快速判重：未发现字节级重复簇（粗筛候选 %d 组）", len(candidate_groups))
    return result


# =====================================================================
# 三、功能 2：内容一致的指纹复用（零解码）
# =====================================================================

def find_content_reuse(
    conn,
    video_path: str,
    sample_hash: str,
    file_size: int,
) -> Optional[Tuple[VideoFingerprint, str]]:
    """
    功能 2：按 (文件名, 字节大小, 采样指纹) 在缓存库找「同一视频」的历史指纹。

    命中含义：该视频上次跑批时已算过 dHash/pHash（复制 / 移动 / 换路径后重现），
    直接复用历史指纹，不再解码。返回 (指纹, 历史记录路径)；未命中返回 None。
    只认「已完成指纹」的记录（fingerprints_ready=1），排除半成品行。
    """
    if not sample_hash:
        return None
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT * FROM video_cache
        WHERE file_size = ? AND sample_hash = ? AND fingerprints_ready = 1
        """,
        (file_size, sample_hash),
    ).fetchall()

    file_name = os.path.basename(video_path)
    # 同名才算「之前跑过的同一视频」（用户约定）；同路径行优先于异路径行
    same_path_rows, same_name_rows = [], []
    for raw_row in rows:
        row = dict(raw_row)
        if row["video_path"] == video_path:
            same_path_rows.append(row)
        elif os.path.basename(row["video_path"]) == file_name:
            same_name_rows.append(row)

    for row in same_path_rows + same_name_rows:
        fingerprint = row_to_fingerprint(row)
        if fingerprint is not None and (fingerprint.fixed_frames or fingerprint.scene_frames):
            return fingerprint, row["video_path"]
    return None
