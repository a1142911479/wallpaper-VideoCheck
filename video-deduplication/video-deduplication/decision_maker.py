# -*- coding: utf-8 -*-
"""
决策模块 —— 聚组、保留评分与删除执行
====================================

职责：
1. cluster_duplicate_groups：用并查集把「重复视频对」聚成重复组
   （A-B 重复、B-C 重复 → {A, B, C} 一组，修复旧代码只按锚点聚组、
   无法合并传递重复的逻辑 bug；帧级比对与文件级快速判重统一合并）；
2. calculate_retention_score：加权保留评分（0-100 分制）
   - 内容差异度 40%：越不像组内其他副本越该留（旧公式保留，已修复「自身相似度
     100 分拉低锚点视频」的 bug —— 改为只统计与「其他成员」的平均相似度）；
   - 码率 30%：越高越该留（归一化到 REFERENCE_BITRATE_KBPS）；
   - 分辨率 20%：越高越该留（归一化到 1080p）；
   - 入库时间 10%：组内越早（越原始）越该留（按组内相对排名给分）；
   - 字幕得分（第五维，仅组内成员均为 >10 分钟视频时启用）：有字幕版本
     自动优先保留 —— 四维基础分 × (1-字幕权重) + 字幕得分 × 字幕权重。
3. make_retention_decisions：每组只保留总分最高的 1 个，其余进删除清单；
   文件级快速判重的副本件（forced_deletes）不参与竞争、直接待删除 ——
   但报告仍列出其保留评分，人工可复核改判（apply_review_overrides）；
4. apply_deletion：执行删除（默认 dry-run 不动文件；--execute 才真正执行，
   可选移动到 TRASH_DIR 而非硬删，留恢复余地）。
"""

import logging
import os
import shutil
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Set, Tuple

from config import Config
from models import PairComparison, RetentionDecision, VideoMetadata

logger = logging.getLogger("VideoDeduplication")


# =====================================================================
# 一、并查集：把重复对聚成重复组
# =====================================================================

class _UnionFind:
    """并查集（路径压缩 + 按秩合并），用于合并传递重复关系。"""

    def __init__(self) -> None:
        self._parent: Dict[str, str] = {}
        self._rank: Dict[str, int] = {}

    def find(self, node: str) -> str:
        if node not in self._parent:
            self._parent[node] = node
            self._rank[node] = 0
        if self._parent[node] != node:
            self._parent[node] = self.find(self._parent[node])
        return self._parent[node]

    def union(self, node_a: str, node_b: str) -> None:
        root_a, root_b = self.find(node_a), self.find(node_b)
        if root_a == root_b:
            return
        if self._rank[root_a] < self._rank[root_b]:
            root_a, root_b = root_b, root_a
        self._parent[root_b] = root_a
        if self._rank[root_a] == self._rank[root_b]:
            self._rank[root_a] += 1


def cluster_duplicate_groups(comparisons: List[PairComparison]) -> List[List[str]]:
    """
    把判定为重复的视频对聚成组（传递合并）。
    返回组列表，每组是排序后的路径列表，仅保留 ≥2 个成员的组。
    """
    union_find = _UnionFind()
    for comparison in comparisons:
        if comparison.is_duplicate:
            union_find.union(comparison.path_a, comparison.path_b)

    members: Dict[str, List[str]] = defaultdict(list)
    for node in list(union_find._parent.keys()):
        members[union_find.find(node)].append(node)

    groups = [sorted(paths) for paths in members.values() if len(paths) >= 2]
    groups.sort(key=lambda paths: paths[0])
    logger.info("重复聚组完成：%d 个重复组", len(groups))
    return groups


# =====================================================================
# 二、保留评分（0-100 分制，权重之和 = 1.0）
# =====================================================================

def _score_bitrate(meta: VideoMetadata, config: Config) -> float:
    """码率得分：线性归一化到 REFERENCE_BITRATE_KBPS，封顶 100。"""
    if not meta.bitrate_kbps or meta.bitrate_kbps <= 0:
        return 0.0
    return min(100.0, meta.bitrate_kbps / config.REFERENCE_BITRATE_KBPS * 100.0)


