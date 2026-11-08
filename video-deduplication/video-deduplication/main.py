#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
视频去重系统 —— 主程序入口
==========================

工作流程（10 步）：
    1. 扫描视频目录，收集路径并**按路径排序**（NAS 机械盘顺序读）；
    2. 读取元数据（优先命中本地 SSD 上的 SQLite 缓存，未命中只读文件头）；
    3. 计算内容采样指纹（头/中/尾各 4KB + 大小 → MD5，缓存优先）——快通道粗筛钥匙；
    4. 文件级快速判重（功能 1，可开关）：(大小+采样指纹+时长) 粗筛 → 全文件 MD5 终审
       → 字节级相同的簇直接判重（新增/副本件预判待删除），零解码零比对；
    5. 按文件大小 + 时长分组，只在组内两两比对（避免 O(N²) 全量比对）；
    6. 对候选视频**单线程顺序解码**一遍，混合抽帧（固定间隔帧 + 场景切换帧），
       算出 dHash / pHash 指纹并写入缓存（二次运行免解码；
       功能 2：文件名+大小+采样指纹与历史记录一致 → 直接复用历史指纹，零解码；
       字节级簇的孪生件共用簇代表指纹，不重复解码）；
    7. 两级相似度判定：pHash 初筛 → dHash 精筛 → 加权融合 → 超阈值判重；
    8. 并查集把重复对（帧级 + 文件级）聚成重复组；
    9. 保留评分（四维 + >10 分钟视频的字幕第五维），每组保留最高分 1 个
       （文件级预判删除的副本件不参与竞争，报告仍列出其评分供复核）；
    10. 生成 CSV 复核报告（人工复核依据）→ 默认 dry-run 不删文件；
       确认报告无误后加 --execute 才真正删除（可选移入回收目录；
       可用 --reviewed-report 提交人工改判结果）。

运行示例：
    # 第 1 步：试运行，只出 CSV 报告，不动任何文件
    python main.py --video-dir /mnt/nas/videos

    # 第 2 步：人工打开 dedup_report.csv 复核「待删除」行
    #          （可改「人工复核」列填 保留/待删除 来改判个别行）

    # 第 3 步：确认无误后执行删除（建议先用 --trash-dir 留恢复余地）
    python main.py --video-dir /mnt/nas/videos --execute --trash-dir /mnt/nas/trash
    #          若复核时改判过，把编辑后的报告传回来：
    #          python main.py ... --execute --reviewed-report dedup_report.csv

环境约束（详见 config.py）：
    - 视频在 NAS 机械硬盘上 → 默认单线程顺序解码（无并发寻道）；
    - 本机 SSD → SQLite 缓存元数据 + 指纹，重复运行不重复解码；
    - 视频存本地 SSD 时可加 --multithread 开启线程池并行解码（可选优化）。
