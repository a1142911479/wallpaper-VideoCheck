# -*- coding: utf-8 -*-
"""
通用工具模块
============

三类职责：
1. 日志初始化（文件 + 控制台双输出）；
2. SQLite 缓存库（放在本地 SSD）：
   - 存储视频元数据与 dHash/pHash 指纹，供重复运行时复用，避免重复解码 NAS 上的视频；
   - 以 (path, size, mtime) 三元组校验缓存新鲜度，文件被替换后自动重算；
   - 附带文件级内容指纹（sample_hash 采样指纹 / full_md5 全文件 MD5），
     支撑增量场景的「文件级快速判重」与「内容一致的指纹复用」（见 fast_dedup.py）。
3. 图像指纹算法、文件指纹算法与相似度换算：
   - dHash（差异哈希）：对整体亮度变化不敏感，用于精筛；
   - pHash（感知哈希，DCT 低频）：抗缩放 / 抗压缩，用于初筛；
   - 采样指纹 / 全文件 MD5：文件字节级内容识别（不解码），用于快通道；
   - 汉明距离 → 0-100 相似度分数。
"""

import hashlib
import logging
import logging.handlers
import os
import sqlite3
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np

from models import FrameFingerprint, VideoFingerprint, VideoMetadata

logger = logging.getLogger("VideoDeduplication")


# =====================================================================
# 零、公共异常
# =====================================================================

class PipelineCancelled(Exception):
    """流水线被用户主动取消（GUI「停止」按钮触发的协作式取消）。"""


def run_compute_jobs(jobs, worker, workers: int = 1, cancel_check=None, label: str = "任务"):
    """
    执行一批「纯计算」任务 —— worker 只做计算/文件读取，**不得触碰 SQLite 连接**
    （连接非线程安全；库写入一律由主线程在拿到结果后完成）。

    workers = 1（默认，NAS 机械盘模式）：顺序逐个执行，保持顺序读；
    workers > 1（SSD 模式）：ThreadPoolExecutor 并发执行 —— OpenCV 解码与文件 IO
    会释放 GIL，线程池即可获得真实并行度，无需多进程（多进程反而要跨进程序列化
    numpy 帧、复制内存）。

    返回与 jobs 同序的结果列表；cancel_check 为协作式取消探针（每个任务完成时检查）。
    """
    job_list = list(jobs)
    if not job_list:
        return []

    if workers <= 1 or len(job_list) == 1:
        results = []
        for job in job_list:
            if cancel_check is not None and cancel_check():
                raise PipelineCancelled(f"{label}被用户取消")
            results.append(worker(job))
        return results

    from concurrent.futures import ThreadPoolExecutor, as_completed

    results = [None] * len(job_list)
    max_workers = min(workers, len(job_list))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_map = {pool.submit(worker, job): index for index, job in enumerate(job_list)}
        try:
            for future in as_completed(future_map):
                if cancel_check is not None and cancel_check():
                    raise PipelineCancelled(f"{label}被用户取消")
                results[future_map[future]] = future.result()
        except BaseException:
            for pending in future_map:
                pending.cancel()
            raise
    return results


# =====================================================================
# 一、日志
# =====================================================================

def setup_logging(
    log_file: str,
    level: str = "INFO",
    max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 3,
) -> logging.Logger:
    """
    初始化日志：同时写入日志文件与控制台。

    文件处理器采用轮转写入（RotatingFileHandler）：单文件达到 max_bytes 后
    自动归档为 deduplication.log.1/.2/...，只保留 backup_count 个历史文件 ——
    防止长期运行的批处理把日志文件写到无限大。
    """
    root_logger = logging.getLogger("VideoDeduplication")
    root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    root_logger.handlers.clear()

    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(stream_handler)
    return root_logger


