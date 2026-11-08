# -*- coding: utf-8 -*-
"""
帧提取与指纹扫描
================

职责：对单个视频做**单遍顺序解码**，同时产出两套关键帧指纹（混合抽帧策略）：

┌──────────────┬────────────────────────────┬──────────────────────────────┐
│ 阶段         │ 抽帧方式                   │ 用途                          │
├──────────────┼────────────────────────────┼──────────────────────────────┤
│ 初筛指纹     │ 每 FIXED_INTERVAL_SEC 秒   │ pHash 比对（按序号对齐）      │
│ (fixed)      │ 取 1 帧，最多 MAX_FIXED    │                              │
│              │ _FRAMES 帧                 │                              │
├──────────────┼────────────────────────────┼──────────────────────────────┤
│ 精筛指纹     │ ThresholdDetector 找出     │ dHash 比对（按时间戳就近匹配）│
│ (scene)      │ 场景切换点，在切换点取帧   │                              │
└──────────────┴────────────────────────────┴──────────────────────────────┘

短视频兜底（时长 ≤ SHORT_VIDEO_MAX_SEC，全部归入同一个「短视频组」）：
    固定间隔法抽不出有效帧，改取「首 / 中 / 尾」3 帧兜底，两套指纹都来自这 3 帧；
    比对时按序号对齐（首对首、中对中、尾对尾），语义正确。

安全兜底：若场景检测一个切换点都没找到（长镜头 / 静态画面），
用初筛帧的 dHash 顶替精筛指纹，避免视频因缺少精筛指纹而无法判重。

性能要点：
    只顺序读一遍视频 —— 对 NAS 机械盘最友好；抽帧即算哈希、不保留图像，
    内存占用恒定（仅场景检测器保留最近 3 帧灰度）。
"""

import logging
from typing import List, Optional, Tuple

import cv2

from config import Config
from models import FrameFingerprint, VideoFingerprint, VideoMetadata
from subtitle_detector import compute_subtitle_score, hard_subtitle_score
from threshold_detector import ThresholdDetector
from utils import dhash, phash

logger = logging.getLogger("VideoDeduplication")


# =====================================================================
# 一、元数据读取（只读文件头，不解码帧，代价极低）
# =====================================================================

def read_video_metadata(video_path: str, config: Config) -> VideoMetadata:
    """
    快速读取视频元数据（大小 / 时长 / 分辨率 / 帧率 / 码率 / 修改时间）。
    只打开容器读属性，不解码像素，适合批量扫描 NAS 目录。

    容错（C1）：文件无法打开 / 解码头失败时**不再抛异常中断流水线**，而是返回
    decode_error 非空的元数据（占位数值），由决策层标记为「无法解码」交人工检查，
    其余视频照常处理。
    """
    import os

    file_size = os.path.getsize(video_path)
    file_mtime = os.path.getmtime(video_path)

    def _corrupt(error_text: str) -> VideoMetadata:
        meta = VideoMetadata(
            path=video_path,
            file_size=file_size,
            file_mtime=file_mtime,
            duration_sec=0.0,
            width=0,
            height=0,
            fps=0.0,
            bitrate_kbps=None,
        )
        meta.decode_error = error_text
        logger.warning("视频无法解码，标记后跳过比对: %s（%s）", video_path, error_text)
        return meta

    try:
        capture = cv2.VideoCapture(video_path)
        if not capture.isOpened():
            return _corrupt("无法打开视频文件（可能已损坏或编码格式不支持）")
        try:
            fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
            frame_count = float(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0)
            width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
            height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
            bitrate_raw = float(capture.get(cv2.CAP_PROP_BITRATE) or 0.0)
        finally:
            capture.release()
    except Exception as error:  # noqa: BLE001 —— OpenCV 后端异常一律按损坏处理
        return _corrupt(f"读取视频属性失败: {error}")

    # 帧率异常时给一个保守默认值，避免除零
    if fps <= 1e-3:
        logger.warning("视频 %s 帧率异常（fps=%.3f），按 25fps 估算时长", video_path, fps)
        fps = 25.0

    duration_sec = frame_count / fps if frame_count > 0 else 0.0
    bitrate_kbps = bitrate_raw if bitrate_raw > 0 else None

    metadata = VideoMetadata(
        path=video_path,
        file_size=file_size,
        file_mtime=file_mtime,
        duration_sec=duration_sec,
        width=width,
        height=height,
        fps=fps,
        bitrate_kbps=bitrate_kbps,
    )
    # 短视频标记：决定分组归属（统一进「短视频组」）与抽帧方式（首/中/尾兜底）
    metadata.is_short_video = duration_sec <= config.SHORT_VIDEO_MAX_SEC
    return metadata


# =====================================================================
# 二、单遍指纹扫描（混合抽帧）
# =====================================================================

def _make_frame_fingerprint(frame_index: int, timestamp_sec: float, frame) -> FrameFingerprint:
    """对一帧图像同时计算 dHash 与 pHash。"""
    return FrameFingerprint(
        frame_index=frame_index,
        timestamp_sec=timestamp_sec,
        dhash=dhash(frame),
        phash=phash(frame),
    )


