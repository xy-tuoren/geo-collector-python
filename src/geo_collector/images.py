"""Lossless viewport crops and conservative, content-verified scroll seams."""

from __future__ import annotations

from io import BytesIO
from math import ceil, floor, isfinite
from functools import lru_cache

import numpy as np
from PIL import Image, UnidentifiedImageError


class CaptureError(ValueError):
    """The supplied frames cannot establish a safe capture."""


@lru_cache(maxsize=4)
def _image(png: bytes) -> Image.Image:
    # Read-only shared images; four frames bound memory while reusing PNG decoding.
    try:
        with Image.open(BytesIO(png)) as source:
            if source.format != "PNG":
                raise CaptureError("Capture frames must be PNG images")
            source.load()
            return source.convert("RGB")
    except (UnidentifiedImageError, OSError, TypeError) as error:
        raise CaptureError("Invalid PNG capture") from error


def _png(image: Image.Image) -> bytes:
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def image_size(png: bytes) -> tuple[int, int]:
    return _image(png).size


def crop(png: bytes, bounds) -> bytes:
    image = _image(png)
    if len(bounds) != 4 or any(not isinstance(value, (int, float)) or not isfinite(value) for value in bounds):
        raise CaptureError("Crop bounds must contain four finite physical coordinates")
    left, top, right, bottom = bounds
    if right <= left or bottom <= top:
        raise CaptureError("Crop bounds have no visible area")
    # Round outward: fractional bounds must not silently remove edge pixels.
    box = max(0, floor(left)), max(0, floor(top)), min(image.width, ceil(right)), min(image.height, ceil(bottom))
    if box[2] <= box[0] or box[3] <= box[1]:
        raise CaptureError("Crop bounds lie outside the image")
    return _png(image.crop(box))


def stable(a: bytes, b: bytes, threshold: float = 2.0, *, bounds=None) -> bool:
    if not isfinite(threshold) or threshold < 0:
        raise CaptureError("Stability threshold must be finite and nonnegative")
    first, second = _image(a), _image(b)
    if first.size != second.size:
        return False
    if bounds is not None:
        box = tuple(floor(v) if i < 2 else ceil(v) for i, v in enumerate(bounds))
        if not (0 <= box[0] < box[2] <= first.width and 0 <= box[1] < box[3] <= first.height):
            raise CaptureError("Stability region lies outside the image")
        first, second = first.crop(box), second.crop(box)
    difference = np.abs(np.asarray(first, dtype=np.int16) - np.asarray(second, dtype=np.int16)).mean(axis=2)
    # Check strips separately so a short paragraph moving on a mostly blank
    # screen cannot disappear into a whole-image average.
    return all(float(strip.mean()) <= threshold and float((strip > 18).mean()) <= 0.008
               for strip in np.array_split(difference, 8, axis=0) if strip.size)