# =====================================================================
# 二、SQLite 缓存（本地 SSD）
# =====================================================================

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS video_cache (
    video_path         TEXT    PRIMARY KEY,
    file_size          INTEGER NOT NULL,
    file_mtime         REAL    NOT NULL,
    duration_sec       REAL    NOT NULL,
    width              INTEGER NOT NULL,
    height             INTEGER NOT NULL,
    fps                REAL    NOT NULL,
    bitrate_kbps       REAL,
    is_short_video     INTEGER NOT NULL DEFAULT 0,
    sample_hash        TEXT,
    full_md5           TEXT,
    fixed_timestamps   TEXT,
    fixed_dhashes      TEXT,
    fixed_phashes      TEXT,
    scene_timestamps   TEXT,
    scene_dhashes      TEXT,
    scene_phashes      TEXT,
    subtitle_score     REAL,
    fingerprints_ready INTEGER NOT NULL DEFAULT 0,
    updated_at         TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cache_file_size  ON video_cache(file_size);
CREATE INDEX IF NOT EXISTS idx_cache_duration   ON video_cache(duration_sec);
"""

# 旧版数据库（无内容指纹列）的增量迁移：补列即可，不破坏既有数据
_MIGRATION_COLUMNS = (
    ("sample_hash", "TEXT"),
    ("full_md5", "TEXT"),
    ("subtitle_score", "REAL"),
)


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """为旧版缓存库补齐新增列（新版建表语句已含，重复补列会被跳过）。"""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(video_cache)")}
    for column_name, column_type in _MIGRATION_COLUMNS:
        if column_name not in existing:
            conn.execute(f"ALTER TABLE video_cache ADD COLUMN {column_name} {column_type}")
            logger.info("缓存库已迁移：新增列 %s", column_name)


def setup_database(db_path: str) -> sqlite3.Connection:
    """
    打开（必要时创建）本地 SQLite 缓存库并建表；旧库自动补新列。

    WAL 模式（Write-Ahead Logging）：改动先追加到独立日志文件再合并，
    崩溃（断电 / 强杀进程）后重开自动恢复，缓存库不会损坏 ——
    8000 视频的指纹缓存重建要重跑几小时解码，值得用一行 PRAGMA 保护。
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")      # 崩溃安全 + 写入不阻塞读取
    conn.execute("PRAGMA synchronous=NORMAL")    # WAL 配套：安全与性能的平衡点
    conn.executescript(_CREATE_TABLE_SQL)
    _migrate_schema(conn)
    # 内容指纹联合索引依赖新列，放在迁移之后创建
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cache_content_key ON video_cache(file_size, sample_hash)"
    )
    conn.commit()
    logger.info("SQLite 缓存库已就绪: %s（WAL 模式，本地磁盘，用于复用元数据与指纹）", db_path)
    return conn


def load_cached_row(conn: sqlite3.Connection, video_path: str) -> Optional[Dict]:
    """按路径读取一条缓存记录；不存在返回 None。"""
    conn.row_factory = sqlite3.Row
    cursor = conn.execute("SELECT * FROM video_cache WHERE video_path = ?", (video_path,))
    row = cursor.fetchone()
    return dict(row) if row is not None else None


def is_cache_fresh(row: Optional[Dict], file_size: int, file_mtime: float) -> bool:
    """缓存新鲜度校验：文件大小与修改时间都未变化才可复用。"""
    if row is None:
        return False
    return (
        int(row["file_size"]) == int(file_size)
        and abs(float(row["file_mtime"]) - float(file_mtime)) < 1e-6
    )


def row_to_metadata(row: Dict) -> VideoMetadata:
    """缓存行 → VideoMetadata。"""
    return VideoMetadata(
        path=row["video_path"],
        file_size=int(row["file_size"]),
        file_mtime=float(row["file_mtime"]),
        duration_sec=float(row["duration_sec"]),
        width=int(row["width"]),
        height=int(row["height"]),
        fps=float(row["fps"]),
        bitrate_kbps=(None if row["bitrate_kbps"] is None else float(row["bitrate_kbps"])),
        is_short_video=bool(row["is_short_video"]),
    )


def row_to_fingerprint(row: Dict) -> Optional[VideoFingerprint]:
    """缓存行 → VideoFingerprint；未计算过指纹（fingerprints_ready=0）时返回 None。"""
    if not row.get("fingerprints_ready"):
        return None
    fixed_frames = _build_frames(
        row["fixed_timestamps"], row["fixed_dhashes"], row["fixed_phashes"]
    )
    scene_frames = _build_frames(
        row["scene_timestamps"], row["scene_dhashes"], row["scene_phashes"]
    )
    return VideoFingerprint(
        video_path=row["video_path"],
        is_short_video=bool(row["is_short_video"]),
        fixed_frames=fixed_frames,
        scene_frames=scene_frames,
        subtitle_score=(None if row.get("subtitle_score") is None else float(row["subtitle_score"])),
    )


def _build_frames(timestamps: Optional[str], dhashes: Optional[str], phashes: Optional[str]) -> List[FrameFingerprint]:
    """把逗号分隔的三段字符串还原成 FrameFingerprint 列表。"""
    if not timestamps or not dhashes or not phashes:
        return []
    ts_list = [float(x) for x in timestamps.split(",") if x != ""]
    dh_list = dhashes.split(",")
    ph_list = phashes.split(",")
    frames = []
    for i, ts in enumerate(ts_list):
        if i < len(dh_list) and i < len(ph_list):
            frames.append(FrameFingerprint(frame_index=i, timestamp_sec=ts, dhash=dh_list[i], phash=ph_list[i]))
    return frames


