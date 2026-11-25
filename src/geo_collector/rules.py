from __future__ import annotations

import hashlib
import io
import re
import time
from pathlib import Path

import numpy as np
from PIL import Image

from .storage import file_lock, read_json, write_json


class LocateError(RuntimeError):
    pass


UI_LABELS = {"搜索", "发送", "新会话", "开启新会话", "查看全文", "免费咨询", "立即咨询", "开始咨询", "进入小程序",
             "关闭", "close", "返回", "更多", "输入问题", "按住说话", "切换文字输入", "查看全部药品", "全部药品", "小荷AI医生",
             "搜你想看的", "小荷AI医生小程序", "小程序", "小荷AI医生智能总结"}
_KNOWN_SHORT_IDS = {"tab", "nav", "top", "end", "msg", "btn", "img", "txt", "bar", "tag", "row", "box", "web",
                    "tip", "dot", "num", "all", "new", "add", "del", "sub", "key", "url", "app", "pic", "map",
                    "log", "pin", "opt", "yes", "go", "ok", "no", "up", "id", "tv", "iv", "et", "rv", "bg", "ic"}


def is_obfuscated_id(value: str | None) -> bool:
    """Detect R8/ProGuard short or symbol-mangled resource IDs (e.g. :id/2vk, :id/nl=, :id/sgk)."""
    if not value or ":id/" not in value:
        return False
    package, short = value.split(":id/", 1)
    if package == "android" or not short:
        return False
    if re.search(r"[^A-Za-z0-9_]", short) or short[0].isdigit():
        return True
    return len(short) <= 3 and "_" not in short and short.lower() not in _KNOWN_SHORT_IDS


def normalized(text):
    return re.sub(r"[\W_]+", "", str(text), flags=re.UNICODE).casefold()


