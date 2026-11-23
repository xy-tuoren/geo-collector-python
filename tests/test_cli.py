"""CLI input, artifact acceptance and locking tests; no phone is accessed."""
import json
from pathlib import Path

from PIL import Image
import pytest

from geo_collector import cli, device
from geo_collector.storage import DeviceLease, read_json, write_json


def args(*values):
    return cli.parser().parse_args(values)


def test_agent_guide_is_available_offline_outside_project(tmp_path, monkeypatch):
    skill = Path(cli.__file__).resolve().parents[2] / ".agents/skills/geo-collector/SKILL.md"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(device, "Device", lambda **_: pytest.fail("guide must not connect to a phone"))
    assert cli.execute(args("agent-guide")) == {"format": "markdown", "guide": skill.read_text(encoding="utf-8")}


def question_file(tmp_path):
    file = tmp_path / "questions.txt"
    file.write_text("\ufeff  示例问题甲  \n\n示例问题乙\n  \n示例问题丙\n", encoding="utf-8")
    return file


def test_questions_preserve_source_line_numbers_bom_and_requested_order(tmp_path):
    file = question_file(tmp_path)
    assert cli.questions(args("run", "--questions-file", str(file))) == [
        {"index": 1, "text": "示例问题甲"}, {"index": 3, "text": "示例问题乙"}, {"index": 5, "text": "示例问题丙"},
    ]
    selected = args("run", "--questions-file", str(file), "--question-index", "5,1,5")
    assert cli.questions(selected) == [{"index": 5, "text": "示例问题丙"}, {"index": 1, "text": "示例问题甲"}]


@pytest.mark.parametrize("selection", ["0", "2", "6"])
def test_blank_or_nonexistent_source_lines_are_not_silently_renumbered(tmp_path, selection):
    with pytest.raises(ValueError, match="题号不存在"):
        cli.questions(args("run", "--questions-file", str(question_file(tmp_path)), "--question-index", selection))


@pytest.mark.parametrize("arguments", [
    ["--question-index", "1", "--entry", "xiaohe", "douyin"],
    ["--question-index", "1,3", "--entry", "xiaohe"],
])
def test_current_answer_rejects_more_than_one_question_or_entry_before_creating_run(tmp_path, arguments):
    output = tmp_path / "captures"
    options = args("run", "--capture-current-answer", "--questions-file", str(question_file(tmp_path)),
                   "--output", str(output), *arguments)
    with pytest.raises(ValueError, match="一个入口和一道"):
        cli.new_run(options)
    assert not output.exists()


def test_current_answer_creates_exactly_one_task_with_original_question_index(tmp_path):
    options = args("run", "--capture-current-answer", "--questions-file", str(question_file(tmp_path)),
                   "--question-index", "3", "--entry", "douyin", "--output", str(tmp_path / "captures"),
                   "--rule-dir", str(tmp_path / "rules"))
    directory, state = cli.new_run(options)
    assert state["mode"] == "current"
    assert state["entries"] == ["douyin"]
    assert state["questions"] == [{"index": 3, "text": "示例问题乙"}]
    assert state["tasks"] == [{"entry": "douyin", "index": 3, "text": "示例问题乙", "status": "pending", "phase": "start"}]
    assert read_json(directory / "调试产物" / "run.json") == state


def test_toutiao_requires_explicit_selection_and_learns_its_own_search(tmp_path):
    assert args("run", "--question", "示例问题").entry == ["xiaohe", "douyin"]
    options = args("run", "--question", "示例问题", "--entry", "toutiao", "--learn",
                   "--output", str(tmp_path / "captures"), "--rule-dir", str(tmp_path / "rules"))
    _, state = cli.new_run(options)
    assert state["entries"] == ["toutiao"]
    assert state["teach"] == ["page.perception.toutiao", "toutiao.open_search"]
    assert state["tasks"][0]["entry"] == "toutiao"


@pytest.fixture
def complete_run(tmp_path):
    directory = tmp_path / "batch"
    image_file = directory / "交付图片" / "xiaohe" / "003" / "answer.png"
    image_file.parent.mkdir(parents=True)
    Image.new("RGB", (120, 260), "white").save(image_file, format="PNG")
    state_file = directory / "调试产物" / "run.json"
    task = {"entry": "xiaohe", "index": 3, "text": "示例问题乙", "status": "complete"}
    state = {"artifact_layout_version": 1, "status": "complete", "entries": ["xiaohe"],
             "questions": [{"index": 3, "text": "示例问题乙"}], "tasks": [task], "agent_requests": 1}
    answer_file = directory / "调试产物" / "xiaohe" / "003" / "answer.json"
    frame = answer_file.parent / "capture-test" / "frame-001.png"
    frame.parent.mkdir(parents=True)
    frame.write_bytes(image_file.read_bytes())
    answer = {"entry": "xiaohe", "index": 3, "question": "示例问题乙", "question_confirmed": True,
              "citations_checked": True, "citations_found": True, "citations_expanded": True,
              "completion_confirmed": True, "end_confirmed": True, "occlusion_checked": True, "frame_count": 1, "seams": [],
              "products": {"present": False, "complete": True}, "image_paths": [str(image_file)], "frame_files": [str(frame)]}
    write_json(state_file, state)
    write_json(answer_file, answer)
    return directory, state_file, answer_file, image_file


