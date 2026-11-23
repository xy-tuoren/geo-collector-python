from io import BytesIO
import time

import numpy as np
from PIL import Image, ImageDraw
import pytest

from geo_collector.images import CaptureError, compose, crop, image_size, match_overlap, stable


def png(image):
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def texture(width, height, seed=1):
    values = np.random.default_rng(seed).integers(0, 256, (height, width, 3), dtype=np.uint8)
    return Image.fromarray(values)


def document(width, height):
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    rng = np.random.default_rng(4)
    for y in range(12, height - 14, 29):
        line = " ".join(str(number) for number in rng.integers(1000, 9999, max(10, width // 32)))
        draw.text((int(width * 0.045), y), line, fill="black")
        draw.line((int(width * 0.05), y + 16, int(width * float(rng.uniform(0.65, 0.94))), y + 16), fill=(85, 85, 85), width=2)
    return image


@pytest.mark.parametrize("width,height", [(360, 800), (720, 1600)])
@pytest.mark.parametrize("factory", [texture, document])
def test_exact_scroll_and_short_tail_at_two_scales(width, height, factory):
    source = factory(width, height * 2)
    for shift in (int(height * 0.44), 9):
        before = png(source.crop((0, 0, width, height)))
        after = png(source.crop((0, shift, width, shift + height)))
        result = match_overlap(before, after, expected_shift=height * 0.44)
        assert result["verified"] and result["overlap"] == height - shift
        assert result["shift"] == shift and result["score"] > 0.99
        assert not stable(before, after)


@pytest.mark.parametrize("width,height", [(360, 800), (720, 1600)])
def test_short_first_frame_requires_fixed_controls_to_be_excluded(width, height):
    source = texture(width, height * 2)
    shift, first_crop = int(height * 0.4), int(height * 0.18)
    before = source.crop((0, first_crop, width, height))
    after = source.crop((0, shift, width, height + shift))
    result = match_overlap(png(before), png(after), expected_shift=shift)
    assert result["overlap"] == height - shift
    assert result["shift"] == shift - first_crop
    for image in (before, after):
        ImageDraw.Draw(image).rectangle((width * 0.41, image.height * 0.89, width * 0.59, image.height * 0.98), fill="#202020")
    with pytest.raises(CaptureError, match="original-pixel"):
        match_overlap(png(before), png(after), expected_shift=shift)


@pytest.mark.parametrize("left,right", [(0, .03), (.40, .60), (.70, .95)])
def test_wide_answer_rejects_changed_content_even_when_other_bands_match(left, right):
    width, height, shift = 360, 800, 320
    source = document(width, height * 2)
    before = source.crop((0, 0, width, height))
    after = source.crop((0, shift, width, height + shift))
    ImageDraw.Draw(after).rectangle((round(width * left), 15, round(width * right) - 1, 180), fill="black")
    with pytest.raises(CaptureError, match="original-pixel"):
        match_overlap(png(before), png(after), expected_shift=shift)


def test_overlap_preserves_small_pixel_noise_tolerance():
    source = document(360, 1600)
    before = png(source.crop((0, 0, 360, 800)))
    after = np.asarray(source.crop((0, 320, 360, 1120))).astype(np.int16)
    noise = np.random.default_rng(3).integers(-2, 3, after.shape)
    after = png(Image.fromarray(np.clip(after + noise, 0, 255).astype(np.uint8)))
    assert match_overlap(before, after, expected_shift=320)["overlap"] == 480


def test_overlap_rejects_small_dose_text_update():
    source = document(360, 1600)
    before = source.crop((0, 0, 360, 800))
    after = source.crop((0, 320, 360, 1120))
    ImageDraw.Draw(after).text((265, 11), "1.5mg", fill="black")
    with pytest.raises(CaptureError, match="original-pixel"):
        match_overlap(png(before), png(after), expected_shift=320)


def test_blank_unrelated_and_repeated_content_are_not_verified():
    blank = png(Image.new("RGB", (360, 800), "white"))
    with pytest.raises(CaptureError):
        match_overlap(blank, blank)
    with pytest.raises(CaptureError):
        match_overlap(png(texture(360, 800, 1)), png(texture(360, 800, 2)))
    tile = texture(360, 80)
    repeated = Image.new("RGB", (360, 800))
    for y in range(0, 800, 80):
        repeated.paste(tile, (0, y))
    with pytest.raises(CaptureError, match="ambiguous"):
        match_overlap(png(repeated), png(repeated), expected_shift=320)


@pytest.mark.parametrize("width,height", [(360, 800), (720, 1600)])
def test_narrow_medicine_column_still_requires_multiple_bands_and_full_overlap(width, height):
    source = Image.new("RGB", (width, height * 2), "white")
    source.paste(texture(round(width * .29), height * 2), (round(width * .035), 0))
    before = source.crop((0, 0, width, height))
    ImageDraw.Draw(before).text((width * .45, 15), "MEDICINES", fill="black")
    shift = round(height * .43)
    after = source.crop((0, shift, width, shift + height))
    seam = match_overlap(png(before), png(after), expected_shift=shift)
    assert seam["verified"] and seam["overlap"] == height - shift and seam["score"] > .99
    # Content added in the previously blank side must not be silently cropped.
    ImageDraw.Draw(after).rectangle((width * .5, 0, width * .9, height * .15), fill="black")
    with pytest.raises(CaptureError):
        match_overlap(png(before), png(after), expected_shift=shift)


def test_stability_ignores_pixel_noise_but_not_local_text_movement():
    source = document(360, 800)
    array = np.asarray(source).astype(np.int16)
    noise = np.random.default_rng(3).integers(-2, 3, array.shape)
    assert stable(png(source), png(Image.fromarray(np.clip(array + noise, 0, 255).astype(np.uint8))))
    altered = source.copy()
    altered.paste(source.crop((0, 100, 360, 180)), (0, 107))
    assert not stable(png(source), png(altered))
    assert not stable(png(source), png(source.crop((0, 0, 360, 700))))


def test_crop_rounds_outward_and_composition_preserves_every_output_pixel():
    source = texture(72, 730)
    first = crop(png(source), (0, 20.4, 72, 320.2))
    second = png(source.crop((0, 210, 72, 540)))
    third = png(source.crop((0, 450, 72, 730)))
    assert image_size(first) == (72, 301)
    parts = compose([first, second, third], [111, 90], max_height=199)
    assert [image_size(part)[1] for part in parts] == [199, 199, 199, 113]
    combined = np.concatenate([np.asarray(Image.open(BytesIO(part))) for part in parts], axis=0)
    assert np.array_equal(combined, np.asarray(source)[20:730])
    for overlaps in ([], [-1, 90], [500, 90], [True, 90]):
        with pytest.raises(CaptureError):
            compose([first, second, third], overlaps)
    with pytest.raises(CaptureError):
        crop(first, (1, 1, float("nan"), 10))


def test_overlap_runtime_is_bounded_for_a_normal_phone():
    source = texture(1080, 3400)
    before = png(source.crop((0, 0, 1080, 2000)))
    after = png(source.crop((0, 850, 1080, 2850)))
    started = time.monotonic()
    assert match_overlap(before, after, expected_shift=850)["overlap"] == 1150
    # A generous regression ceiling, not a claim about every host or phone.
    assert time.monotonic() - started < 5