def _plan_fixed_targets(metadata: VideoMetadata, config: Config) -> Tuple[set, bool]:
    """
    计算初筛帧的目标帧号集合。
    返回 (目标帧号集合, 是否动态模式)。动态模式 = 总帧数未知，按帧号间隔捕捉。

    短视频：首 / 中 / 尾 3 帧（用户约定的兜底策略）。
    普通视频：t = 0, T, 2T ... （T = FIXED_INTERVAL_SEC），最多 MAX_FIXED_FRAMES 帧。
    """
    fps = metadata.fps if metadata.fps > 1e-3 else 25.0

    if metadata.is_short_video:
        # 总帧数未知（=0）时退化为「只保证抓到首帧，其余靠动态捕捉」
        total_frames = int(round(metadata.duration_sec * fps))
        if total_frames <= 0:
            return {0}, True
        middle = total_frames // 2
        last = max(0, total_frames - 1)
        return {0, middle, last}, False

    total_frames = int(round(metadata.duration_sec * fps))
    if total_frames <= 0:
        # 帧数未知：改为按固定帧间隔动态捕捉
        return set(), True

    num_frames = min(config.MAX_FIXED_FRAMES, int(metadata.duration_sec // config.FIXED_INTERVAL_SEC) + 1)
    targets = set()
    for i in range(num_frames):
        frame_index = int(round(i * config.FIXED_INTERVAL_SEC * fps))
        if frame_index < total_frames:
            targets.add(frame_index)
    return targets, False


def scan_video_fingerprints(
    video_path: str,
    metadata: VideoMetadata,
    config: Config,
    detector: ThresholdDetector,
) -> VideoFingerprint:
    """
    单遍顺序解码视频，产出初筛指纹（fixed_frames）与精筛指纹（scene_frames）。

    - 短视频（≤ SHORT_VIDEO_MAX_SEC）：抓首 / 中 / 尾 3 帧，两套指纹共用；
    - 普通视频：fixed 来自固定间隔帧，scene 来自 ThresholdDetector 的场景切换点；
    - 若场景切换点为 0：scene 回退为 fixed 帧（长镜头兜底）。
    """
    capture = cv2.VideoCapture(video_path)
    if not capture.isOpened():
        raise ValueError(f"无法打开视频文件: {video_path}")

    fps = metadata.fps if metadata.fps > 1e-3 else 25.0
    fixed_targets, dynamic_mode = _plan_fixed_targets(metadata, config)
    max_dynamic_captures = config.SHORT_VIDEO_FALLBACK_FRAMES if metadata.is_short_video else config.MAX_FIXED_FRAMES
    dynamic_step = max(1, int(round(config.FIXED_INTERVAL_SEC * fps)))

    detector.reset()
    fixed_frames: List[FrameFingerprint] = []
    scene_frames: List[FrameFingerprint] = []
    last_seen_frame = None
    last_seen_index = -1
    dynamic_captured = 0

    # 字幕得分（第五维评分）：仅时长 > SUBTITLE_CHECK_MIN_SEC 的视频计算。
    # 硬字幕启发式直接复用已抽取的初筛帧（底部条带边缘密度），零额外解码。
    check_subtitle = metadata.duration_sec > config.SUBTITLE_CHECK_MIN_SEC
    subtitle_hard_scores: List[float] = []

    frame_index = 0
    while True:
        ok, frame = capture.read()
        if not ok:
            break

        capture_this_frame = False
        if dynamic_mode:
            # 动态模式（总帧数未知）：首帧必抓，之后按间隔抓，最多 max_dynamic_captures 帧
            if frame_index == 0 or (dynamic_captured < max_dynamic_captures and frame_index % dynamic_step == 0):
                capture_this_frame = True
        elif frame_index in fixed_targets:
            capture_this_frame = True

        if capture_this_frame:
            fixed_frames.append(_make_frame_fingerprint(frame_index, frame_index / fps, frame))
            dynamic_captured += 1
            if check_subtitle:
                subtitle_hard_scores.append(hard_subtitle_score(frame, config))

        detected = detector.feed(frame_index, frame)
        if detected is not None:
            change_index, change_frame = detected
            scene_frames.append(_make_frame_fingerprint(change_index, change_index / fps, change_frame))

        last_seen_frame = frame
        last_seen_index = frame_index
        frame_index += 1

    capture.release()

    # 冲刷场景检测器末帧判定
    flushed = detector.flush()
    if flushed is not None:
        change_index, change_frame = flushed
        scene_frames.append(_make_frame_fingerprint(change_index, change_index / fps, change_frame))

    # 动态模式：把最后一帧补进初筛帧（首/尾兜底）
    if dynamic_mode and last_seen_frame is not None:
        tail_index = last_seen_index
        if not fixed_frames or fixed_frames[-1].frame_index != tail_index:
            fixed_frames.append(_make_frame_fingerprint(tail_index, tail_index / fps, last_seen_frame))

    # ---------- 兜底策略 ----------
    if metadata.is_short_video:
        # 短视频：两套指纹统一为「首 / 中 / 尾」3 帧
        scene_frames = list(fixed_frames)
    elif not scene_frames:
        # 长镜头 / 静态画面：场景检测无输出，用初筛帧的 dHash 顶替精筛指纹
        logger.debug("视频 %s 未检测到场景切换，精筛指纹回退为初筛帧", video_path)
        scene_frames = list(fixed_frames)

    # 按帧号排序，保证比对时顺序稳定
    fixed_frames.sort(key=lambda f: f.frame_index)
    scene_frames.sort(key=lambda f: f.frame_index)

    # 字幕得分：软字幕轨命中=100，否则硬字幕启发式（初筛帧底部条带边缘密度均值）
    subtitle_score = (
        compute_subtitle_score(video_path, subtitle_hard_scores, config)
        if check_subtitle else None
    )

    return VideoFingerprint(
        video_path=video_path,
        is_short_video=metadata.is_short_video,
        fixed_frames=fixed_frames,
        scene_frames=scene_frames,
        subtitle_score=subtitle_score,
    )
