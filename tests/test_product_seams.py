from io import BytesIO

import numpy as np
from PIL import Image, ImageDraw
import pytest

from geo_collector.capture import verify_capture_images
from geo_collector.images import CaptureError, compose, match_overlap, match_product_overlap


def png(image):
    stream = BytesIO()
    image.save(stream, "PNG")
    return stream.getvalue()


def medicine_frames(scale=1, *, repeated=False):
    """Distinct drug photos in a narrow column, with identical card footers."""
    source = Image.new("RGB", (360, 1200), "white")
    draw = ImageDraw.Draw(source)
    for index in range(4):
        y = index * 300
        seed = 1 if repeated else index + 1
        pixels = np.random.default_rng(seed).integers(0, 256, (160, 120, 3), dtype=np.uint8)
        source.paste(Image.fromarray(pixels), (10, y + 30))
        draw.text((10, y + 5), f"PRODUCT {0 if repeated else index}", fill="black")
        draw.text((10, y + 200), "SHARED MEDICARE", fill="black")
        draw.rectangle((10, y + 235, 130, y + 280), outline="#999999", width=2)
        draw.text((20, y + 250), "INSTRUCTIONS", fill="black")
    images = [source.crop((0, 20, 360, 600)), source.crop((0, 210, 360, 790))]
    return [image.resize((360 * scale, 580 * scale), Image.Resampling.NEAREST) for image in images]


@pytest.mark.parametrize("scale", [1, 2])
@pytest.mark.parametrize("hint", [None, 190, 490])
def test_repeated_footer_is_resolved_by_unique_original_content_not_gesture(scale, hint):
    before, after = [png(image) for image in medicine_frames(scale)]
    expected = None if hint is None else hint * scale
    with pytest.raises(CaptureError, match="ambiguous"):
        match_overlap(before, after, expected_shift=expected)
    seam = match_product_overlap(before, after, expected_shift=expected)
    assert seam["overlap"] == 390 * scale and seam["shift"] == 190 * scale
    assert seam["verified"] and len(seam["unique_content_anchors"]) == 2
    # Rebuild the complete visible interval, without deleting the middle drug.
    joined = Image.open(BytesIO(compose([before, after], [seam["overlap"]])[0]))
    assert joined.height == 770 * scale
    assert np.array_equal(np.asarray(joined)[:580 * scale], np.asarray(Image.open(BytesIO(before))))
    assert np.array_equal(np.asarray(joined)[580 * scale:], np.asarray(Image.open(BytesIO(after)))[390 * scale:])


def test_repeated_whole_drug_cards_remain_ambiguous_despite_expected_gesture():
    before, after = [png(image) for image in medicine_frames(repeated=True)]
    for expected in (None, 190, 490):
        with pytest.raises(CaptureError, match="ambiguous"):
            match_product_overlap(before, after, expected_shift=expected)


@pytest.mark.parametrize("mutation", ["dose", "photo", "unrelated"])
def test_changed_medicine_cannot_be_cropped_using_the_only_remaining_footer(mutation):
    before, after = medicine_frames()
    draw = ImageDraw.Draw(after)
    if mutation == "dose":
        draw.text((140, 170), "5 mg", fill="black")
    elif mutation == "photo":
        draw.rectangle((20, 200, 30, 210), fill="black")
    else:
        replacement = np.random.default_rng(80).integers(0, 256, (490, 360, 3), dtype=np.uint8)
        after.paste(Image.fromarray(replacement), (0, 90))
    first, second = png(before), png(after)
    # The common footer alone is still an exact suffix/prefix match.
    assert match_overlap(first, second)["overlap"] == 90
    with pytest.raises(CaptureError, match="unique content"):
        match_product_overlap(first, second, expected_shift=190)


def test_small_pixel_noise_preserves_the_unique_evidence():
    before, after = medicine_frames()
    array = np.asarray(after).astype(np.int16)
    noise = np.random.default_rng(9).integers(-1, 2, array.shape)
    second = Image.fromarray(np.clip(array + noise, 0, 255).astype(np.uint8))
    assert match_product_overlap(png(before), png(second))["overlap"] == 390


def delivery_record(tmp_path):
    debug, delivery = tmp_path / "debug", tmp_path / "delivery"
    debug.mkdir()
    delivery.mkdir()
    body = png(Image.new("RGB", (360, 100), "white"))
    body_frame = debug / "body.png"
    body_frame.write_bytes(body)
    body_output = delivery / "body.png"
    body_output.write_bytes(body)
    frames = [png(image) for image in medicine_frames()]
    frame_files = []
    for index, frame in enumerate(frames):
        path = debug / f"product-{index}.png"
        path.write_bytes(frame)
        frame_files.append(str(path))
    product_output = delivery / "products.png"
    product_output.write_bytes(compose(frames, [390])[0])
    result = {"frame_files": [str(body_frame)], "frame_count": 1, "seams": [],
              "image_paths": [str(body_output), str(product_output)],
              "products": {"present": True, "mode": "drawer", "frame_count": 2,
                           "frame_files": frame_files, "image_paths": [str(product_output)],
                           "seams": [{"verified": True, "overlap": 390, "shift": 190}]}}
    return result, debug, delivery, frames, product_output


def test_delivery_verify_recomputes_unique_anchors_without_saved_confirmation_flags(tmp_path):
    result, debug, delivery, _, _ = delivery_record(tmp_path)
    assert "unique_content_anchors" not in result["products"]["seams"][0]
    verify_capture_images(result, debug, delivery)


def test_delivery_verify_rejects_a_forged_footer_seam_even_when_composed_pixels_match_it(tmp_path):
    result, debug, delivery, frames, product_output = delivery_record(tmp_path)
    result["products"]["seams"][0].update(overlap=90, shift=490, unique_content_anchors=[{}, {}])
    product_output.write_bytes(compose(frames, [90])[0])
    with pytest.raises(CaptureError, match="接缝与原始帧不一致"):
        verify_capture_images(result, debug, delivery)


def test_delivery_verify_rejects_changed_original_drug_even_with_forged_anchor_flags(tmp_path):
    result, debug, delivery, frames, product_output = delivery_record(tmp_path)
    after = Image.open(BytesIO(frames[1])).convert("RGB")
    ImageDraw.Draw(after).rectangle((20, 200, 30, 210), fill="black")
    frames[1] = png(after)
    from pathlib import Path
    Path(result["products"]["frame_files"][1]).write_bytes(frames[1])
    result["products"]["seams"][0].update(overlap=90, shift=490, unique_content_anchors=[{}, {}])
    product_output.write_bytes(compose(frames, [90])[0])
    with pytest.raises(CaptureError, match="unique content"):
        verify_capture_images(result, debug, delivery)
