"""Evidence for a single assistant welcome card after a new-session divider.

The learned selector names the suggestions heading. Every row and the rest of
this session's visible pixels are independently checked, including offline.
"""
from __future__ import annotations

import hashlib
import re

import numpy as np

from ..device import map_bounds
from ..goals import role_nodes
from ..images import _image
from .common import exact


# Compatibility with saved captures made before reset transitions were recorded.
_LEGACY_INTRO = re.compile(r"(?:你好|您好)[,，!！]?我是小荷(?:健康)?(?:推出的)?AI健康咨询助手[,，。]?"
                   r"(?:可以|可)为(?:你|您)提供(?:全天)?24小时的健康帮助[,，。！!]?"
                   r"(?:快来)?和我对话吧[。！!]?", re.I)
_SUGGESTION = re.compile(r"[·•・∙●]\s*[^·•・∙●]{2,32}")


def _inside(inner, outer):
    return outer[0] <= inner[0] < inner[2] <= outer[2] and outer[1] <= inner[1] < inner[3] <= outer[3]


def _card_pixels(observation, tail, container, lines):
    """Account for the white card, recognized text and a small sender avatar."""
    image = np.asarray(_image(observation.png), dtype=np.int16)
    l, t, r, b = tail
    pixels = image[t:b, l:r]
    if not pixels.size or image.shape[:2] != tuple(reversed(observation.physical_size)):
        return False
    width = r - l
    background = np.median(pixels[:, :max(1, width // 100)], axis=(0, 1))
    if background.min() < 232 or background.max() > 250 or np.ptp(background) > 7:
        return False
    residual = np.max(np.abs(pixels - background), axis=2) > 5
    cl, ct, cr, cb = container
    card = image[ct:cb, cl:cr]
    if not card.size or (card.min(axis=2) >= 251).mean() < .63:
        return False
    neutral = (card.min(axis=2) >= background.min() - 3) & (np.ptp(card, axis=2) <= 6)
    residual[ct-t:cb-t, cl-l:cr-l] &= ~neutral
    for x1, y1, x2, y2 in lines:
        ink = image[y1:y2, x1:x2]
        if not ink.size or (ink.min(axis=2) < 177).mean() < .025 or (np.ptp(ink, axis=2) > 48).mean() > .012:
            return False
        pad = max(1, round(width * .003))
        residual[max(0, y1-t-pad):y2-t+pad, max(0, x1-l-pad):x2-l+pad] = False
    # The sender icon is small and immediately left of the author on the same baseline.
    x, y1, _, y2 = lines[0]
    size = y2 - y1
    residual[max(0, y1-t-round(size*.42)):y2-t+round(size*.42),
             max(0, x-l-round(size*1.93)):max(0, x-l)] = False
    return np.count_nonzero(residual) <= max(4, round(width * .025))


def tail_rows(labels, tail):
    return sorted((v for v in labels if v['bounds'][3] > tail[1] and v['bounds'][1] < tail[3]
                   and not (v.get('node', {}).get('description') == '点击退出小程序'
                            and _inside(tail, v['bounds']))), key=lambda v: (v['bounds'][1], v['bounds'][0]))


def card_signature(rows):
    # OCR line wraps may change when the keyboard opens. Content may not.
    return hashlib.sha256(''.join(v['label'] for v in rows).encode()).hexdigest()


def welcome_fault(observation, boundary, labels):
    """First unsatisfied witness. Used for both live acceptance and Agent feedback."""
    w, h = observation.logical_size
    tail = boundary['tail']
    rows = tail_rows(labels, tail)
    if not 4 <= len(rows) <= 13 or any(not _inside(v['bounds'], tail) for v in rows):
        return '最新分隔以下的整张卡须可见，检查是否有裁切、其他发言或遮挡'
    headings = {tuple(n['bounds']) for n in role_nodes(observation, 'session_welcome')}
    indices = [i for i, row in enumerate(rows) if tuple(row['bounds']) in headings]
    if len(indices) != 1 or not 2 <= indices[0] <= len(rows) - 2:
        return '需要唯一的示例区标题，位置在助手介绍与示例选项之间'
    pivot = indices[0]
    sender, intro, heading, suggestions = rows[0], rows[1:pivot], rows[pivot], rows[pivot+1:]
    if (not re.fullmatch(r'(?:小荷)?AI医生', sender['label'], re.I)
            or not 1 <= len(suggestions) <= 5 or not 2 <= len(heading['label']) <= 20
            or not all(_SUGGESTION.fullmatch(v['label']) for v in suggestions)):
        return '核对卡片的助手署名、示例标题以及其下的全部示例行'
    attested = observation.reset_welcome == card_signature(rows)
    if not attested and not _LEGACY_INTRO.fullmatch(''.join(v['label'] for v in intro)):
        return '未知卡片内容缺少独立来源，需要单次新会话确认前后的原始现场证明此卡随新分隔出现'
    raw = {(exact(v['text']), tuple(map_bounds(v['bounds'], observation.physical_size, observation.logical_size)))
           for v in observation.texts if v.get('confidence', 0) >= .85}
    boxes = [v['bounds'] for v in rows]
    if any((v['label'], tuple(v['bounds'])) not in raw for v in rows):
        return '卡内文字需要原图 OCR 逐行佐证'
    if (boxes[0][2] > w*.5 or any(a[3] > b[1] for a, b in zip(boxes, boxes[1:]))
            or not attested and (not .05*w <= boxes[0][0] < .23*w or boxes[0][2] > .45*w
                or any(not (.015*w < box[0] < .19*w and box[2] <= .94*w) for box in boxes[1:])
                or any(abs(box[0]-boxes[1][0]) > .04*w for box in boxes[1:])
                or any(b[1]-a[3] > .052*h for a, b in zip(boxes, boxes[1:])))):
        return '候选文字未构成连续的单张助手卡片'
    bodies = {tuple(n['bounds']) for n in observation.nodes if n.get('package') in ('', observation.package)
              and n.get('class_name', '').endswith(('ViewGroup', 'FrameLayout', 'LinearLayout', 'View'))
              and _inside(n['bounds'], tail) and boxes[0][3] <= n['bounds'][1] <= boxes[1][1]
              and boxes[1][1]-n['bounds'][1] <= .09*w and 0 <= n['bounds'][3]-boxes[-1][3] <= .10*w
              and all(_inside(box, n['bounds']) for box in boxes[1:])}
    if len(bodies) != 1:
        return '需要唯一层级容器完整包住介绍、标题和全部示例'
    def physical(box):
        return map_bounds(box, observation.logical_size, observation.physical_size)
    if not _card_pixels(observation, boundary['physical_tail'], physical(bodies.pop()), [physical(box) for box in boxes]):
        return '原图仍有无法归属的像素，请核对额外气泡、遮挡和卡外区域'
    return None