def _score_resolution(meta: VideoMetadata, config: Config) -> float:
    """分辨率得分：像素数线性归一化到 1080p，封顶 100。"""
    pixels = meta.width * meta.height
    if pixels <= 0:
        return 0.0
    return min(100.0, pixels / config.REFERENCE_PIXELS * 100.0)


def _score_creation_time(meta: VideoMetadata, group_metas: List[VideoMetadata]) -> float:
    """
    入库时间得分：组内相对排名 —— 修改时间最早得 100 分，最晚得 0 分。
    （修改时间是 NAS 上「创建时间」的可靠替代；组内全部相同时各得 100 分。）
    """
    mtimes = [m.file_mtime for m in group_metas]
    oldest, newest = min(mtimes), max(mtimes)
    if newest <= oldest:
        return 100.0
    return (newest - meta.file_mtime) / (newest - oldest) * 100.0


def calculate_retention_score(
    meta: VideoMetadata,
    avg_similarity: float,
    group_metas: List[VideoMetadata],
    config: Config,
) -> Tuple[float, Dict[str, float]]:
    """
    计算单个视频的保留总分与分项得分。

    avg_similarity: 与组内「其他成员」的平均融合相似度（0-100）。
                    越低表示与副本差异越大 → 内容得分越高。
    字幕维度（第五维）：仅当组内所有成员都计算了字幕得分（均为 >10 分钟视频）时启用，
                    同组内自动优先保留有字幕的版本；否则维持四维评分（老行为不变）。
    返回 (总分, {分项名: 得分})，分项得分均已在 0-100 区间。
    """
    content_score = max(0.0, 100.0 - avg_similarity)
    bitrate_score = _score_bitrate(meta, config)
    resolution_score = _score_resolution(meta, config)
    creation_time_score = _score_creation_time(meta, group_metas)

    base_total = (
        content_score * config.WEIGHT_CONTENT
        + bitrate_score * config.WEIGHT_BITRATE
        + resolution_score * config.WEIGHT_RESOLUTION
        + creation_time_score * config.WEIGHT_CREATION_TIME
    )
    breakdown = {
        "内容差异": content_score,
        "码率": bitrate_score,
        "分辨率": resolution_score,
        "入库时间": creation_time_score,
    }

    # 字幕第五维：组内全员都有字幕得分才启用（组内口径一致，避免跨口径混算）
    subtitle_available = all(m.subtitle_score is not None for m in group_metas)
    if subtitle_available:
        subtitle_score = meta.subtitle_score if meta.subtitle_score is not None else 0.0
        weight = config.SUBTITLE_WEIGHT
        total_score = base_total * (1.0 - weight) + subtitle_score * weight
        breakdown["字幕"] = subtitle_score
    else:
        total_score = base_total
    return total_score, breakdown


# =====================================================================
# 三、组内决策
# =====================================================================

def _average_similarity_to_group(
    video_path: str,
    group_paths: List[str],
    comparisons: List[PairComparison],
) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """
    统计某视频与组内「其他成员」的平均 pHash / dHash / 融合相似度。
    只统计实际比过的对（传递组中可能存在没直接比过的成员）。
    """
    phash_values: List[float] = []
    dhash_values: List[float] = []
    final_values: List[float] = []
    peer_set = set(group_paths) - {video_path}

    for comparison in comparisons:
        if comparison.path_a == video_path and comparison.path_b in peer_set:
            peer = comparison.path_b
        elif comparison.path_b == video_path and comparison.path_a in peer_set:
            peer = comparison.path_a
        else:
            continue
        peer_set.discard(peer)
        if comparison.phash_similarity is not None:
            phash_values.append(comparison.phash_similarity)
        if comparison.dhash_similarity is not None:
            dhash_values.append(comparison.dhash_similarity)
        if comparison.final_similarity is not None:
            final_values.append(comparison.final_similarity)

    def _mean(values: List[float]) -> Optional[float]:
        return sum(values) / len(values) if values else None

    return _mean(phash_values), _mean(dhash_values), _mean(final_values)


def _format_breakdown(breakdown: Dict[str, float]) -> str:
    return "＋".join(f"{name} {score:.1f}" for name, score in breakdown.items())