def _overlap_errors(previous: np.ndarray, current: np.ndarray, overlaps: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Score every suffix/prefix alignment with an FFT and prefix sums."""
    rows, width = previous.shape
    size = 1 << (rows + current.shape[0] - 2).bit_length()
    transform = np.fft.rfft(previous, size, axis=0) * np.fft.rfft(current[::-1], size, axis=0)
    correlation = np.fft.irfft(transform.sum(axis=1), size)
    dot = correlation[rows + current.shape[0] - overlaps - 1]

    def sums(values):
        return np.r_[0.0, np.cumsum(values.sum(axis=1))], np.r_[0.0, np.cumsum((values * values).sum(axis=1))]

    a, aa = sums(previous)
    b, bb = sums(current)
    count = overlaps * width
    a_sum, a_square = a[-1] - a[rows - overlaps], aa[-1] - aa[rows - overlaps]
    b_sum, b_square = b[overlaps], bb[overlaps]
    a_variance = np.maximum(0, a_square / count - (a_sum / count) ** 2)
    b_variance = np.maximum(0, b_square / count - (b_sum / count) ** 2)
    informative = (a_variance > 0.0004) & (b_variance > 0.0004)
    error = np.maximum(0, (a_square + b_square - 2 * dot) / count) / np.maximum(1e-8, a_variance + b_variance)
    return error, informative


def _unique_content_anchors(first: Image.Image, second: Image.Image, overlap: int) -> list[dict]:
    """Find two disjoint original-pixel strips unique in both whole frames.

    Repeated medicine footers can be a valid short suffix/prefix match as well
    as part of the real, longer overlap. They cannot prove displacement: every
    strip in that shorter match also occurs elsewhere in both frames. Unique
    image/text content may establish it without trusting gesture distance.
    """
    import cv2

    arrays = [np.asarray(image) for image in (first, second)]
    height = max(48, round(min(first.height, second.height) * .10))
    shift = first.height - overlap
    anchors = []
    last_end = 0
    offsets = sorted(set([*range(0, max(0, overlap - height + 1), max(1, height // 2)), overlap - height]))
    for offset in offsets:
        if offset < last_end or offset < 0:
            continue
        patch = arrays[1][offset:offset + height]
        if float(patch.std()) < 20:
            continue
        patch16 = patch.astype(np.int16)
        unique = True
        for array, expected in zip(arrays, (shift + offset, offset)):
            # Any block whose RGB max difference <= 18 and mean <= 2 has
            # mean-square difference <= 36. Search every original vertical
            # position, then verify all possible matches in original RGB.
            errors = cv2.matchTemplate(array, patch, cv2.TM_SQDIFF).ravel() / patch.size
            positions = []
            for y in np.flatnonzero(errors <= 36.25):
                difference = np.abs(array[y:y + height].astype(np.int16) - patch16)
                if difference.max() <= 18 and float(difference.mean()) <= 2:
                    positions.append(int(y))
            if expected not in positions or any(abs(y - expected) > 2 for y in positions):
                unique = False
                break
        if unique:
            anchors.append({"previous": [0, shift + offset, first.width, shift + offset + height],
                            "current": [0, offset, second.width, offset + height]})
            last_end = offset + height
            if len(anchors) == 2:
                return anchors
    return []


def match_overlap(previous: bytes, current: bytes, expected_shift: float | None = None, *, unique_content: bool = False) -> dict:
    """Return a verified suffix/prefix seam; score is confidence (1 is best).

    ``shift`` is previous-frame height minus overlap, including when the first
    frame was cropped. The requested gesture distance is only a search hint,
    never evidence that authorizes cropping. Ambiguous seams raise CaptureError.
    """
    first, second = _image(previous), _image(current)
    if first.width != second.width:
        raise CaptureError("Adjacent frames have different widths")
    if expected_shift is not None and (not isfinite(expected_shift) or expected_shift < 0):
        raise CaptureError("Expected shift must be finite and nonnegative")
    minimum = max(24, round(min(first.height, second.height) * 0.06))
    maximum = min(first.height, second.height)
    if maximum < minimum or first.width < 24:
        raise CaptureError("Not enough viewport content to verify an overlap")
    overlaps = np.arange(minimum, maximum + 1)
    width = min(192, first.width)
    reduced = [np.asarray(image.convert("L").resize((width, image.height), Image.Resampling.BILINEAR), dtype=np.float64) / 255
               for image in (first, second)]
    bands = ((0.04, 0.32), (0.36, 0.64), (0.68, 0.96))
    active = np.flatnonzero(np.minimum(reduced[0].var(axis=0), reduced[1].var(axis=0)) > .0004)
    narrow = len(active) >= 12 and active[-1] - active[0] < width * .65
    if narrow:
        # Keep independent bands inside the actual medicine column. The blank
        # remainder is still checked at full RGB resolution before cropping.
        start, span = active[0] / width, (active[-1] - active[0] + 1) / width
        bands = tuple((start + left * span, start + right * span) for left, right in bands)
    errors, information = [], []
    for left, right in bands:
        error, informative = _overlap_errors(reduced[0][:, round(width * left):round(width * right)], reduced[1][:, round(width * left):round(width * right)], overlaps)
        errors.append(error)
        information.append(informative)
    errors, information = np.array(errors), np.array(information)
    votes = ((errors <= 0.03) & information).sum(axis=0)
    eligible = (votes >= 2) & (votes > information.sum(axis=0) / 2)
    if not eligible.any():
        raise CaptureError("No matching overlap supported by multiple informative content bands")
    ranked = np.sort(np.where(information, errors, np.inf), axis=0)[:2].mean(axis=0)
    distance_hint = np.abs(first.height - overlaps - expected_shift) if expected_shift is not None else np.zeros_like(overlaps)
    order = np.lexsort((distance_hint, np.where(eligible, ranked, np.inf)))
    candidates = []
    for index in order:
        if not eligible[index]:
            break
        overlap = int(overlaps[index])
        if all(abs(overlap - value) > 2 for value in candidates):
            candidates.append(overlap)
        if len(candidates) == 12:
            break
    full = [np.asarray(image.convert("L"), dtype=np.float32) / 255 for image in (first, second)]
    verified = []
    for overlap in candidates:
        band_errors = []
        informative_count = 0
        for left, right in bands:
            # Pixel verification uses original resolution, preventing reduced
            # images from accepting unrelated fine patterns as the same text.
            a = full[0][-overlap:, round(first.width * left):round(first.width * right)]
            b = full[1][:overlap, round(first.width * left):round(first.width * right)]
            a_variance, b_variance = float(a.var()), float(b.var())
            if min(a_variance, b_variance) <= 0.0004:
                continue
            informative_count += 1
            error = float(np.square(a - b).mean()) / (a_variance + b_variance)
            if error <= 0.03:
                band_errors.append(error)
        if len(band_errors) >= 2 and len(band_errors) > informative_count / 2:
            # Bands locate the seam; every discarded pixel region must also
            # match. Callers must crop fixed controls out before matching.
            difference = np.abs(np.asarray(first, dtype=np.int16)[-overlap:] - np.asarray(second, dtype=np.int16)[:overlap])
            if difference.max() > 18 or any(float(strip.mean()) > 2
                                          for strip in np.array_split(difference, 8, axis=0) if strip.size):
                continue
            verified.append((float(np.mean(sorted(band_errors)[:2])), overlap))
    if not verified:
        raise CaptureError("Candidate overlap failed original-pixel verification")
    verified.sort()
    best_error, best_overlap = verified[0]
    tolerance = max(3, round(minimum * 0.05))
    anchors = []
    if any(abs(overlap - best_overlap) > tolerance and error <= best_error + 0.005 for error, overlap in verified[1:]):
        resolved = []
        if unique_content:
            for error, overlap in verified:
                if error <= best_error + 0.005 and (evidence := _unique_content_anchors(first, second, overlap)):
                    resolved.append((error, overlap, evidence))
        if len(resolved) != 1:
            raise CaptureError("Multiple distinct overlaps match; refusing to crop ambiguous content")
        best_error, best_overlap, anchors = resolved[0]
    result = {"overlap": best_overlap, "shift": first.height - best_overlap, "score": max(0.0, 1.0 - best_error ** 0.5), "verified": True}
    if anchors:
        result["unique_content_anchors"] = anchors
    return result


def match_product_overlap(previous: bytes, current: bytes, expected_shift: float | None = None) -> dict:
    """Verify medicine seams using unique pixels rather than common card footers."""
    result = match_overlap(previous, current, expected_shift, unique_content=True)
    if not result.get("unique_content_anchors"):
        anchors = _unique_content_anchors(_image(previous), _image(current), result["overlap"])
        if not anchors:
            # A changed image may invalidate the long candidate and leave only
            # a repeated footer. A single remaining candidate is not proof.
            raise CaptureError("Medicine overlap lacks two independent unique content anchors")
        result["unique_content_anchors"] = anchors
    return result


def compose(frames: list[bytes], overlaps: list[int], max_height: int = 16000) -> list[bytes]:
    if not frames or len(overlaps) != len(frames) - 1:
        raise CaptureError("Composition requires one fewer overlap than frames")
    if isinstance(max_height, bool) or not isinstance(max_height, int) or max_height <= 0:
        raise CaptureError("Maximum output height must be a positive integer")
    images = [_image(frame) for frame in frames]
    width = images[0].width
    if any(image.width != width for image in images):
        raise CaptureError("Cannot compose frames of different widths")
    for index, overlap in enumerate(overlaps):
        if isinstance(overlap, bool) or not isinstance(overlap, int) or not 0 <= overlap <= min(images[index].height, images[index + 1].height):
            raise CaptureError("Overlap is outside the adjacent frame bounds")
    result = []
    canvas = Image.new("RGB", (width, max_height))
    used = 0
    for image, start in zip(images, [0, *overlaps]):
        while start < image.height:
            amount = min(max_height - used, image.height - start)
            canvas.paste(image.crop((0, start, width, start + amount)), (0, used))
            start += amount
            used += amount
            if used == max_height:
                result.append(_png(canvas))
                canvas = Image.new("RGB", (width, max_height))
                used = 0
    if used:
        result.append(_png(canvas.crop((0, 0, width, used))))
    return result
