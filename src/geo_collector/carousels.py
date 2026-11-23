"""Capture real medicine carousels without changing the vertical list position."""
from __future__ import annotations

from pathlib import Path
import re
import xml.etree.ElementTree as ET

from PIL import Image

from .device import map_bounds
from .images import CaptureError, _image, _png, crop, image_size, match_overlap, stable
from .recovery import load_observation


def _bounds(value):
    match = re.fullmatch(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", value or "")
    return tuple(map(int, match.groups())) if match else None


def product_carousels(observation, viewport, *, include_static=False):
    """Only horizontal descendants of the actual medicine RecyclerView qualify.

    Returned bounds are physical visible pixels. A boundary-touching row is
    deliberately considered clipped until a later vertical viewport proves it.
    """
    try:
        root = ET.fromstring(observation.xml)
    except ET.ParseError as exc:
        raise CaptureError("药品层级无效，无法验证横向规格范围") from exc
    lw, lh = observation.logical_size
    pw, ph = observation.physical_size
    rows = []

    def visit(node, inside=False):
        attrs = node.attrib
        box = _bounds(attrs.get("bounds"))
        physical = None
        if box and box[0] < box[2] and box[1] < box[3]:
            visible = (max(0, box[0]), max(0, box[1]), min(lw, box[2]), min(lh, box[3]))
            if visible[0] < visible[2] and visible[1] < visible[3]:
                physical = map_bounds(visible, (lw, lh), (pw, ph))
        own = (attrs.get("class", "").endswith("RecyclerView") and physical is not None
               and tuple(physical) == tuple(viewport) and attrs.get("package", "") in ("", observation.package))
        if (inside and attrs.get("class") == "android.widget.HorizontalScrollView"
                and (include_static or attrs.get("scrollable") == "true") and attrs.get("visible-to-user", "true") != "false"
                and attrs.get("package", "") in ("", observation.package) and physical is not None):
            visible = (max(viewport[0], physical[0]), max(viewport[1], physical[1]),
                       min(viewport[2], physical[2]), min(viewport[3], physical[3]))
            if visible[2] - visible[0] > (viewport[2] - viewport[0]) * .5 and visible[3] > visible[1]:
                rows.append(tuple(visible))
        for child in node:
            visit(child, inside or own)

    visit(root)
    return sorted(set(rows), key=lambda box: (box[1], box[0]))


def horizontal_overlap(previous, current):
    if image_size(previous) != image_size(current):
        raise CaptureError("横向药品规格视口尺寸改变")
    # Transpose maps the horizontal suffix/prefix to the already verified
    # original-pixel vertical matcher, including its ambiguity rejection.
    frames = [_png(_image(frame).transpose(Image.Transpose.TRANSPOSE)) for frame in (previous, current)]
    seam = match_overlap(*frames)
    if seam["shift"] <= 0:
        raise CaptureError("横向药品规格未证明新增内容")
    return {**seam, "axis": "horizontal"}


class ProductCarousels:
    def __init__(self, capture, checkpoint):
        self.capture, self.checkpoint = capture, checkpoint
        self.state = checkpoint.setdefault("horizontal", {"complete": False, "rows": [], "active": None})

    def _save(self):
        self.capture._checkpoint_products(self.checkpoint)

    def _frame(self, observation, row):
        if tuple(row) not in product_carousels(observation, self.capture.viewport):
            raise CaptureError("横向药品规格列表位置改变，保留断点")
        return crop(observation.png, row)

    def _known(self, frame, top, bottom):
        for row in self.state["rows"]:
            original = Path(row["frame_files"][0]).read_bytes()
            ow, oh = image_size(original)
            width, height = image_size(frame)
            if ow != width or oh < height or (oh != height and not top and not bottom):
                continue
            candidate = crop(original, (0, oh - height if top else 0, ow, oh if top else height))
            if stable(candidate, frame):
                return True
        return False

    def _record(self, row, current, frame, seam=None):
        number = len(row["frame_files"]) + 1
        path = self.capture.attempt / f"product-horizontal-{row['number']:03d}-frame-{number:03d}.png"
        path.write_bytes(frame)
        observation = self.capture._save("product-horizontal-frame", current)
        row["frame_files"].append(str(path))
        row["frame_observations"].append(observation)
        if seam is not None:
            row["seams"].append(seam)
        row["end_observations"] = []
        self._save()

    def _ready(self, row):
        current, boxes = self.capture._drawer_ready(self.capture._settled())
        self._check_row_images(row, current, boxes)
        return current

    def _check_row_images(self, row, current, boxes):
        bounds = row["bounds_physical"]
        self._frame(current, bounds)
        if not any(box[0] < bounds[2] and box[2] > bounds[0] and box[1] < bounds[3] and box[3] > bounds[1] for box in boxes):
            raise CaptureError("横向药品规格没有可验证的已加载图片")

    def _swipe(self, row, restore=False):
        left, top, right, bottom = row["bounds_physical"]
        width = right - left
        y = top + (bottom - top) * .35
        x1, x2 = ((left + width * .2, left + width * .8) if restore
                  else (left + width * .8, left + width * .4))
        logical, physical = self.capture.geometry
        self._save()  # Persist the active row/phase before dispatching a gesture.
        self.capture.device.swipe((x1 * logical[0] / physical[0], y * logical[1] / physical[1]),
                                  (x2 * logical[0] / physical[0], y * logical[1] / physical[1]),
                                  duration=.8, expected_package=self.capture.package)

    def _finish(self, current):
        row = self.state["active"]
        bounds = row["bounds_physical"]
        if row["phase"] == "forward":
            # Resume may observe a dispatched swipe before its frame was saved.
            # Verify that displacement first; never skip straight to another swipe.
            frame = self._frame(current, bounds)
            previous = Path(row["frame_files"][-1]).read_bytes()
            if not stable(previous, frame):
                self._record(row, current, frame, horizontal_overlap(previous, frame))
            while len(row["end_observations"]) < 2:
                self._swipe(row)
                current = self._ready(row)
                frame = self._frame(current, bounds)
                previous = Path(row["frame_files"][-1]).read_bytes()
                if stable(previous, frame):
                    row["end_observations"].append(self.capture._save("product-horizontal-end", current))
                    self._save()
                else:
                    self._record(row, current, frame, horizontal_overlap(previous, frame))
                    self.capture.engine.log("product_horizontal_frame_captured", row=row["number"], frames=len(row["frame_files"]))
            row["phase"] = "restore"
            row["restore_attempts"] = 0
            self._save()
        origin = load_observation(row["frame_observations"][0])
        original_list = crop(origin.png, self.capture.viewport)
        while not stable(original_list, crop(current.png, self.capture.viewport)):
            if row["restore_attempts"] >= len(row["frame_files"]) + 3:
                raise CaptureError("横向药品列表未恢复起点，停止纵向滚动以保留接缝")
            row["restore_attempts"] += 1
            self._swipe(row, restore=True)
            current = self._ready(row)
        row["restore_observation"] = self.capture._save("product-horizontal-restored", current)
        row["complete"] = True
        row["phase"] = "complete"
        self.state["rows"].append(row)
        self.state["active"] = None
        self._save()
        self.capture.engine.log("product_horizontal_complete", row=row["number"], frames=len(row["frame_files"]),
                                end_probes=len(row["end_observations"]), restored=True)
        return current

    def capture_visible(self, current, *, final=False):
        if self.state.get("active"):
            current = self._finish(current)
        for bounds in product_carousels(current, self.capture.viewport):
            frame = self._frame(current, bounds)
            top, bottom = bounds[1] <= self.capture.viewport[1], bounds[3] >= self.capture.viewport[3]
            if self._known(frame, top, bottom):
                continue
            if bottom and not top and not final:
                continue
            if top or bottom:
                raise CaptureError("横向药品规格未完整进入视口，不能确认全部规格")
            row = {"number": len(self.state["rows"]) + 1, "bounds_physical": list(bounds),
                   "viewport_physical": list(self.capture.viewport), "phase": "forward", "complete": False,
                   "frame_files": [], "frame_observations": [], "seams": [], "end_observations": []}
            current, boxes = self.capture._drawer_ready(current)
            self._check_row_images(row, current, boxes)
            frame = self._frame(current, bounds)
            self.state["active"] = row
            self.state["complete"] = False
            self._record(row, current, frame)
            self.capture.engine.log("product_horizontal_started", row=row["number"])
            current = self._finish(current)
        if final:
            self.state["complete"] = True
            self._save()
        return current

    def image_files(self):
        return [file for row in self.state["rows"] if len(row["frame_files"]) > 1 for file in row["frame_files"]]


def verify_carousels(metadata, debug, delivery):
    """Validate saved horizontal seams, true ends, restoration and output pixels."""
    if metadata.get("complete") is not True or metadata.get("active") is not None or not isinstance(metadata.get("rows"), list):
        raise CaptureError("横向药品规格采集未完成")
    debug, delivery = Path(debug).resolve(), Path(delivery).resolve()
    expected_images = []

    def observation(path):
        if not isinstance(path, str) or not Path(path).resolve().is_relative_to(debug):
            raise CaptureError("横向药品原始观察不属于本题")
        return load_observation(path)

    for row in metadata["rows"]:
        files, observations, seams = row.get("frame_files"), row.get("frame_observations"), row.get("seams")
        if (row.get("complete") is not True or not isinstance(files, list) or not files
                or not isinstance(observations, list) or len(files) != len(observations)
                or not isinstance(seams, list) or len(seams) != len(files) - 1):
            raise CaptureError("横向药品帧或接缝证据缺失")
        frames, originals = [], []
        bounds, viewport = row["bounds_physical"], row["viewport_physical"]
        for file, saved in zip(files, observations):
            if not isinstance(file, str) or not Path(file).resolve().is_relative_to(debug):
                raise CaptureError("横向药品原始帧不属于本题")
            frame, original = Path(file).read_bytes(), observation(saved)
            if tuple(bounds) not in product_carousels(original, viewport) or _image(crop(original.png, bounds)).tobytes() != _image(frame).tobytes():
                raise CaptureError("横向药品帧与实际药品列表原始像素不符")
            if originals and (original.package, original.logical_size, original.physical_size) != (originals[0].package, originals[0].logical_size, originals[0].physical_size):
                raise CaptureError("横向药品原始观察应用或尺寸变化")
            frames.append(frame)
            originals.append(original)
        for index, seam in enumerate(seams):
            measured = horizontal_overlap(frames[index], frames[index + 1])
            if seam.get("verified") is not True or seam.get("axis") != "horizontal" or measured["overlap"] != seam.get("overlap"):
                raise CaptureError("横向药品接缝与原始像素不符")
        ends = row.get("end_observations", [])
        if len(ends) != 2 or len(set(ends)) != 2:
            raise CaptureError("横向药品缺少连续两次真实末项证据")
        for saved in ends:
            end = observation(saved)
            if tuple(bounds) not in product_carousels(end, viewport) or not stable(frames[-1], crop(end.png, bounds)):
                raise CaptureError("横向药品末项证据不匹配")
        restored = observation(row.get("restore_observation"))
        if tuple(bounds) not in product_carousels(restored, viewport) or not stable(crop(originals[0].png, viewport), crop(restored.png, viewport)):
            raise CaptureError("横向药品未恢复原位")
        if len(frames) > 1:
            expected_images.extend(frames)
    paths = metadata.get("image_paths", [])
    if not isinstance(paths, list) or len(paths) != len(expected_images) or len(set(paths)) != len(paths):
        raise CaptureError("横向药品交付图片数量不符")
    for path, frame in zip(paths, expected_images):
        if not isinstance(path, str) or not Path(path).resolve().is_relative_to(delivery) or Path(path).suffix != ".png":
            raise CaptureError("横向药品交付图片路径无效")
        if _image(Path(path).read_bytes()).size != _image(frame).size or _image(Path(path).read_bytes()).tobytes() != _image(frame).tobytes():
            raise CaptureError("横向药品交付图片像素被修改")