def _decision_source(
    path: str,
    comparisons: List[PairComparison],
    forced_deletes: Dict[str, str],
) -> str:
    """
    判定来源标注（写入报告「判定来源」列，供复核者区分快判 / 精判）：
        文件级快速判重 —— 该视频由全量 MD5 字节级比对定案（未经帧级比对）；
        帧级比对       —— 该视频的结局由 pHash/dHash 两级比对参与决定；
        未比对         —— 孤立视频，没有和任何视频比对过。
    """
    if path in forced_deletes:
        return "文件级快速判重"
    has_frame_pair = False
    has_file_level_pair = False
    for comparison in comparisons:
        if comparison.path_a == path or comparison.path_b == path:
            if comparison.is_file_level:
                has_file_level_pair = True
            else:
                has_frame_pair = True
    if has_frame_pair:
        return "帧级比对"
    if has_file_level_pair:
        return "文件级快速判重"
    return "未比对"


def _decide_group(
    group_id: int,
    group_paths: List[str],
    metadatas: Dict[str, VideoMetadata],
    comparisons: List[PairComparison],
    config: Config,
    forced_deletes: Dict[str, str],
    alias_map: Dict[str, str],
) -> List[RetentionDecision]:
    """
    对一个重复组做保留决策：
    算出每个成员的保留总分 → 取最高分保留，其余待删除。
    并列时依次比较：码率 → 分辨率 → 修改时间更早者优先。

    forced_deletes（文件级快速判重的副本件）不参与保留竞争、直接待删除 ——
    但保留评分照常计算并写入报告（人工复核时两边分数都看得到，可改判）。
    """
    group_metas = [metadatas[path] for path in group_paths]
    score_rows = []

    for path in group_paths:
        meta = metadatas[path]
        phash_sim, dhash_sim, final_sim = _average_similarity_to_group(path, group_paths, comparisons)
        avg_similarity = final_sim if final_sim is not None else (
            phash_sim if phash_sim is not None else 0.0
        )
        total_score, breakdown = calculate_retention_score(meta, avg_similarity, group_metas, config)
        score_rows.append({
            "path": path,
            "meta": meta,
            "phash_sim": phash_sim,
            "dhash_sim": dhash_sim,
            "final_sim": final_sim,
            "total": total_score,
            "breakdown": breakdown,
        })

    # 排序：总分降序；并列时按 码率 → 分辨率 → 修改时间（早者优先）
    score_rows.sort(
        key=lambda row: (
            -row["total"],
            -(row["meta"].bitrate_kbps or 0),
            -(row["meta"].width * row["meta"].height),
            row["meta"].file_mtime,
        )
    )

    # 保留竞争只在「非预判删除」成员中进行（簇代表永不预判，故 contenders 非空）
    contenders = [row for row in score_rows if row["path"] not in forced_deletes] or score_rows
    best = contenders[0]
    contender_rank = {row["path"]: rank for rank, row in enumerate(contenders)}
    twin_counts = Counter(alias_map.get(path) for path in forced_deletes)

    decisions: List[RetentionDecision] = []
    for row in score_rows:
        path = row["path"]
        breakdown_text = _format_breakdown(row["breakdown"])
        sim_text = (
            f"｜与组内均值相似度 {row['final_sim']:.1f}"
            if row["final_sim"] is not None else ""
        )
        twin_note = ""
        if twin_counts.get(path):
            twin_note = f"（另含 {twin_counts[path]} 个文件级重复副本已预判待删除）"

        if path in forced_deletes:
            action = "DELETE"
            reason = (
                f"{forced_deletes[path]}｜该件保留评分 {row['total']:.1f}（仅备查）"
                f"｜分项：{breakdown_text}{sim_text}"
            )
        elif contender_rank[path] == 0:
            action = "KEEP"
            reason = f"组内保留总分最高（{row['total']:.1f}）｜分项：{breakdown_text}{sim_text}{twin_note}"
        else:
            action = "DELETE"
            diff = best["total"] - row["total"]
            reason = (
                f"保留总分 {row['total']:.1f}，低于组内最高 {best['total']:.1f}（差 {diff:.1f}）"
                f"｜分项：{breakdown_text}{sim_text}{twin_note}"
            )
        decisions.append(RetentionDecision(
            video_path=path,
            group_id=group_id,
            action=action,
            phash_similarity=row["phash_sim"],
            dhash_similarity=row["dhash_sim"],
            final_similarity=row["final_sim"],
            content_score=row["breakdown"]["内容差异"],
            bitrate_score=row["breakdown"]["码率"],
            resolution_score=row["breakdown"]["分辨率"],
            creation_time_score=row["breakdown"]["入库时间"],
            subtitle_score=row["breakdown"].get("字幕"),
            total_score=row["total"],
            decision_source=_decision_source(path, comparisons, forced_deletes),
            reason=reason,
        ))
    return decisions