def center(bounds):
    return ((bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2)


def validate_recipe(recipe):
    if isinstance(recipe, dict) and "perception" in recipe:
        from .goals import validate_perception
        return validate_perception(recipe)
    if isinstance(recipe, dict) and set(recipe) == {"route"}:
        route = recipe["route"]
        if not isinstance(route, list) or not 1 <= len(route) <= 32:
            raise ValueError("目标路径需要1～32个已观察步骤")
        for hop in route:
            if not isinstance(hop, dict) or set(hop) != {"action", "before", "after"}:
                raise ValueError("路径必须包含动作和前后页面证据")
            validate_recipe({"actions": [hop["action"]]})
            for guard in (hop["before"], hop["after"]):
                if (not isinstance(guard, dict) or set(guard) != {"activity", "anchors"}
                        or not isinstance(guard["activity"], str) or not guard["activity"]
                        or not isinstance(guard["anchors"], list) or len(guard["anchors"]) > 8):
                    raise ValueError("路径页面证据无效")
                for target in guard["anchors"]:
                    validate_recipe({"actions": [{"type": "tap", "target": target}]})
                    if target["kind"] != "node":
                        raise ValueError("路径结构证据必须为节点")
        return recipe
    if isinstance(recipe, dict) and set(recipe) == {"interrupt"}:
        interrupt = recipe["interrupt"]
        if not isinstance(interrupt, dict) or set(interrupt) != {"detect", "actions"}:
            raise ValueError("中断规则需要 detect 目标和 actions 动作")
        detect = interrupt["detect"]
        validate_recipe({"actions": [{"type": "tap", "target": detect}]})
        if detect.get("kind") not in {"node", "ocr"} or "offset" in detect:
            raise ValueError("中断检测只能依据节点或文字，不能用偏移或模板")
        validate_recipe({"actions": interrupt["actions"]})
        if len(interrupt["actions"]) > 3:
            raise ValueError("关闭中断最多三个动作")
        return recipe
    if isinstance(recipe, dict) and set(recipe) == {"layout"}:
        layout = recipe["layout"]
        if not isinstance(layout, dict) or set(layout) - {"role", "target", "photo_region"}:
            raise ValueError("布局修复只接受 role、节点 target 和可选 photo_region")
        if layout.get("role") not in {"answer_container", "product_list", "product_images"}:
            raise ValueError("未知布局角色")
        target = layout.get("target", {})
        if target.get("kind") != "node" or "offset" in target:
            raise ValueError("布局必须使用真实层级节点，不能保存裸裁剪坐标")
        validate_recipe({"actions": [{"type": "tap", "target": target}]})
        region = layout.get("photo_region")
        if region is not None:
            if layout["role"] != "product_images":
                raise ValueError("仅药品卡片允许相对图片区域")
            # Reuse the existing normalized-region validator.
            validate_recipe({"actions": [{"type": "tap", "target": {**target, "region": region}}]})
            if min(region[2] - region[0], region[3] - region[1]) < .15:
                raise ValueError("图片区域过小，不能用于判断整张照片已加载")
        return recipe
    if not isinstance(recipe, dict) or set(recipe) - {"actions", "explore", "transient"}:
        raise ValueError("修复只接受 actions，不接受代码或自定义成功判据。")
    actions = recipe.get("actions")
    if not isinstance(actions, list) or not 1 <= len(actions) <= 6:
        raise ValueError("每个修复需要 1～6 个动作。")
    if "explore" in recipe and (recipe["explore"] is not True or len(actions) != 1):
        raise ValueError("explore每次只执行一个观察过的动作")
    if "transient" in recipe and (recipe["transient"] is not True or len(actions) != 1):
        raise ValueError("transient每次只执行一个观察过的排障动作")
    for action in actions:
        if not isinstance(action, dict):
            raise ValueError("修复动作必须为对象。")
        kind = action.get("type")
        if kind not in {"tap", "back", "swipe"}:
            raise ValueError("修复动作只支持 tap/back/swipe；输入和发送由流程管理。")
        if kind == "tap" or (kind == "swipe" and "target" in action):
            target = action.get("target", {})
            if not isinstance(target, dict):
                raise ValueError("定位方案必须为对象。")
            if target.get("kind") not in {"node", "ocr", "template"}:
                raise ValueError("点击需要节点、文字锚点或图像模板定位，不能保存裸坐标。")
            if target["kind"] == "node" and target.get("attr") not in {"text", "description", "resource_id", "class_name"}:
                raise ValueError("未知节点属性。")
            if target["kind"] == "ocr" and (not isinstance(target.get("pattern"), str) or len(target["pattern"]) > 256):
                raise ValueError("OCR pattern 必须是最多 256 字符的正则表达式。")
            if target["kind"] == "ocr":
                if re.search(r"\([^)]*[+*][^)]*\)[+*]", target["pattern"]):
                    raise ValueError("OCR 正则表达式包含可能导致回溯卡死的嵌套量词")
                try:
                    re.compile(target["pattern"])
                except re.error as error:
                    raise ValueError(f"OCR 正则表达式无效：{error}") from error
            if target["kind"] == "node" and (not isinstance(target.get("value"), str) or not target["value"]):
                raise ValueError("节点属性值不能为空。")
            region = target.get("region", [0, 0, 1, 1])
            if (not isinstance(region, (list, tuple)) or len(region) != 4
                    or not all(isinstance(v, (float, int)) and np.isfinite(v) and 0 <= v <= 1 for v in region)
                    or region[0] >= region[2] or region[1] >= region[3]):
                raise ValueError("无效定位区域。")
            if target["kind"] == "template":
                if not isinstance(target.get("file"), str):
                    raise ValueError("模板文件无效。")
                for key in ("screen_size", "point"):
                    value = target.get(key)
                    if not isinstance(value, (list, tuple)) or len(value) != 2 or not all(isinstance(n, (float, int)) and np.isfinite(n) and n >= 0 for n in value):
                        raise ValueError("模板坐标无效。")
                if min(target["screen_size"]) <= 0:
                    raise ValueError("模板尺寸无效。")
            offset = target.get("offset", [.5, .5])
            if len(offset) != 2 or not all(isinstance(n, (int, float)) and np.isfinite(n) and -2 <= n <= 3 for n in offset):
                raise ValueError("文字/控件锚点偏移不合法。")
        if kind == "swipe" and action.get("direction") not in {"up", "down"}:
            raise ValueError("滑动仅支持 up/down。")
        if kind == "swipe":
            distance = action.get("distance_fraction", .37)
            if not isinstance(distance, (int, float)) or not np.isfinite(distance) or not .05 <= distance <= .8:
                raise ValueError("滑动距离必须为屏幕高度的5%～80%。")
    return recipe


def apply_layout(recipe, observation):
    """Detection-only aliases; original XML, PNG and action selectors are retained."""
    from dataclasses import replace
    from .device import validate_geometry
    validate_recipe(recipe)
    validate_geometry(observation.logical_size, observation.physical_size)
    layout = recipe["layout"]
    target, role = layout["target"], layout["role"]
    w, h = observation.logical_size
    matches = {tuple(node["bounds"]): node for node in observation.nodes
               if node.get("package") == observation.package and node.get(target["attr"]) == target["value"]
               and in_region(node["bounds"], (w, h), target.get("region", [0, 0, 1, 1]))}
    if not matches or (role != "product_images" and len(matches) != 1):
        raise LocateError("布局目标不存在或容器不唯一")
    nodes = [dict(node) for node in observation.nodes]
    for box, node in matches.items():
        l, t, r, b = box
        if not 0 <= l < r <= w or not 0 <= t < b <= h:
            raise LocateError("布局节点越过有效窗口")
        if role != "product_images":
            if r - l < w * .65 or b - t < h * .35:
                raise LocateError("布局容器不足以覆盖正文或药品列表")
            for item in nodes:
                if tuple(item["bounds"]) == box:
                    item["scrollable"] = True
        else:
            region = layout.get("photo_region", [0, 0, 1, 1])
            photo = [l + (r-l) * region[0], t + (b-t) * region[1], l + (r-l) * region[2], t + (b-t) * region[3]]
            bw, bh = photo[2] - photo[0], photo[3] - photo[1]
            if not w * .075 <= bw <= w * .5 or not .6 <= bw / bh <= 1.7:
                raise LocateError("药品图片区域尺寸异常")
            nodes.append({"package": observation.package, "bounds": photo, "class_name": "android.widget.ImageView", "text": "", "layout_hint": True})
    return replace(observation, nodes=nodes)


def in_region(bounds, size, region):
    x, y = center(bounds)
    return region[0] <= x / size[0] <= region[2] and region[1] <= y / size[1] <= region[3]


def _match_template(template_image: Image.Image, target: dict, observation) -> tuple[float, float]:
    import cv2
    region = target.get("region", [0, 0, 1, 1])
    if len(region) != 4 or not all(0 <= float(v) <= 1 for v in region):
        raise LocateError("无效定位区域")
    physical = observation.physical_size
    logical = observation.logical_size
    scale = physical[0] / target["screen_size"][0]
    template = template_image.resize((max(2, round(template_image.width * scale)), max(2, round(template_image.height * scale))))
    gray = cv2.cvtColor(np.asarray(Image.open(io.BytesIO(observation.png)).convert("RGB")), cv2.COLOR_RGB2GRAY)
    patch = cv2.cvtColor(np.asarray(template), cv2.COLOR_RGB2GRAY)
    l, t, r, b = (round(v * physical[i % 2]) for i, v in enumerate(region))
    area = gray[t:b, l:r]
    if np.std(patch) < 10 or area.shape[0] < patch.shape[0] or area.shape[1] < patch.shape[1]:
        raise LocateError("模板无信息或尺寸不兼容")
    scores = cv2.matchTemplate(area, patch, cv2.TM_CCOEFF_NORMED)
    _, score, _, (x, y) = cv2.minMaxLoc(scores)
    masked = scores.copy()
    masked[max(0, y - patch.shape[0] // 2):y + patch.shape[0] // 2 + 1, max(0, x - patch.shape[1] // 2):x + patch.shape[1] // 2 + 1] = -1
    if score < .91 or (masked.size and float(masked.max()) >= score - .045):
        raise LocateError("模板未唯一匹配，需重新定位")
    px = l + x + target["point"][0] * scale
    py = t + y + target["point"][1] * scale
    return (px * logical[0] / physical[0], py * logical[1] / physical[1])


def locate(target, observation, assets: Path):
    kind = target["kind"]
    region = target.get("region", [0, 0, 1, 1])
    if len(region) != 4 or not all(0 <= float(v) <= 1 for v in region):
        raise LocateError("无效定位区域")
    physical = observation.physical_size
    logical = observation.logical_size
    boxes = []
    if kind == "node":
        boxes = [node["bounds"] for node in observation.nodes
                 if node.get(target["attr"]) == target.get("value")
                 and in_region(node["bounds"], logical, region)]
    elif kind == "ocr":
        for line in observation.texts:
            if line.get("confidence", 0) >= .75 and re.search(target["pattern"], line["text"]) and in_region(line["bounds"], physical, region):
                b = line["bounds"]
                boxes.append([b[0] * logical[0] / physical[0], b[1] * logical[1] / physical[1], b[2] * logical[0] / physical[0], b[3] * logical[1] / physical[1]])
    elif kind == "template":
        file = (assets / target.get("file", "")).resolve()
        if not file.is_relative_to(assets.resolve()) or file.suffix != ".png":
            raise LocateError("模板必须在本地规则目录内")
        return _match_template(Image.open(file).convert("RGB"), target, observation)
    unique = list({tuple(b): b for b in boxes if b[2] > b[0] and b[3] > b[1]}.values())
    if len(unique) != 1:
        raise LocateError(f"定位要求唯一目标，实际匹配 {len(unique)} 个")
    box = unique[0]
    ox, oy = target.get("offset", [.5, .5])
    point = (box[0] + (box[2] - box[0]) * ox, box[1] + (box[3] - box[1]) * oy)
    if not (0 <= point[0] < logical[0] and 0 <= point[1] < logical[1]):
        raise LocateError("锚点偏移越过屏幕边界")
    return point


def generalize_region(target: dict, observation, original_bounds=None) -> dict:
    """Widen a synthesized ±0.005 region to the broadest resilient band that stays unique."""
    kind = target.get("kind")
    if kind not in {"node", "ocr"} or "region" not in target or observation is None:
        return target
    get = observation.get if isinstance(observation, dict) else lambda key, default=None: getattr(observation, key, default)
    size = get("logical_size", (0, 0)) if kind == "node" else get("physical_size", (0, 0))
    if len(size) != 2 or not all(size):
        return target
    nodes = get("nodes", ())
    texts = get("texts", ())
    pkg = get("package", "")
    expected = {tuple(original_bounds)} if original_bounds is not None else None

    def unique_in_band(band) -> bool:
        if kind == "node":
            boxes = {tuple(n["bounds"]) for n in nodes
                     if n.get(target["attr"]) == target.get("value")
                     and len(n.get("bounds", ())) == 4 and n["bounds"][2] > n["bounds"][0] and n["bounds"][3] > n["bounds"][1]
                     and in_region(n["bounds"], size, band)}
            scoped = {tuple(n["bounds"]) for n in nodes
                      if n.get(target["attr"]) == target.get("value") and n.get("package", "") in ("", None, pkg)
                      and len(n.get("bounds", ())) == 4 and n["bounds"][2] > n["bounds"][0] and n["bounds"][3] > n["bounds"][1]
                      and in_region(n["bounds"], size, band)}
            return len(boxes) == 1 and boxes == scoped and (expected is None or boxes == expected)
        boxes75 = {tuple(l["bounds"]) for l in texts
                   if l.get("confidence", 0) >= .75 and re.search(target["pattern"], l.get("text", ""))
                   and len(l.get("bounds", ())) == 4 and l["bounds"][2] > l["bounds"][0] and l["bounds"][3] > l["bounds"][1]
                   and in_region(l["bounds"], size, band)}
        boxes78 = {tuple(l["bounds"]) for l in texts
                   if l.get("confidence", 0) >= .78 and re.search(target["pattern"], l.get("text", ""))
                   and len(l.get("bounds", ())) == 4 and l["bounds"][2] > l["bounds"][0] and l["bounds"][3] > l["bounds"][1]
                   and in_region(l["bounds"], size, band)}
        return len(boxes75) == 1 and boxes75 == boxes78 and (expected is None or boxes75 == expected)

    if kind == "node" and target.get("attr") == "resource_id" and not is_obfuscated_id(target.get("value")):
        if unique_in_band([0, 0, 1, 1]):
            return {k: v for k, v in target.items() if k != "region"}

    if original_bounds is not None and len(original_bounds) == 4:
        nx = (original_bounds[0] + original_bounds[2]) / 2 / size[0]
        ny = (original_bounds[1] + original_bounds[3]) / 2 / size[1]
        padded = [round(max(0.0, original_bounds[0] / size[0] - .08), 3),
                  round(max(0.0, original_bounds[1] / size[1] - .10), 3),
                  round(min(1.0, original_bounds[2] / size[0] + .08), 3),
                  round(min(1.0, original_bounds[3] / size[1] + .10), 3)]
    else:
        reg = target["region"]
        nx, ny = (reg[0] + reg[2]) / 2, (reg[1] + reg[3]) / 2
        padded = [round(max(0.0, reg[0] - .08), 3), round(max(0.0, reg[1] - .10), 3),
                  round(min(1.0, reg[2] + .08), 3), round(min(1.0, reg[3] + .10), 3)]

    bands = []
    if ny <= .20:
        bands.extend([[0, 0, 1, .22], [0, 0, .48, .22], [.52, 0, 1, .22]])
    if ny >= .80:
        bands.extend([[0, .78, 1, 1], [0, .78, .48, 1], [.52, .78, 1, 1]])
    if .12 <= ny <= .82:
        bands.extend([[0, .12, 1, .85], [0, .12, .55, .85], [.45, .12, 1, .85]])
    if padded[0] < padded[2] and padded[1] < padded[3]:
        bands.append(padded)

    for band in bands:
        if band[0] <= nx <= band[2] and band[1] <= ny <= band[3] and unique_in_band(band):
            return {**target, "region": list(band)}
    return target


def present(target, observation):
    """Whether a node/OCR target is visible at all; uniqueness is not required."""
    region = target.get("region", [0, 0, 1, 1])
    if target["kind"] == "node":
        return any(node.get(target["attr"]) == target.get("value") and node.get("package") in (None, "", observation.package)
                   and in_region(node["bounds"], observation.logical_size, region) for node in observation.nodes)
    if target["kind"] == "ocr":
        return any(line.get("confidence", 0) >= .78 and re.search(target["pattern"], line["text"])
                   and in_region(line["bounds"], observation.physical_size, region) for line in observation.texts)
    return False


def infer_target(observation, physical_point, assets: Path | None, hints=(), *, save_assets=True):
    px, py = map(float, physical_point)
    pw, ph = observation.physical_size
    lw, lh = observation.logical_size
    if not (0 <= px < pw and 0 <= py < ph):
        raise ValueError("点击位置不在原始截图内")
    x, y = px * lw / pw, py * lh / ph
    nodes = sorted((n for n in observation.nodes if n["bounds"][0] <= x <= n["bounds"][2] and n["bounds"][1] <= y <= n["bounds"][3]), key=lambda n: (n["bounds"][2] - n["bounds"][0]) * (n["bounds"][3] - n["bounds"][1]))
    for node in nodes:
        for attr in ["resource_id", "description"]:
            value = node.get(attr)
            if attr == "resource_id" and is_obfuscated_id(value):
                continue
            if attr == "description" and (value not in UI_LABELS or any(re.search(hint, value or "") for hint in hints)):
                continue
            if value and len(value) < 100 and sum(n.get(attr) == value for n in observation.nodes) == 1:
                b = node["bounds"]
                if (b[2] - b[0]) <= lw * .65 and (b[3] - b[1]) <= lh * .15 and (b[2] - b[0]) * (b[3] - b[1]) < lw * lh * .08:
                    return {"kind": "node", "attr": attr, "value": value, "offset": [(x - b[0]) / max(1, b[2] - b[0]), (y - b[1]) / max(1, b[3] - b[1])]}
    for hint in hints:
        matching = [line for line in observation.texts if re.search(hint, line["text"]) and line.get("confidence", 0) >= .75]
        if len(matching) == 1:
            b = matching[0]["bounds"]
            ox, oy = (px - b[0]) / max(1, b[2] - b[0]), (py - b[1]) / max(1, b[3] - b[1])
            if -1 <= ox <= 2 and -1 <= oy <= 2:
                return {"kind": "ocr", "pattern": hint, "offset": [ox, oy]}
    for node in nodes:
        value = node.get("text", "")
        # Only known UI controls are stable labels; questions and counts are data.
        if value in UI_LABELS:
            if sum(n.get("text") == value for n in observation.nodes) == 1:
                b = node["bounds"]
                if (b[2] - b[0]) * (b[3] - b[1]) < lw * lh * .15:
                    return {"kind": "node", "attr": "text", "value": value, "offset": [(x-b[0])/max(1, b[2]-b[0]), (y-b[1])/max(1, b[3]-b[1])]}
    image = Image.open(io.BytesIO(observation.png)).convert("RGB")
    half_w, half_h = pw * .04, ph * .022
    box = [max(0, int(px - half_w)), max(0, int(py - half_h)), min(pw, int(px + half_w)), min(ph, int(py + half_h))]
    patch = image.crop(box)
    if np.asarray(patch).std() < 12:
        raise ValueError("该位置没有可复用视觉特征，请提供文字或控件定位方案")
    encoded = io.BytesIO(); patch.save(encoded, format="PNG")
    name = hashlib.sha256(encoded.getvalue()).hexdigest() + ".png"
    target = {"kind": "template", "file": name, "screen_size": [pw, ph], "point": [px - box[0], py - box[1]],
              "region": [max(0, px / pw - .3), max(0, py / ph - .18), min(1, px / pw + .3), min(1, py / ph + .18)]}
    try:
        _match_template(patch, target, observation)
    except LocateError as error:
        raise ValueError(f"该位置的视觉特征在当前屏幕不唯一或不可靠：{error}") from error
    if save_assets:
        if assets is None:
            raise ValueError("保存视觉模板需要规则资源目录")
        assets.mkdir(parents=True, exist_ok=True)
        (assets / name).write_bytes(encoded.getvalue())
    return target


FAILURE_LIMIT = 3
VERSION_HISTORY = 8


def rule_eligible(rule, version=None):
    """Active rules, plus rules that were only disabled on a different app version."""
    if rule.get("active"):
        return True
    invalidated = rule.get("invalidated_version")
    return bool(invalidated and version and invalidated != version)


class RuleStore:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.file = self.root / "rules.json"
        self.assets = self.root / "templates"

    def all(self):
        return read_json(self.file) if self.file.exists() else {"version": 1, "rules": {}}

    def candidates(self, package, step, observation=None):
        """Same activity and aspect ratio are required; the app version only ranks.

        A rule verified on another version is still tried, so an app update does
        not discard every learned locator at once. Failures are counted per rule
        and a rule is disabled only after FAILURE_LIMIT consecutive failures.
        """
        entry = self.all()["rules"].get(f"{package}:{step}")
        if not entry:
            return []
        candidates = [entry, *reversed(entry.get("variants", []))]
        version = observation.version if observation else None

        def compatible(rule):
            if not rule_eligible(rule, version):
                return False
            if not observation:
                return True
            size = rule.get("screen_size", observation.logical_size)
            return (rule.get("activity", observation.activity) == observation.activity
                    and abs((size[0] / size[1]) / (observation.logical_size[0] / observation.logical_size[1]) - 1) <= .02)

        def rank(rule):
            known = version is not None and (rule.get("app_version") == version or version in rule.get("verified_versions", []))
            return (0 if known else 1, rule.get("failures", 0), -(rule.get("last_success_at") or rule.get("verified_at") or 0))

        return sorted([rule for rule in candidates if compatible(rule)], key=rank)

    def get(self, package, step, observation=None):
        return next(iter(self.candidates(package, step, observation)), None)

    def interrupts(self, package, observation=None):
        """Learned dialogs/overlays for one app, each as (step, rule)."""
        prefix = f"{package}:interrupt."
        found = []
        for key in self.all()["rules"]:
            if key.startswith(prefix):
                step = key[len(package) + 1:]
                rule = self.get(package, step, observation)
                if rule and "interrupt" in rule.get("recipe", {}):
                    found.append((step, rule))
        return found

    def promote(self, package, step, recipe, observation, source):
        validated = validate_recipe(recipe)
        with file_lock(self.root / ".rules.lock"):
            value = self.all()
            key = f"{package}:{step}"
            previous = value["rules"].get(key)
            revision = (previous or {}).get("revision", 0) + 1
            if previous:
                write_json(self.root / "history" / f"{hashlib.sha256(key.encode()).hexdigest()[:16]}-{previous['revision']}.json", previous)
            variants = [*previous.get("variants", []), {k: v for k, v in previous.items() if k != "variants"}] if previous else []
            value["rules"][key] = {"revision": revision, "active": True, "recipe": validated, "source": source,
                                    "verified_at": time.time(), "app_version": observation.version, "activity": observation.activity,
                                    "screen_size": list(observation.logical_size), "verified_versions": [observation.version],
                                    "successes": 0, "failures": 0, "variants": variants}
            write_json(self.file, value)

    def _rules(self, value, package, step, revision=None):
        entry = value["rules"].get(f"{package}:{step}")
        if not entry:
            return []
        return [rule for rule in [entry, *entry.get("variants", [])] if revision is None or rule["revision"] == revision]

    def record_success(self, package, step, revision, observation):
        with file_lock(self.root / ".rules.lock"):
            value = self.all()
            changed = False
            for rule in self._rules(value, package, step, revision):
                versions = [v for v in rule.get("verified_versions", [rule.get("app_version")]) if v and v != observation.version]
                rule.update(active=True, failures=0, successes=rule.get("successes", 0) + 1, last_success_at=time.time(),
                            verified_versions=[*versions[-(VERSION_HISTORY - 1):], observation.version])
                rule.pop("invalidated_version", None)
                changed = True
            if changed:
                write_json(self.file, value)

    def invalidate(self, package, step, reason, revision=None, version=None, *, immediate=False):
        """Count a failure; disable the rule after FAILURE_LIMIT in a row or when immediate."""
        with file_lock(self.root / ".rules.lock"):
            value = self.all()
            rules = self._rules(value, package, step, revision)
            for rule in rules:
                failures = rule.get("failures", 0) + 1
                rule.update(failures=failures, last_failure_at=time.time(), last_error=reason)
                if immediate or failures >= FAILURE_LIMIT:
                    rule.update(active=False, invalidated_at=time.time(), reason=reason)
                    if version:
                        rule["invalidated_version"] = version
            if rules:
                write_json(self.file, value)

    def health(self):
        """Per-rule status for `geo-collect rules`; recipes are omitted."""
        report = []
        for key, entry in self.all()["rules"].items():
            for rule in [entry, *entry.get("variants", [])]:
                report.append({"step": key, "revision": rule.get("revision"), "active": bool(rule.get("active")),
                               "source": rule.get("source"), "kind": next(iter(rule.get("recipe", {})), None),
                               "app_version": rule.get("app_version"), "verified_versions": rule.get("verified_versions", [rule.get("app_version")]),
                               "activity": rule.get("activity"), "successes": rule.get("successes", 0), "failures": rule.get("failures", 0),
                               "last_error": rule.get("last_error") or rule.get("reason"),
                               "invalidated_version": rule.get("invalidated_version")})
        return report