"""

import argparse
import dataclasses
import logging
import os
import sys
import time
from typing import Dict, List, Optional, Set, Tuple

from config import Config, DEFAULT_CONFIG
from decision_maker import (
    apply_deletion,
    apply_review_overrides,
    cluster_duplicate_groups,
    make_retention_decisions,
)
from fast_dedup import (
    FastPathResult,
    analyze_content_clusters,
    compute_sample_hashes,
    find_content_reuse,
)
from frame_extractor import read_video_metadata, scan_video_fingerprints
from metadata_processor import (
    collect_candidate_pairs,
    collect_video_paths,
    group_videos_by_duration,
    group_videos_by_size,
    paths_in_pairs,
)
from report_generator import load_review_overrides, write_csv_report, write_xlsx_report
from similarity_calculator import find_duplicate_pairs
from threshold_detector import ThresholdDetector
from models import VideoFingerprint, VideoMetadata
from utils import (
    PipelineCancelled,
    is_cache_fresh,
    load_cached_row,
    row_to_fingerprint,
    row_to_metadata,
    run_compute_jobs,
    save_fingerprint_row,
    save_metadata_row,
    setup_database,
    setup_logging,
)

logger = logging.getLogger("VideoDeduplication")


# =====================================================================
# 阶段封装
# =====================================================================

def load_metadata_with_cache(
    conn, video_paths: List[str], config: Config, cancel_check=None
) -> Dict[str, VideoMetadata]:
    """
    阶段 2：读取全部视频元数据。
    命中缓存（且文件未变化）→ 直接复用；否则快速读文件头并回写缓存。
    遍历顺序 = 路径排序后的顺序（顺序读 NAS）。
    cancel_check：协作式取消探针（GUI「停止」按钮）。

    多线程（MULTITHREAD_ENABLED，适合视频存本地 SSD）：缓存未命中的
    「读文件头」环节交给线程池并发执行；SQLite 读写仍在主线程串行完成
    （连接非线程安全）。
    """
    metadatas: Dict[str, VideoMetadata] = {}
    cache_hits = 0
    pending_commits = 0
    miss_paths: List[str] = []

    for path in video_paths:
        if cancel_check is not None and cancel_check():
            raise PipelineCancelled("元数据阶段被用户取消")
        file_size, file_mtime = os.path.getsize(path), os.path.getmtime(path)
        row = load_cached_row(conn, path)

        if is_cache_fresh(row, file_size, file_mtime):
            metadatas[path] = row_to_metadata(row)
            cache_hits += 1
        else:
            miss_paths.append(path)

    # 缓存未命中 → 读文件头（多线程模式下并发；read_video_metadata 线程安全）
    def _read_one(path: str) -> Tuple[str, VideoMetadata]:
        return path, read_video_metadata(path, config)

    results = run_compute_jobs(
        miss_paths, _read_one,
        workers=config.effective_workers, cancel_check=cancel_check, label="元数据阶段",
    )

    for path, metadata in results:
        metadatas[path] = metadata
        if metadata.decode_error:
            continue   # 无法解码：不写缓存（下次重试），报告单列「无法解码」
        save_metadata_row(conn, metadata)
        pending_commits += 1
        if pending_commits >= config.DB_COMMIT_EVERY:
            conn.commit()
            pending_commits = 0

    if pending_commits:
        conn.commit()
    logger.info("元数据就绪：%d 个视频（缓存命中 %d 个，新读取 %d 个）",
                len(metadatas), cache_hits, len(metadatas) - cache_hits)
    return metadatas


def extract_fingerprints_with_cache(
    conn,
    needed_paths: Set[str],
    metadatas: Dict[str, VideoMetadata],
    config: Config,
    cancel_check=None,
    sample_hashes: Optional[Dict[str, str]] = None,
    alias_map: Optional[Dict[str, str]] = None,
) -> Dict[str, VideoFingerprint]:
    """
    阶段 6：为「候选比对视频」提取指纹。
    三级取数，逐级更贵，能省则省：
        1. 路径缓存命中（且文件未变化）→ 直接复用；
        2. 功能 2 内容一致（文件名+大小+采样指纹命中历史记录）→ 复用历史指纹，零解码；
        3. 未命中 → 单遍解码抽帧算哈希并回写缓存。
    字节级重复簇的孪生件（alias_map）不单独解码 —— 共用簇代表的指纹（零解码）。
    只处理候选视频 —— 孤立视频（无重复可能）不浪费解码时间。
    cancel_check：协作式取消探针（GUI「停止」按钮）。

    并发模型（MULTITHREAD_ENABLED 时，适合视频存本地 SSD）：
        缓存查询 / 指纹落库仍固定在主线程（SQLite 连接非线程安全）；
        只有「解码抽帧」环节交给线程池并发（每个任务自建 ThresholdDetector）。
    """
    alias_map = alias_map or {}
    sample_hashes = sample_hashes or {}
    fingerprints: Dict[str, VideoFingerprint] = {}
    cache_hits = 0
    content_reuse_hits = 0
    pending_commits = 0

    # 实际解码对象 = 簇代表（孪生 → 代表折叠；代表必随孪生进入候选对集合）
    representative_paths = sorted({alias_map.get(p, p) for p in needed_paths})
    total = len(representative_paths)

    # ---- 阶段 A：缓存 / 内容复用短路（串行，需访问 SQLite） ----
    decode_jobs: List[Tuple[int, str]] = []   # (进度序号, 路径) —— 真正需要解码的视频
    for position, path in enumerate(representative_paths, start=1):
        if cancel_check is not None and cancel_check():
            raise PipelineCancelled("指纹提取阶段被用户取消")
        metadata = metadatas[path]
        row = load_cached_row(conn, path)
        cached_fingerprint = row_to_fingerprint(row) if row and is_cache_fresh(
            row, metadata.file_size, metadata.file_mtime
        ) else None

        if cached_fingerprint is not None:
            fingerprints[path] = cached_fingerprint
            cache_hits += 1
            continue

        # 功能 2：内容一致的历史指纹复用（复制 / 移动 / 换路径后重现的同一视频）
        if config.FAST_DEDUP_ENABLED:
            reused = find_content_reuse(
                conn, path, sample_hashes.get(path, ""), metadata.file_size
            )
            if reused is not None:
                fingerprint, source_path = reused
                fingerprint = dataclasses.replace(fingerprint, video_path=path)
                fingerprints[path] = fingerprint
                save_fingerprint_row(
                    conn, metadata, fingerprint, sample_hash=sample_hashes.get(path)
                )
                content_reuse_hits += 1
                pending_commits += 1
                logger.info(
                    "内容一致，复用历史指纹（零解码）[%d/%d]: %s ← %s",
                    position, total, path, source_path,
                )
                if pending_commits >= config.DB_COMMIT_EVERY:
                    conn.commit()
                    pending_commits = 0
                continue

        decode_jobs.append((position, path))

    # ---- 阶段 B：解码抽帧（多线程可并发；每任务自建场景检测器，互不共享） ----
    def _decode_one(job: Tuple[int, str]) -> Tuple[int, str, Optional[VideoFingerprint], str]:
        position, path = job
        logger.info("解码提取指纹 [%d/%d]: %s", position, total, path)
        detector = ThresholdDetector(
            threshold=config.SCENE_THRESHOLD,
            fade_bias=config.FADE_BIAS,
            min_scene_len=config.MIN_SCENE_LEN,
        )
        try:
            fingerprint = scan_video_fingerprints(path, metadatas[path], config, detector)
            return position, path, fingerprint, ""
        except Exception as error:  # noqa: BLE001 —— 解码中途失败按损坏处理，不中断流水线
            return position, path, None, f"解码抽帧失败: {error}"

    decode_results = run_compute_jobs(
        decode_jobs, _decode_one,
        workers=config.effective_workers, cancel_check=cancel_check, label="指纹提取阶段",
    )

    # ---- 阶段 C：指纹落库（串行，SQLite 单线程写入） ----
    for position, path, fingerprint, error_text in decode_results:
        metadata = metadatas[path]
        if fingerprint is None:
            metadata.decode_error = error_text
            logger.warning("解码抽帧失败，标记为无法解码并跳过: %s（%s）", path, error_text)
            continue
        fingerprints[path] = fingerprint
        save_fingerprint_row(
            conn, metadata, fingerprint, sample_hash=sample_hashes.get(path)
        )
        pending_commits += 1
        if pending_commits >= config.DB_COMMIT_EVERY:
            conn.commit()
            pending_commits = 0

    if pending_commits:
        conn.commit()

    # 孪生件别名展开：同内容共用簇代表的指纹（dataclasses.replace 只改归属路径）
    alias_count = 0
    for twin_path, representative in alias_map.items():
        if twin_path in needed_paths and representative in fingerprints:
            fingerprints[twin_path] = dataclasses.replace(
                fingerprints[representative], video_path=twin_path
            )
            alias_count += 1

    # 字幕得分同步到元数据（保留评分第五维从 metadata.subtitle_score 读取）
    for path, fingerprint in fingerprints.items():
        if path in metadatas and fingerprint.subtitle_score is not None:
            metadatas[path].subtitle_score = fingerprint.subtitle_score

    logger.info(
        "指纹就绪：%d 个视频（缓存命中 %d 个，内容复用 %d 个，孪生别名 %d 个，新解码 %d 个）",
        len(fingerprints), cache_hits, content_reuse_hits, alias_count,
        total - cache_hits - content_reuse_hits,
    )
    return fingerprints


# =====================================================================
# 主流程
# =====================================================================

def run_pipeline(config: Config, execute: bool, cancel_check=None, reviewed_report: Optional[str] = None) -> int:
    """执行完整去重流水线，返回进程退出码（0 = 正常）。
    cancel_check：协作式取消探针（GUI「停止」按钮），命令行场景传 None。
    reviewed_report：人工复核后的 CSV 路径（可选）——「人工复核」列的改判决定优先生效。"""
    start_time = time.time()
    logger.info("===== 视频去重系统启动（%s）=====", "执行模式" if execute else "dry-run 模式")
    logger.info("视频目录: %s | 缓存库: %s | 报告: %s", config.VIDEO_DIR, config.DB_PATH, config.REPORT_PATH)

    conn = setup_database(config.DB_PATH)
    try:
        # 1. 收集路径（排序 → NAS 顺序读）
        video_paths = collect_video_paths(config.VIDEO_DIR, config.SUPPORTED_EXTENSIONS)
        if not video_paths:
            logger.warning("目录中未发现视频文件，流程结束。")
            return 0

        # 2. 元数据（缓存优先）
        metadatas = load_metadata_with_cache(conn, video_paths, config, cancel_check)

        # 3. 内容采样指纹 + 文件级快速判重（功能 1/2 的快通道；可整体关闭）
        sample_hashes: Dict[str, str] = {}
        fast_result = FastPathResult()
        if config.FAST_DEDUP_ENABLED:
            sample_hashes = compute_sample_hashes(conn, video_paths, metadatas, config, cancel_check)
            fast_result = analyze_content_clusters(conn, metadatas, sample_hashes, config, cancel_check)

        # 4. 分组 + 候选对；字节级簇内部已由文件级判定，不再进帧级比对
        duration_groups = group_videos_by_duration(metadatas, config)
        size_groups = group_videos_by_size(metadatas)
        candidate_pairs = collect_candidate_pairs(duration_groups, size_groups)
        cluster_pair_set = {(pair.path_a, pair.path_b) for pair in fast_result.fast_pairs}
        frame_pairs = [pair for pair in candidate_pairs if pair not in cluster_pair_set]
        if not frame_pairs and not fast_result.fast_pairs:
            logger.info("所有视频在各自组内均无同类，判定全部唯一，仅生成报告。")

        # 5. 指纹提取（仅候选视频；单线程顺序解码；缓存/内容复用优先；孪生件零解码）
        needed_paths = paths_in_pairs(frame_pairs)
        fingerprints = extract_fingerprints_with_cache(
            conn, needed_paths, metadatas, config, cancel_check,
            sample_hashes=sample_hashes, alias_map=fast_result.alias_map,
        )

        # 6. 两级相似度比对（帧级）＋ 文件级合成比对（快通道产物）
        comparisons = find_duplicate_pairs(frame_pairs, fingerprints, config, cancel_check)
        comparisons.extend(fast_result.fast_pairs)

        # 7. 重复聚组（并查集，帧级 + 文件级统一合并）
        groups = cluster_duplicate_groups(comparisons)

        # 8. 保留决策（四维评分；文件级预判删除的副本件不参与竞争）
        decisions = make_retention_decisions(
            groups, metadatas, comparisons, config,
            forced_deletes=fast_result.forced_deletes,
            alias_map=fast_result.alias_map,
        )

        # 8.5 人工改判（复核后的 CSV 里「人工复核」列有填写的行优先生效）
        if reviewed_report:
            overrides = load_review_overrides(reviewed_report)
            if overrides:
                changed = apply_review_overrides(decisions, overrides)
                logger.info("人工复核改判：%d 个视频的处置决定已按 %s 覆盖", changed, reviewed_report)
            else:
                logger.info("人工复核报告 %s 中无有效改判（「人工复核」列均为空）", reviewed_report)

        # 9. CSV 复核报告 + Excel 分组报告 →（人工复核）→ 删除执行
        report_path = write_csv_report(config.REPORT_PATH, metadatas, decisions)
        write_xlsx_report(config.REPORT_PATH, metadatas, decisions)
        apply_deletion(decisions, config, execute=execute)

        elapsed = time.time() - start_time
        logger.info("处理完成！耗时 %.2f 秒。复核报告: %s", elapsed, report_path)
        logger.info("===== 视频去重系统结束 =====")
        return 0
    finally:
        conn.close()


# =====================================================================
# 命令行入口
# =====================================================================

def parse_args(argv: List[str]) -> Tuple[Config, bool, str]:
    """解析命令行参数，返回 (覆盖后的配置, 是否执行删除, 人工复核报告路径)。"""
    parser = argparse.ArgumentParser(
        description="大规模视频库去重工具（NAS 顺序读 + 本地 SSD 指纹缓存 + dry-run 人工复核）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--video-dir", default=DEFAULT_CONFIG.VIDEO_DIR,
                        help="视频目录（NAS 挂载点）")
    parser.add_argument("--db-path", default=DEFAULT_CONFIG.DB_PATH,
                        help="SQLite 指纹缓存库路径（放本地 SSD）")
    parser.add_argument("--report-path", default=DEFAULT_CONFIG.REPORT_PATH,
                        help="CSV 复核报告输出路径")
    parser.add_argument("--execute", action="store_true",
                        help="真正执行删除（默认 dry-run，只出报告不动文件）")
    parser.add_argument("--trash-dir", default=DEFAULT_CONFIG.TRASH_DIR,
                        help="回收目录：配置后删除动作改为「移动到该目录」，可恢复")
    parser.add_argument("--reviewed-report", default="",
                        help="人工复核后的 CSV 路径：「人工复核」列填了 保留/待删除 的行将覆盖自动判定")
    parser.add_argument("--no-fast-dedup", action="store_true",
                        help="关闭文件级快速判重快通道（退回全帧级比对，结果等价只是更慢）")
    parser.add_argument("--multithread", action="store_true",
                        help="开启多线程并行解码（仅推荐视频存本地 SSD 时使用；NAS 机械盘请保持关闭）")
    parser.add_argument("--workers", type=int, default=DEFAULT_CONFIG.WORKER_THREADS,
                        help="多线程工作线程数（--multithread 时生效，默认 4）")
    parser.add_argument("--md5-chunk-size-mb", type=float,
                        default=DEFAULT_CONFIG.MD5_CHUNK_SIZE / (1024 * 1024),
                        help="全文件 MD5 读块大小（MB，可选调优；万兆 SSD 可调到 4）")
    parser.add_argument("--interval", type=float, default=DEFAULT_CONFIG.FIXED_INTERVAL_SEC,
                        help="初筛固定抽帧间隔（秒）")
    parser.add_argument("--scene-threshold", type=float, default=DEFAULT_CONFIG.SCENE_THRESHOLD,
                        help="场景切换阈值，越小越敏感")
    parser.add_argument("--phash-threshold", type=float, default=DEFAULT_CONFIG.PHASH_SCREENING_THRESHOLD,
                        help="pHash 初筛线（0-100）")
    parser.add_argument("--suspect-threshold", type=float, default=DEFAULT_CONFIG.SUSPECT_SIMILARITY_THRESHOLD,
                        help="疑似线（0-100）：融合相似度落在此线与判重线之间标「疑似重复」交人工判断")
    parser.add_argument("--final-threshold", type=float, default=DEFAULT_CONFIG.FINAL_SIMILARITY_THRESHOLD,
                        help="融合判重线（0-100）")
    parser.add_argument("--log-level", default=DEFAULT_CONFIG.LOG_LEVEL,
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="日志级别")

    args = parser.parse_args(argv)

    config = dataclasses.replace(
        DEFAULT_CONFIG,
        VIDEO_DIR=args.video_dir,
        DB_PATH=args.db_path,
        REPORT_PATH=args.report_path,
        TRASH_DIR=args.trash_dir,
        FAST_DEDUP_ENABLED=not args.no_fast_dedup,
        MULTITHREAD_ENABLED=args.multithread,
        WORKER_THREADS=max(1, args.workers),
        MD5_CHUNK_SIZE=max(64 * 1024, int(args.md5_chunk_size_mb * 1024 * 1024)),
        FIXED_INTERVAL_SEC=args.interval,
        SCENE_THRESHOLD=args.scene_threshold,
        PHASH_SCREENING_THRESHOLD=args.phash_threshold,
        SUSPECT_SIMILARITY_THRESHOLD=args.suspect_threshold,
        FINAL_SIMILARITY_THRESHOLD=args.final_threshold,
        LOG_LEVEL=args.log_level,
    )
    return config, args.execute, args.reviewed_report


def main(argv: List[str] = None) -> int:
    config, execute, reviewed_report = parse_args(argv if argv is not None else sys.argv[1:])
    setup_logging(config.LOG_FILE, config.LOG_LEVEL, config.LOG_MAX_BYTES, config.LOG_BACKUP_COUNT)

    if execute and not config.TRASH_DIR:
        logger.warning(
            "已开启 --execute 且未配置 --trash-dir：重复文件将被**直接删除**（不可恢复）。"
            "建议配置 --trash-dir 以保留恢复余地。"
        )
    elif not execute:
        logger.info("当前为 dry-run 模式：只会生成 CSV 复核报告，不会改动任何视频文件。")

    try:
        return run_pipeline(config, execute=execute, reviewed_report=reviewed_report or None)
    except PipelineCancelled:
        logger.info("流水线已被取消，已处理的缓存结果保留，可下次继续。")
        return 130
    except FileNotFoundError as error:
        logger.error("%s", error)
        return 1
    except Exception:
        logger.exception("程序发生未处理异常")
        return 2


if __name__ == "__main__":
    sys.exit(main())