def test_verify_accepts_only_a_complete_artifact_set_and_returns_success_exit_code(complete_run, capsys):
    directory, *_ = complete_run
    result = cli.verify_run(directory)
    assert result["complete"] is True and result["tasks"] == 1 and result["problems"] == []
    assert cli.main(["verify", str(directory)]) == 0
    assert json.loads(capsys.readouterr().out)["complete"] is True


def test_toutiao_entry_absence_cannot_use_historical_multiscreen_evidence(complete_run):
    directory, state_file, _, _ = complete_run
    state = read_json(state_file)
    result_file = directory / "调试产物" / "toutiao" / "003" / "entry-not-found.json"
    state["entries"] = ["toutiao"]
    task = state["tasks"][0]
    task.update(entry="toutiao", outcome="entry_not_found", result=str(result_file))
    write_json(state_file, state)
    write_json(result_file, {"entry": "toutiao", "index": 3, "question": task["text"],
                            "status": "complete", "outcome": "entry_not_found", "search_query_confirmed": True,
                            "search_results_confirmed": True, "scan_complete": True, "platform_entry_found": False,
                            "scan_scope": {"max_viewports": 5, "viewports_checked": 5, "limit_reached": True}})
    result = cli.verify_run(directory)
    assert not result["complete"] and any("仅接受首屏" in problem for problem in result["problems"])


def test_verify_drawer_requires_its_own_images_and_seam_evidence(complete_run):
    from geo_collector.images import match_overlap
    from test_images import png, texture

    directory, _, answer_file, image_file = complete_run
    answer = read_json(answer_file)
    answer["products"] = {"present": True, "mode": "drawer", "complete": True, "first_item_confirmed": True,
                          "end_confirmed": True, "images_ready": True, "seams_verified": True, "frame_count": 2,
                          "seams": [], "image_paths": []}
    write_json(answer_file, answer)
    result = cli.verify_run(directory)
    assert not result["complete"] and any("药品交付图片缺失" in problem for problem in result["problems"])
    source = texture(120, 420)
    frames = [answer_file.parent / "capture-test" / f"product-frame-{index:03d}.png" for index in (1, 2)]
    frames[0].write_bytes(png(source.crop((0, 0, 120, 260))))
    frames[1].write_bytes(png(source.crop((0, 160, 120, 420))))
    seam = match_overlap(*(frame.read_bytes() for frame in frames))
    product_image = image_file.with_name("products.png")
    source.save(product_image)
    answer["image_paths"].append(str(product_image))
    answer["products"].update(seams=[seam], image_paths=[str(product_image)], frame_files=list(map(str, frames)))
    write_json(answer_file, answer)
    assert cli.verify_run(directory)["complete"]
    # A valid PNG with the same dimensions must still match its medicine frames.
    Image.new("RGB", source.size).save(product_image)
    assert not cli.verify_run(directory)["complete"]


@pytest.mark.parametrize("mutation", ["replaced_pixels", "resized", "transparent", "extra_png", "missing_frame", "missing_evidence", "foreign_frame"])
def test_verify_requires_original_frames_and_matching_delivery_pixels(complete_run, mutation):
    directory, _, answer_file, image_file = complete_run
    answer = read_json(answer_file)
    if mutation == "replaced_pixels":
        Image.new("RGB", (120, 260), "black").save(image_file)
    elif mutation == "resized":
        Image.new("RGB", (1, 1), "white").save(image_file)
    elif mutation == "transparent":
        Image.new("RGBA", (120, 260), (255, 255, 255, 0)).save(image_file)
    elif mutation == "extra_png":
        Image.new("RGB", (120, 260), "black").save(image_file.with_name("answer-old.png"))
    elif mutation == "missing_frame":
        Path(answer["frame_files"][0]).unlink()
    elif mutation == "missing_evidence":
        answer.pop("frame_files")
    else:
        answer["frame_files"] = [str(image_file)]
    write_json(answer_file, answer)
    assert not cli.verify_run(directory)["complete"]