def _join(values: List[str]) -> str:
    return ",".join(values)


def save_metadata_row(conn: sqlite3.Connection, meta: VideoMetadata) -> None:
    """
    写入 / 更新元数据缓存（指纹列留空，fingerprints_ready=0）。

    注意：缓存不新鲜（文件被替换）才走这里，因此更新时**同时作废**内容指纹
    （sample_hash / full_md5）—— 内容可能已变，等待内容指纹阶段重新计算。
    """
    conn.execute(
        """
        INSERT INTO video_cache
            (video_path, file_size, file_mtime, duration_sec, width, height, fps,
             bitrate_kbps, is_short_video, fingerprints_ready, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, datetime('now'))
        ON CONFLICT(video_path) DO UPDATE SET
            file_size = excluded.file_size,
            file_mtime = excluded.file_mtime,
            duration_sec = excluded.duration_sec,
            width = excluded.width,
            height = excluded.height,
            fps = excluded.fps,
            bitrate_kbps = excluded.bitrate_kbps,
            is_short_video = excluded.is_short_video,
            sample_hash = NULL,
            full_md5 = NULL,
            fingerprints_ready = 0,
            updated_at = datetime('now')
        """,
        (
            meta.path, meta.file_size, meta.file_mtime, meta.duration_sec,
            meta.width, meta.height, meta.fps, meta.bitrate_kbps,
            1 if meta.is_short_video else 0,
        ),
    )


def save_sample_hash(conn: sqlite3.Connection, video_path: str, sample_hash: str) -> None:
    """把采样指纹写入缓存（内容指纹阶段调用，供快通道 / 指纹复用查询）。"""
    conn.execute(
        "UPDATE video_cache SET sample_hash = ?, updated_at = datetime('now') WHERE video_path = ?",
        (sample_hash, video_path),
    )


def save_full_md5(conn: sqlite3.Connection, video_path: str, full_md5: str) -> None:
    """把全文件 MD5 写入缓存（仅粗筛命中的候选才会计算，避免重复顺序读）。"""
    conn.execute(
        "UPDATE video_cache SET full_md5 = ?, updated_at = datetime('now') WHERE video_path = ?",
        (full_md5, video_path),
    )


def save_fingerprint_row(
    conn: sqlite3.Connection,
    meta: VideoMetadata,
    fingerprint: VideoFingerprint,
    sample_hash: Optional[str] = None,
    full_md5: Optional[str] = None,
) -> None:
    """
    把指纹写入缓存（同时刷新元数据，保证一行记录完整）。

    sample_hash / full_md5：已知时一并落库（新插入的行写入；已存在的行保留原值，
    由内容指纹阶段统一维护）。
    """
    fixed_ts = _join([f"{f.timestamp_sec:.3f}" for f in fingerprint.fixed_frames])
    fixed_dh = _join([f.dhash for f in fingerprint.fixed_frames])
    fixed_ph = _join([f.phash for f in fingerprint.fixed_frames])
    scene_ts = _join([f"{f.timestamp_sec:.3f}" for f in fingerprint.scene_frames])
    scene_dh = _join([f.dhash for f in fingerprint.scene_frames])
    scene_ph = _join([f.phash for f in fingerprint.scene_frames])

    conn.execute(
        """
        INSERT INTO video_cache
            (video_path, file_size, file_mtime, duration_sec, width, height, fps,
             bitrate_kbps, is_short_video, sample_hash, full_md5,
             fixed_timestamps, fixed_dhashes, fixed_phashes,
             scene_timestamps, scene_dhashes, scene_phashes,
             subtitle_score, fingerprints_ready, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, datetime('now'))
        ON CONFLICT(video_path) DO UPDATE SET
            file_size = excluded.file_size,
            file_mtime = excluded.file_mtime,
            duration_sec = excluded.duration_sec,
            width = excluded.width,
            height = excluded.height,
            fps = excluded.fps,
            bitrate_kbps = excluded.bitrate_kbps,
            is_short_video = excluded.is_short_video,
            sample_hash = COALESCE(excluded.sample_hash, video_cache.sample_hash),
            full_md5 = COALESCE(excluded.full_md5, video_cache.full_md5),
            fixed_timestamps = excluded.fixed_timestamps,
            fixed_dhashes = excluded.fixed_dhashes,
            fixed_phashes = excluded.fixed_phashes,
            scene_timestamps = excluded.scene_timestamps,
            scene_dhashes = excluded.scene_dhashes,
            scene_phashes = excluded.scene_phashes,
            subtitle_score = excluded.subtitle_score,
            fingerprints_ready = 1,
            updated_at = datetime('now')
        """,
        (
            meta.path, meta.file_size, meta.file_mtime, meta.duration_sec,
            meta.width, meta.height, meta.fps, meta.bitrate_kbps,
            1 if meta.is_short_video else 0,
            sample_hash, full_md5,
            fixed_ts, fixed_dh, fixed_ph, scene_ts, scene_dh, scene_ph,
            fingerprint.subtitle_score,
        ),
    )