def _corrupt_decision(path: str, meta: VideoMetadata) -> RetentionDecision:
    """无法解码的视频：单列「无法解码」，不参与比对与删除，交人工检查。"""
    return RetentionDecision(
        video_path=path,
        group_id=-1,
        action="CORRUPT",
        phash_similarity=None,
        dhash_similarity=None,
        final_similarity=None,
        decision_source="未比对",
        reason=f"无法解码（{meta.decode_error}），未参与比对，请人工检查文件是否损坏",
    )


def _suspect_partners(
    video_path: str,
    comparisons: List[PairComparison],
) -> List[Tuple[str, float]]:
    """
    收集某视频的「疑似重复」伙伴：[(对方路径, 融合相似度)]，按相似度降序。
    只看 is_suspect 的对（未达判重线、但在疑似线以上）。
    """
    partners: List[Tuple[str, float]] = []
    for comparison in comparisons:
        if not comparison.is_suspect or comparison.final_similarity is None:
            continue
        if comparison.path_a == video_path:
            partners.append((comparison.path_b, comparison.final_similarity))
        elif comparison.path_b == video_path:
            partners.append((comparison.path_a, comparison.final_similarity))
    partners.sort(key=lambda item: -item[1])
    return partners


def make_retention_decisions(
    groups: List[List[str]],
    metadatas: Dict[str, VideoMetadata],
    comparisons: List[PairComparison],
    config: Config,
    forced_deletes: Optional[Dict[str, str]] = None,
    alias_map: Optional[Dict[str, str]] = None,
) -> List[RetentionDecision]:
    """
    对全部重复组生成保留 / 删除决策；未进入任何重复组的视频按情况标记：
        无法解码（decode_error 非空）→ CORRUPT，人工检查；
        与某视频构成疑似重复（疑似线 ≤ 融合 < 判重线）→ SUSPECT，人工判断；
        其余 → UNIQUE。
    forced_deletes：文件级快速判重预判删除的 {路径: 判定依据}（副本件不参与竞争）。
    alias_map：孪生件 → 簇代表（用于统计「保留件含 N 个副本」的报告提示）。
    返回覆盖所有视频的决策列表（CSV 报告的直接数据源）。
    """
    forced_deletes = forced_deletes or {}
    alias_map = alias_map or {}
    decisions: List[RetentionDecision] = []
    grouped_paths: Set[str] = set()

    for group_id, group_paths in enumerate(groups, start=1):
        grouped_paths.update(group_paths)
        # 解码中途失败的成员（元数据正常但抽帧时打不开）单列 CORRUPT，不参与组内竞争
        healthy_paths = []
        for path in group_paths:
            if metadatas[path].decode_error:
                corrupt = _corrupt_decision(path, metadatas[path])
                corrupt.group_id = group_id
                decisions.append(corrupt)
            else:
                healthy_paths.append(path)
        if not healthy_paths:
            continue
        decisions.extend(_decide_group(
            group_id, healthy_paths, metadatas, comparisons, config, forced_deletes, alias_map,
        ))

    for path, meta in metadatas.items():
        if path in grouped_paths:
            continue
        if meta.decode_error:
            decisions.append(_corrupt_decision(path, meta))
            continue
        partners = _suspect_partners(path, comparisons)
        if partners:
            top_text = "、".join(
                f"{os.path.basename(peer)}（{sim:.1f} 分）" for peer, sim in partners[:3]
            )
            more = f" 等 {len(partners)} 个伙伴" if len(partners) > 3 else ""
            max_sim = partners[0][1]
            decisions.append(RetentionDecision(
                video_path=path,
                group_id=-1,
                action="SUSPECT",
                phash_similarity=None,
                dhash_similarity=None,
                final_similarity=max_sim,
                decision_source=_decision_source(path, comparisons, forced_deletes),
                reason=(
                    f"疑似重复：与 {top_text}{more} 融合相似度处于疑似线 "
                    f"{config.SUSPECT_SIMILARITY_THRESHOLD:.1f} 与判重线 "
                    f"{config.FINAL_SIMILARITY_THRESHOLD:.1f} 之间，未自动删除，需人工判断"
                ),
            ))
            continue
        decisions.append(RetentionDecision(
            video_path=path,
            group_id=-1,
            action="UNIQUE",
            phash_similarity=None,
            dhash_similarity=None,
            final_similarity=None,
            decision_source=_decision_source(path, comparisons, forced_deletes),
            reason=f"未与任何视频达到疑似阈值（{config.SUSPECT_SIMILARITY_THRESHOLD:.1f} 分），判定为唯一视频",
        ))

    # 报告顺序：先重复组（按组号），后 疑似 → 无法解码 → 唯一
    action_order = {"SUSPECT": 0, "CORRUPT": 1, "UNIQUE": 2}
    decisions.sort(key=lambda d: (
        d.group_id == -1, d.group_id, action_order.get(d.action, 9), d.video_path,
    ))
    delete_count = sum(1 for d in decisions if d.action == "DELETE")
    logger.info(
        "保留决策完成：%d 个待删除，%d 个保留，%d 个疑似重复，%d 个无法解码，%d 个唯一",
        delete_count,
        sum(1 for d in decisions if d.action == "KEEP"),
        sum(1 for d in decisions if d.action == "SUSPECT"),
        sum(1 for d in decisions if d.action == "CORRUPT"),
        sum(1 for d in decisions if d.action == "UNIQUE"),
    )
    return decisions


