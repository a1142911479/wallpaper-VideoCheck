# -*- coding: utf-8 -*-
"""
ThresholdDetector —— 流式场景切换检测器
========================================

职责：在**单遍顺序解码**视频的过程中找出「画面剧烈变化」的帧号（场景切换点），
供 frame_extractor 在这些位置抽取精筛关键帧（dHash）。

为什么做成流式 / 单遍：
    视频存放在 NAS 机械硬盘上，随机寻道与重复解码代价极高；
    本类只在内存中保留最近 3 帧灰度做差分，占用恒定，且整段视频只需顺序读一遍。

算法（与旧版一致）：
    对帧 i 计算相邻帧灰度平均绝对差 diff(i-1, i)，并做淡入淡出补偿：
        adjusted(i) = diff(i-1, i)
                    + mean(diff(i-2, i-1), diff(i, i+1)) * (fade_bias / 100)
    当 adjusted(i) > threshold 且距上一个切换点超过 min_scene_len 帧时，
    判定 i 为场景切换点。fade_bias 为负值（默认 -50），抑制「淡入淡出」被误判。

参数：
    threshold      场景变化阈值，越小越敏感（默认 12.0）
    fade_bias      淡入淡出补偿系数（默认 -50）
    min_scene_len  相邻切换点的最小间隔（帧，默认 15）
"""

from collections import deque
from typing import Deque, Optional, Tuple

import cv2
import numpy as np


class ThresholdDetector:
    """流式场景切换检测器（调用 feed 逐帧送入，延迟一帧出判定结果）。"""

    def __init__(self, threshold: float = 12.0, fade_bias: int = -50, min_scene_len: int = 15):
        self.threshold = threshold
        self.fade_bias = fade_bias
        self.min_scene_len = min_scene_len
        self._reset_state()

    # ------------------------------------------------------------------
    # 内部状态
    # ------------------------------------------------------------------
    def _reset_state(self) -> None:
        """重置滑动窗口状态（每个视频扫描前调用一次）。"""
        self._gray_window: Deque[np.ndarray] = deque(maxlen=3)  # 保存 g(i-2) / g(i-1) / g(i)
        self._current_color_frame: Optional[np.ndarray] = None  # 帧 i 的彩色图（供抽帧）
        self._current_diff: float = 0.0     # diff(i-1, i)
        self._previous_diff: float = 0.0    # diff(i-2, i-1)
        self._last_change_index: int = -10 ** 9
        self._last_frame_index: int = -1

    def reset(self) -> None:
        """对外暴露的状态重置入口。"""
        self._reset_state()

    # ------------------------------------------------------------------
    # 流式判定
    # ------------------------------------------------------------------
    @staticmethod
    def _mean_abs_diff(gray_a: np.ndarray, gray_b: np.ndarray) -> float:
        """两帧灰度图的平均绝对差。"""
        return float(np.mean(cv2.absdiff(gray_a, gray_b)))

    def feed(self, frame_index: int, frame: np.ndarray) -> Optional[Tuple[int, np.ndarray]]:
        """
        送入第 frame_index 帧。

        返回 (切换点帧号, 该帧彩色图像) 或 None。
        判定存在一帧延迟：送入帧 i+1 时才对帧 i 下结论（因为淡变补偿需要后一帧）。
        """
        gray = frame if frame.ndim == 2 else cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        if not self._gray_window:
            # 第一帧：只初始化窗口
            self._gray_window.append(gray)
            self._current_color_frame = frame
            self._last_frame_index = frame_index
            return None

        incoming_diff = self._mean_abs_diff(self._gray_window[-1], gray)

        result: Optional[Tuple[int, np.ndarray]] = None
        candidate_index = self._last_frame_index
        if candidate_index >= 1:
            adjusted_diff = self._current_diff
            # 淡变补偿需要 i-2 与 i+1 两侧上下文；上下文不全时跳过补偿（与旧版一致）
            have_full_context = len(self._gray_window) >= 2 and candidate_index >= 2
            if have_full_context:
                fade_compensation = (self._previous_diff + incoming_diff) / 2.0
                adjusted_diff += fade_compensation * (self.fade_bias / 100.0)

            spacing_ok = (candidate_index - self._last_change_index) > self.min_scene_len
            if adjusted_diff > self.threshold and spacing_ok:
                self._last_change_index = candidate_index
                result = (candidate_index, self._current_color_frame)

        # 滑动窗口推进
        self._previous_diff = self._current_diff
        self._current_diff = incoming_diff
        self._gray_window.append(gray)
        self._current_color_frame = frame
        self._last_frame_index = frame_index
        return result

    def flush(self) -> Optional[Tuple[int, np.ndarray]]:
        """
        视频结束时冲刷最后一个候选帧的判定。
        末帧没有「后一帧」，不做淡变补偿（与旧版 i < len-1 的保护一致）。
        """
        result: Optional[Tuple[int, np.ndarray]] = None
        if self._last_frame_index >= 1 and self._gray_window:
            spacing_ok = (self._last_frame_index - self._last_change_index) > self.min_scene_len
            if self._current_diff > self.threshold and spacing_ok:
                self._last_change_index = self._last_frame_index
                result = (self._last_frame_index, self._current_color_frame)
        self._reset_state()
        return result