# =====================================================================
# 三、文件指纹（文件级快通道用，不解码、只读字节）
# =====================================================================

def compute_sample_fingerprint(file_path: str, file_size: int, block_size: int = 4096) -> str:
    """
    采样指纹：头 / 中 / 尾各取 block_size 字节 + 文件大小 → MD5 hex。

    只读 3 个小块（约 12KB），代价几乎为零，却足以区分绝大多数不同内容。
    用作粗筛：不同时采样指纹 ⇒ 内容必不相同，直接排除；
    相同采样指纹 ⇒ 只是「疑似相同」，还要全文件 MD5 终审后才能判重。
    为什么不只比前 128 字节：MP4 头部（ftyp/moov）批量导出时高度雷同，
    只比头部有真实碰撞风险（不同内容被误判重复 → 误删）。
    """
    digest = hashlib.md5()
    digest.update(str(file_size).encode("utf-8"))
    with open(file_path, "rb") as file:
        digest.update(file.read(block_size))                       # 头部
        middle_pos = max(0, file_size // 2 - block_size // 2)
        file.seek(middle_pos)
        digest.update(file.read(block_size))                       # 中部
        tail_pos = max(0, file_size - block_size)
        file.seek(tail_pos)
        digest.update(file.read(block_size))                       # 尾部
    return digest.hexdigest()


def compute_file_md5(file_path: str, chunk_size: int = 1024 * 1024) -> str:
    """
    全文件 MD5（终审判据）：分块顺序读整文件，不做视频解码。

    仅对「大小 + 采样指纹 + 时长」粗筛命中的少量候选计算 ——
    顺序读 1 个大文件远快于解码（无 CPU 解码开销，NAS 顺序读友好）。
    全文件 MD5 一致 ⇒ 字节级完全相同 ⇒ 可直接判为文件级重复。
    """
    digest = hashlib.md5()
    with open(file_path, "rb") as file:
        for chunk in iter(lambda: file.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


# =====================================================================
# 四、图像指纹与相似度
# =====================================================================

def _to_gray(image: np.ndarray) -> np.ndarray:
    """统一转灰度（已是单通道则原样返回）。"""
    if image.ndim == 3:
        return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return image


def dhash(image: np.ndarray, hash_size: int = 8) -> str:
    """
    计算差异哈希 dHash（64 bit 的 0/1 字符串）。

    原理：缩放到 (hash_size+1, hash_size) 灰度图，比较每行相邻像素的大小关系。
    对整体亮度 / 对比度变化不敏感，适合做精筛。
    """
    gray = _to_gray(image)
    resized = cv2.resize(gray, (hash_size + 1, hash_size), interpolation=cv2.INTER_AREA)
    diff = resized[:, 1:] > resized[:, :-1]
    return "".join("1" if bit else "0" for bit in diff.flatten())


def phash(image: np.ndarray, hash_size: int = 8, highfreq_factor: int = 4) -> str:
    """
    计算感知哈希 pHash（64 bit 的 0/1 字符串）。

    原理：缩放到 32x32 灰度图 → DCT 变换 → 取左上 hash_size x hash_size 低频系数
    → 与均值比较生成比特串。低频分量代表画面结构，抗缩放 / 抗压缩，适合做初筛。
    """
    gray = _to_gray(image)
    img_size = hash_size * highfreq_factor  # 默认 32
    resized = cv2.resize(gray, (img_size, img_size), interpolation=cv2.INTER_AREA).astype(np.float32)
    dct_result = cv2.dct(resized)
    low_freq = dct_result[:hash_size, :hash_size]
    mean_val = float(np.mean(low_freq))
    return "".join("1" if bit else "0" for bit in (low_freq > mean_val).flatten())


def hamming_distance(hash1: str, hash2: str) -> int:
    """两个等长比特串的汉明距离（不同位的个数）。"""
    return sum(c1 != c2 for c1, c2 in zip(hash1, hash2))


def hash_similarity(hash1: str, hash2: str) -> float:
    """两个比特串的相似度（0-100 分）：100 表示完全一致。"""
    if not hash1 or not hash2 or len(hash1) != len(hash2):
        return 0.0
    distance = hamming_distance(hash1, hash2)
    return (1.0 - distance / len(hash1)) * 100.0