# =====================================================================
# 四、人工改判与删除执行
# =====================================================================

def apply_review_overrides(
    decisions: List[RetentionDecision],
    overrides: Dict[str, str],
) -> int:
    """
    应用人工复核改判：overrides = {视频路径: 目标动作(KEEP/DELETE/UNIQUE)}。
    改判后的判定依据追加改判痕迹（保留完整审计链），返回改判条数。
    """
    changed = 0
    for decision in decisions:
        new_action = overrides.get(decision.video_path)
        if new_action and new_action != decision.action:
            decision.reason += f"｜人工复核改判：{decision.action} → {new_action}"
            decision.action = new_action
            changed += 1
    return changed

def apply_deletion(
    decisions: List[RetentionDecision],
    config: Config,
    execute: bool,
) -> int:
    """
    应用删除决策。

    execute=False（默认 dry-run）：只打印将要删除的文件，不动磁盘。
    execute=True：
        - 配置了 TRASH_DIR → 移动到回收目录（保留相对路径结构，可恢复）；
        - 未配置 TRASH_DIR → 直接删除（os.remove）。
    返回实际处理（移动 / 删除）的文件数。
    """
    deletion_list = [d.video_path for d in decisions if d.action == "DELETE"]

    if not execute:
        logger.info("【dry-run】共 %d 个文件将被删除（本次未执行任何文件操作）：", len(deletion_list))
        for path in deletion_list:
            logger.info("  [DRY-RUN 将删除] %s", path)
        logger.info("确认 CSV 报告无误后，使用 --execute 参数真正执行删除。")
        return 0

    handled = 0
    for path in deletion_list:
        try:
            if config.TRASH_DIR:
                relative = os.path.relpath(path, config.VIDEO_DIR)
                target = os.path.join(config.TRASH_DIR, relative)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                shutil.move(path, target)
                logger.info("已移入回收目录: %s -> %s", path, target)
            else:
                os.remove(path)
                logger.info("已删除重复视频: %s", path)
            handled += 1
        except OSError as error:
            logger.error("处理视频 %s 失败: %s", path, error)
    logger.info("删除执行完成：%d / %d 个文件已处理", handled, len(deletion_list))
    return handled
