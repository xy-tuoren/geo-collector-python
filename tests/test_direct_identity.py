"""Offline direct-entry delivery identity and resume guards."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
import pytest

from geo_collector.capture import (AnswerCapture, answer_viewport, direct_question_bubble,
                                   verify_capture_images, verify_direct_capture_identity)
from geo_collector.cli import verify_run
from geo_collector.images import CaptureError, crop
from geo_collector.platforms import douyin_direct as direct
from geo_collector.recovery import save_observation
from geo_collector.storage import read_json, write_json
from test_douyin_direct import DirectPhone


@pytest.fixture
def delivery(tmp_path):
    root = (tmp_path / "run").resolve()
    debug = root / "调试产物" / direct.ID / "001"
    output = root / "交付图片" / direct.ID / "001"
    output.mkdir(parents=True)
    phone = DirectPhone(stage="clean")
    question = "本次完整问题"
    save_observation(debug / "new-session", phone.observe())
    phone.draft = question
    save_observation(debug / "before-send", phone.observe())
    phone.stage, phone.draft, phone.question, phone.marker = "answer", "", question, False
    observed = phone.observe()
    proof_root = debug / "submission-confirm-offline"
    save_observation(proof_root / "tail", observed)
    save_observation(proof_root / "question", observed)
    end_observations = [str(proof_root / f"end-{index}") for index in range(3)]
    for path in end_observations:
        save_observation(path, observed)
    proof = {"identity": [question, direct.PACKAGE, direct.ID, 1], "root": str(debug),
             "new_session": str(debug / "new-session"), "before_send": str(debug / "before-send"),
             "latest_tail": str(proof_root / "tail"), "question_observation": str(proof_root / "question"),
             "send_intent": {"step": direct.ID + ".send", "risk": "send", "before": 123.0},
             "latest_user_question": True, "end_observations": end_observations,
             "backtrack": [{"observation": str(proof_root / "question")}]}
    paths = {proof[key] for key in ("new_session", "before_send", "latest_tail", "question_observation")}
    paths.update(end_observations)
    proof["evidence_hashes"] = {path: direct._evidence_hash(Path(path)) for path in paths}
    task = {"entry": direct.ID, "index": 1, "text": question, "status": "complete", "phase": "done",
            "direct_submission": proof}
    state = {"status": "complete", "entries": [direct.ID], "questions": [{"index": 1, "text": question}],
             "entry": direct.ID, "index": 1, "tasks": [task]}
    write_json(root / "调试产物" / "run.json", state)
    attempt = debug / "capture-offline"
    first_scene = attempt / "001-first-frame"
    save_observation(first_scene, observed)
    viewport = answer_viewport(observed)
    target = direct_question_bubble(observed, question, viewport)
    bounds = [viewport[0], target["bounds"][1] - 2, viewport[2], viewport[3]]
    frame = attempt / "frame-001.png"
    frame.write_bytes(crop(observed.png, bounds))
    final = output / "回答_001.png"
    final.write_bytes(frame.read_bytes())
    result = {"entry": direct.ID, "index": 1, "question": question, "question_confirmed": True,
              "question_visible": True, "citations_checked": True, "citations_found": False,
              "completion_confirmed": True, "end_confirmed": True, "occlusion_checked": True,
              "frame_count": 1, "frame_files": [str(frame)], "seams": [], "image_paths": [str(final)],
              "frame_viewports": [{"viewport_physical": bounds}], "products": {"present": False, "complete": True},
              "direct_submission": proof, "direct_first_frame": str(first_scene)}
    write_json(debug / "answer.json", result)
    return SimpleNamespace(root=root, debug=debug, output=output, task=task, state=state, proof=proof,
                           observed=observed, result=result, frame=frame, final=final, phone=phone)


def test_direct_delivery_verifies_original_send_and_first_frame_without_divider(delivery):
    assert direct.submission_evidence(delivery.task, delivery.root)
    verify_capture_images(delivery.result, delivery.debug, delivery.output)
    assert verify_run(delivery.root)["complete"]


@pytest.mark.parametrize("change", ["missing", "other_task", "altered", "foreign_scene"])
def test_direct_delivery_rejects_missing_or_foreign_send_identity(delivery, change):
    result = deepcopy(delivery.result)
    if change == "missing":
        result.pop("direct_submission")
    elif change == "other_task":
        result["direct_submission"]["identity"][3] = 2
    elif change == "altered":
        result["direct_submission"]["send_intent"]["before"] = 456.0
    else:
        result["direct_first_frame"] = str(delivery.root / "other" / "001-first-frame")
    with pytest.raises(CaptureError):
        verify_direct_capture_identity(result, delivery.debug)


@pytest.mark.parametrize("which", ["new_session", "before_send", "question_observation"])
def test_direct_delivery_reopens_original_evidence_instead_of_trusting_flags(delivery, which):
    Path(delivery.proof[which], "screen.png").unlink()
    with pytest.raises(CaptureError):
        verify_direct_capture_identity(delivery.result, delivery.debug)
    assert not verify_run(delivery.root)["complete"]


def test_direct_delivery_rejects_replaced_body_pixels_even_if_output_matches(delivery):
    # Ordinary seam/output matching would accept both files being replaced.
    with Image.open(delivery.frame) as original:
        Image.new("RGB", original.size, "white").save(delivery.frame)
    delivery.final.write_bytes(delivery.frame.read_bytes())
    with pytest.raises(CaptureError, match="首帧 PNG"):
        verify_capture_images(delivery.result, delivery.debug, delivery.output)


def test_direct_delivery_rejects_original_observation_with_a_different_user_question(delivery):
    delivery.phone.question = "后来的另一个问题"
    save_observation(delivery.result["direct_first_frame"], delivery.phone.observe())
    with pytest.raises(CaptureError):
        verify_direct_capture_identity(delivery.result, delivery.debug)


@pytest.mark.parametrize("checkpoint", [None, "body_resume", "products_resume"])
def test_direct_capture_and_resume_fail_before_phone_access_without_send_proof(tmp_path, checkpoint):
    root = tmp_path / "run"
    debug = root / "调试产物" / direct.ID / "001"
    question = "本次完整问题"
    state = {"entry": direct.ID, "index": 1, "tasks": [{"entry": direct.ID, "index": 1, "text": question}]}
    if checkpoint:
        geometry = {"logical_size": [720, 1600], "physical_size": [720, 1600]}
        state[checkpoint] = {"question": question, "package": direct.PACKAGE, "entry": direct.ID, "index": 1,
                             "attempt": str(debug / "capture-paused"), "counter": 1,
                             "geometry": geometry, "body": {"geometry": geometry}}
    engine = SimpleNamespace(state=state, run_dir=root, log=lambda *a, **k: None)
    phone = SimpleNamespace(observe=lambda *a, **k: pytest.fail("Unverified direct identity must not touch the phone"))
    capture = AnswerCapture(phone, engine)
    with pytest.raises(CaptureError, match="原始发送身份证据"):
        capture.run(question, direct.PACKAGE, root / "交付图片" / direct.ID / "001", debug)


def test_cli_direct_verify_rejects_removed_task_proof_even_with_complete_flags(delivery):
    state = read_json(delivery.root / "调试产物" / "run.json")
    state["tasks"][0].pop("direct_submission")
    write_json(delivery.root / "调试产物" / "run.json", state)
    result = verify_run(delivery.root)
    assert not result["complete"]
    assert any("发送身份证据" in message for message in result["problems"])


@pytest.mark.parametrize('fault', [None, 'wrong_question', 'missing_proof', 'products', 'uncertain'])
def test_recorded_direct_recapture_retains_send_and_original_frames(delivery, tmp_path, fault):
    from geo_collector.rules import RuleStore
    from geo_collector.workflow import recover_current
    state = read_json(delivery.root / '调试产物' / 'run.json')
    task = state['tasks'][0]
    task.update(status='needs_repair', phase='capture')
    original = delivery.frame.read_bytes()
    body = {'attempt': str(delivery.frame.parent), 'entry': direct.ID, 'index': 1, 'question': task['text']}
    state.update(body_resume=body, phase='capture', serial=delivery.phone.serial)
    if fault == 'wrong_question':
        body['question'] = '另一题'
    if fault == 'missing_proof':
        task.pop('direct_submission')
    if fault == 'products':
        state['products_resume'] = {'phase': 'list'}
    if fault == 'uncertain':
        state['inflight'] = {'step': direct.ID + '.send'}
    def recover():
        recover_current(delivery.phone, RuleStore(tmp_path/'rules'), delivery.root, state, 'recapture-body', '保存首帧接缝不连续')
    archive = delivery.frame.parent/'superseded-checkpoint.json'
    if fault:
        with pytest.raises(ValueError):
            recover()
        assert state['body_resume'] == body and not archive.exists()
    else:
        recover()
        assert read_json(archive) == body and 'body_resume' not in state
        assert task['direct_submission'] == delivery.proof and state['recovery_actions'][-1]['status'] == 'dispatched'
    assert task['phase'] == 'capture' and not delivery.phone.calls and delivery.frame.read_bytes() == original
