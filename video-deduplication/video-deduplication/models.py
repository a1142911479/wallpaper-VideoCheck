# -*- coding: utf-8 -*-
"""
数据模型
========

定义系统内部流转的核心数据结构，所有模块共享，避免各处裸字典传参。
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass
class VideoMetadata:
    """单个视频文件的元数据（不包含图像指纹）。"""
    path: str                       # 视频文件完整路径
    file_size: int                  # 文件大小（字节）
    file_mtime: float               # 文件修改时间（时间戳，作为「入库时间」的可靠替代）
    duration_sec: float             # 时长（秒）
    width: int                      # 宽（像素）
    height: int                     # 高（像素）
    fps: float                      # 帧率
    bitrate_kbps: Optional[float]   # 码率（kbps，部分后端读不到时为 None）
    is_short_video: bool = False    # 是否短视频（≤ SHORT_VIDEO_MAX_SEC），决定分组与抽帧方式
    decode_error: str = ""          # 非空 = 文件无法解码（损坏/格式不支持），标记后不参与比对，报告单列「无法解码」
    subtitle_score: Optional[float] = None  # 字幕得分（0-100，指纹扫描时回填；None=未计算）

    @property
    def resolution_text(self) -> str:
        return f"{self.width}x{self.height}"

    @property
    def file_size_mb(self) -> float:
        return self.file_size / (1024 * 1024)


@dataclass
class FrameFingerprint:
    """单个关键帧的指纹。"""
    frame_index: int        # 帧号
    timestamp_sec: float    # 时间戳（秒）
    dhash: str              # 差异哈希（64 位 0/1 字符串）
    phash: str              # 感知哈希（64 位 0/1 字符串）


@dataclass
class VideoFingerprint:
    """单个视频的完整指纹：初筛帧 + 精筛帧（混合抽帧策略的产物）。"""
    video_path: str
    is_short_video: bool = False
    # 初筛帧：固定间隔 / 短视频首中尾兜底帧 —— 比对时用 pHash
    fixed_frames: List[FrameFingerprint] = field(default_factory=list)
    # 精筛帧：场景切换点关键帧 —— 比对时用 dHash
    scene_frames: List[FrameFingerprint] = field(default_factory=list)
    # 字幕得分（0-100，仅时长 > 10 分钟的视频计算）：软字幕轨=100，
    # 否则按硬字幕启发式（底部条带边缘密度）打分；None = 未计算/不适用
    subtitle_score: Optional[float] = None


@dataclass
class PairComparison:
    """一对视频的比对结果。"""
    path_a: str
    path_b: str
    phash_similarity: Optional[float]   # 初筛相似度（0-100）；未做初筛时为 None
    dhash_similarity: Optional[float]   # 精筛相似度（0-100）；未做精筛时为 None
    final_similarity: Optional[float]   # 加权融合相似度（0-100）
    is_duplicate: bool                  # 是否判定为重复（≥ 判重线）
    is_suspect: bool = False            # 是否「疑似重复」（≥ 疑似线且 < 判重线，交人工判断）
    stage_note: str = ""                # 判定阶段说明（写入报告「判定依据」列）
    is_file_level: bool = False         # True = 文件级快速判重（全量 MD5 一致，未经帧级比对）


@dataclass
class RetentionDecision:
    """单个视频的保留 / 删除决策，是 CSV 报告的直接数据源。"""
    video_path: str
    group_id: int                       # 所属重复组编号；-1 表示未发现重复
    action: str                         # KEEP（保留）/ DELETE（待删除）/ UNIQUE（唯一）
                                        # / SUSPECT（疑似重复，人工判断）/ CORRUPT（无法解码，人工检查）
    phash_similarity: Optional[float]   # 与组内其他成员的 pHash 平均相似度
    dhash_similarity: Optional[float]   # 与组内其他成员的 dHash 平均相似度
    final_similarity: Optional[float]   # 与组内其他成员的融合平均相似度
    content_score: float = 0.0          # 内容差异度得分
    bitrate_score: float = 0.0          # 码率得分
    resolution_score: float = 0.0       # 分辨率得分
    creation_time_score: float = 0.0    # 入库时间得分
    subtitle_score: Optional[float] = None  # 字幕得分（第五维；仅 >10 分钟视频计算，None=未计算）
    total_score: float = 0.0            # 保留总分（加权和）
    decision_source: str = "帧级比对"   # 判定来源：帧级比对 / 文件级快速判重 / 未比对
    reason: str = ""                    # 判定依据（人类可读）
