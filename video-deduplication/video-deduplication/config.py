# -*- coding: utf-8 -*-
"""
全局配置模块
============

集中管理视频去重系统的全部可调参数。所有阈值均以 0-100 分制或秒/帧为单位，
尽量做到「看名字就懂含义」。

【运行环境约束 —— 修改代码前请先阅读】
1. 视频文件存放在 NAS 的 16T 机械硬盘上，通过网络挂载访问：
   - 视频解码必须**单线程顺序读**（并发随机寻道会拖垮吞吐）；
   - 目录遍历时对文件路径排序，让机械盘尽量顺序读（见 metadata_processor）。
2. 运行本程序的本机是 SSD：
   - SQLite 缓存库（DB_PATH）放在本地磁盘，保存元数据 + dHash/pHash 指纹；
   - 二次运行时命中缓存的视频不再解码，直接复用历史结果。
"""

from dataclasses import dataclass, field
from typing import Optional, Tuple


@dataclass
class Config:
    # ==================== 路径 ====================
    VIDEO_DIR: str = "videos"                      # NAS 上的视频目录
    DB_PATH: str = "video_hashes.db"              # 本地 SSD 上的 SQLite 缓存库
    REPORT_PATH: str = "dedup_report.csv"         # 人工复核用的 CSV 报告
    LOG_FILE: str = "deduplication.log"           # 运行日志
    TRASH_DIR: str = ""                           # 非空时：删除动作改为「移动到该目录」，更安全

    # ==================== 视频筛选 ====================
    SUPPORTED_EXTENSIONS: Tuple[str, ...] = (".mp4", ".avi", ".mov", ".mkv")

    # ==================== 抽帧策略 ====================
    # 初筛帧：固定时间间隔抽帧，覆盖视频「骨架」
    FIXED_INTERVAL_SEC: float = 30.0     # 固定间隔（秒）
    MAX_FIXED_FRAMES: int = 8            # 每个视频最多取多少个初筛帧

    # 短视频兜底：时长 ≤ 此阈值的视频统一归入「短视频组」，并取首/中/尾 3 帧兜底
    SHORT_VIDEO_MAX_SEC: float = 30.0
    SHORT_VIDEO_FALLBACK_FRAMES: int = 3

    # ==================== ThresholdDetector 场景检测（精筛帧） ====================
    SCENE_THRESHOLD: float = 12.0    # 相邻帧灰度差阈值，越小越敏感（默认 12.0）
    FADE_BIAS: int = -50             # 淡入淡出补偿系数，负值 = 抑制淡变被误判为切换
    MIN_SCENE_LEN: int = 15          # 相邻两个场景切换点的最小间隔（帧）

    # ==================== 相似度判定（0-100 分制） ====================
    PHASH_SCREENING_THRESHOLD: float = 80.0   # 初筛线：pHash 相似度低于此值不再精筛
    SUSPECT_SIMILARITY_THRESHOLD: float = 85.0  # 疑似线：融合相似度落在此线与判重线之间 → 标「疑似重复」交人工判断
    FINAL_SIMILARITY_THRESHOLD: float = 90.0  # 判重线：融合相似度高于此值视为重复
    PHASH_WEIGHT: float = 0.7                 # 融合权重（初筛 pHash）
    DHASH_WEIGHT: float = 0.3                 # 融合权重（精筛 dHash）

    # 精筛时两视频关键帧按时间戳就近匹配的容差（秒）
    KEYFRAME_MATCH_TOLERANCE_SEC: float = 3.0

    # ==================== 文件级快速判重（增量快通道，功能 1/2） ====================
    # 增量场景（库里已有历史跑批结果，同目录又新增视频）下，用便宜的文件级比对
    # 替代昂贵的「解码 + 帧级比对」：
    #   功能 1：(大小 + 采样指纹 + 时长) 一致 → 全文件 MD5 终审 → 判文件级重复；
    #   功能 2：(文件名 + 大小 + 采样指纹) 一致 → 直接复用历史 dHash/pHash，零解码。
    # 异常时置 False 即整体关闭，退回全帧级比对（结果等价，只是慢）。
    FAST_DEDUP_ENABLED: bool = True
    SAMPLE_HASH_BLOCK_SIZE: int = 4096   # 采样指纹块大小：头/中/尾各取这么多字节

    # ==================== 保留评分（0-100 分制） ====================
    # 四个基础维度权重之和 = 1.0；启用字幕维度时四维等比缩放 (1-SUBTITLE_WEIGHT)
    WEIGHT_CONTENT: float = 0.4        # 内容差异度：越不像其他副本越该保留
    WEIGHT_BITRATE: float = 0.3        # 码率：越高越该保留
    WEIGHT_RESOLUTION: float = 0.2     # 分辨率：越高越该保留
    WEIGHT_CREATION_TIME: float = 0.1  # 入库时间：越早（越原始）越该保留
    REFERENCE_BITRATE_KBPS: float = 10000.0  # 码率满分参考（10 Mbps）
    REFERENCE_PIXELS: int = 1920 * 1080      # 分辨率满分参考（1080p）

    # ==================== 字幕得分（保留评分第五维） ====================
    # 仅对时长 > SUBTITLE_CHECK_MIN_SEC（10 分钟）的视频计算字幕得分：
    #   软字幕（容器内字幕轨标记）→ 100 分；否则用硬字幕启发式
    #   （画面底部 20% 条带的边缘密度，烧录字幕/字幕条得分高）。
    # 启用时五维权重 = 原四维 × (1-SUBTITLE_WEIGHT) + 字幕 × SUBTITLE_WEIGHT，
    # 同组内自动优先保留有字幕的版本。
    SUBTITLE_CHECK_MIN_SEC: float = 600.0    # 时长超过该值才做字幕检测（用户约定 10 分钟）
    SUBTITLE_WEIGHT: float = 0.15            # 字幕维度在保留评分中的权重
    SUBTITLE_EDGE_DENSITY_CAP: float = 0.05  # 底部条带 Canny 边缘像素占比满分参考（5%）

    # ==================== 性能开关（按存储介质选择） ====================
    # NAS 机械盘（默认）：必须单线程顺序读（MULTITHREAD_ENABLED=False），并发随机寻道会拖垮吞吐；
    # 本地 SSD：可开启多线程并行处理（--multithread），解码/哈希阶段按线程池并发，SQLite 写入仍在主线程。
    MULTITHREAD_ENABLED: bool = False
    WORKER_THREADS: int = 4                # 多线程工作线程数（MULTITHREAD_ENABLED=True 时生效）
    MD5_CHUNK_SIZE: int = 1024 * 1024      # 全文件 MD5 读块大小（字节）；万兆 SSD 可调到 4MB（可选调优）

    # ==================== 时长分组区间（上限秒数, 组名） ====================
    # 仅在同一组内部两两比对，避免 O(N²) 全量比对；区间可按实际片库分布调整。
    # 注意：≤30 秒的视频全部归入同一组（短视频组），与 SHORT_VIDEO_MAX_SEC 保持一致。
    DURATION_BUCKETS: Tuple[Tuple[float, str], ...] = (
        (30, "30秒内组"),
        (90, "90秒内组"),
        (150, "150秒内组"),
        (210, "210秒内组"),
        (270, "270秒内组"),
        (330, "330秒内组"),
        (600, "10分钟内组"),
        (1200, "20分钟内组"),
        (1800, "30分钟内组"),
        (2400, "40分钟内组"),
        (6600, "1小时50分内组"),
        (10500, "2小时55分内组"),
        (float("inf"), "超长组"),
    )

    # ==================== 日志与数据库 ====================
    LOG_LEVEL: str = "INFO"
    LOG_MAX_BYTES: int = 5 * 1024 * 1024   # 日志轮转：单文件上限 5 MB，超限自动归档
    LOG_BACKUP_COUNT: int = 3              # 日志轮转：保留 3 个历史归档（deduplication.log.1 ~ .3）
    DB_COMMIT_EVERY: int = 20   # 每处理 N 个视频提交一次缓存（批量提交减少 fsync）

    @property
    def effective_workers(self) -> int:
        """实际工作线程数：多线程关闭或线程数不足时归一为 1（纯顺序执行）。"""
        return self.WORKER_THREADS if self.MULTITHREAD_ENABLED else 1


# 便捷的默认配置实例（命令行解析后可用 dataclasses.replace 覆盖字段）
DEFAULT_CONFIG = Config()