def test_verify_rechecks_recorded_seams_against_source_pixels(complete_run):
    from geo_collector.images import match_overlap
    from test_images import png, texture

    directory, _, answer_file, image_file = complete_run
    answer = read_json(answer_file)
    source = texture(120, 420)
    first = Path(answer["frame_files"][0])
    second = first.with_name("frame-002.png")
    first.write_bytes(png(source.crop((0, 0, 120, 260))))
    second.write_bytes(png(source.crop((0, 160, 120, 420))))
    seam = match_overlap(first.read_bytes(), second.read_bytes())
    answer.update(frame_files=[str(first), str(second)], frame_count=2, seams=[seam])
    source.save(image_file)
    write_json(answer_file, answer)
    assert cli.verify_run(directory)["complete"]
    seam["overlap"] -= 1
    write_json(answer_file, answer)
    result = cli.verify_run(directory)
    assert not result["complete"] and any("接缝与原始帧不一致" in item for item in result["problems"])


@pytest.mark.parametrize("mutation", ["empty", "missing_entry", "duplicate", "unfinished"])
def test_verify_rejects_empty_missing_duplicate_and_unfinished_tasks(complete_run, mutation):
    directory, state_file, *_ = complete_run
    state = read_json(state_file)
    if mutation == "empty":
        state.update(entries=[], questions=[], tasks=[])
    elif mutation == "missing_entry":
        state["entries"].append("douyin")
    elif mutation == "duplicate":
        state["tasks"].append(dict(state["tasks"][0]))
    else:
        state["tasks"][0]["status"] = "failed"
    write_json(state_file, state)
    result = cli.verify_run(directory)
    assert result["complete"] is False and result["problems"]


@pytest.mark.parametrize("changes", [
    {"question": "上一道不同问题"},
    {"entry": "douyin"},
    {"index": 1},
    {"citations_checked": False},
    {"citations_expanded": False},
    {"frame_count": True},
    {"frame_count": 2},
    {"frame_count": 2, "seams": [{"verified": False, "overlap": 60}]},
    {"products": {"present": True, "complete": False}},
    {"products": {"present": True, "complete": True, "first_item_confirmed": True,
                  "end_confirmed": True, "images_ready": False, "seams_verified": True}},
])
def test_verify_does_not_trust_complete_flags_when_identity_or_capture_evidence_fails(complete_run, changes):
    directory, _, answer_file, _ = complete_run
    answer = read_json(answer_file)
    answer.update(changes)
    write_json(answer_file, answer)
    result = cli.verify_run(directory)
    assert result["complete"] is False and result["problems"]


def test_verify_rejects_an_image_from_another_question_even_inside_the_same_batch(complete_run):
    directory, _, answer_file, _ = complete_run
    other_image = directory / "交付图片" / "xiaohe" / "099" / "answer.png"
    other_image.parent.mkdir(parents=True)
    Image.new("RGB", (120, 260), "black").save(other_image, format="PNG")
    answer = read_json(answer_file)
    answer["image_paths"] = [str(other_image)]
    write_json(answer_file, answer)
    result = cli.verify_run(directory)
    assert result["complete"] is False and result["problems"]


@pytest.mark.parametrize("mutation", ["jpeg_with_png_suffix", "missing_png", "diagnostics_in_delivery"])
def test_verify_inspects_real_delivery_files_and_returns_failure_exit_code(complete_run, mutation, capsys):
    directory, _, _, image_file = complete_run
    if mutation == "jpeg_with_png_suffix":
        Image.new("RGB", (120, 260), "white").save(image_file, format="JPEG")
    elif mutation == "missing_png":
        image_file.unlink()
    else:
        image_file.with_name("private.xml").write_text("<hierarchy/>", encoding="utf-8")
    assert cli.main(["verify", str(directory)]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["complete"] is False and result["problems"]


def test_verify_ignores_macos_finder_metadata(complete_run):
    directory, *_ = complete_run
    (directory / "交付图片" / ".DS_Store").write_bytes(b"Finder metadata")
    assert cli.verify_run(directory)["complete"] is True


def test_global_lease_blocks_all_rule_directories_before_device_initialization(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    def never_connect(**_):
        pytest.fail("a conflicting collector must not initialize a phone")
    monkeypatch.setattr(device, "Device", never_connect)
    with DeviceLease(tmp_path / ".cache" / "geo-collector", "phone-control"):
        for rule_dir in (tmp_path / "store-a", tmp_path / "store-b"):
            with pytest.raises(RuntimeError, match="已有采集进程"):
                cli.execute(args("doctor", "--serial", "fake-device", "--rule-dir", str(rule_dir)))
