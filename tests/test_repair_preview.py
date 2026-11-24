"""Frozen-scene trials exercise the same guards as resume without a device."""
from dataclasses import replace
from pathlib import Path

from PIL import ImageDraw
import pytest

from geo_collector import cli
from geo_collector.goals import SESSION_STEP, bind_roles
from geo_collector.handoff import handoff_summary
from geo_collector.images import _image, _png
from geo_collector.platforms import douyin_direct as direct
from geo_collector.recovery import NeedsRepair, StepEngine, submit_repair
from geo_collector.rules import RuleStore
from geo_collector.storage import read_json, write_json
from test_douyin_direct import DirectPhone
from test_session_welcome import welcome_scene


def reset_pair(draft=''):
    before = DirectPhone(stage='chat').observe()
    after = welcome_scene(draft=draft)
    # Unseen copy with an indented heading and list. No real-device data.
    names = ['即刻开始一段全新的咨询', '从下面任选一个主题开始', '初次见面的提问方向', '·示例甲', '·示例乙']
    for scene in (before, after):
        image = _image(scene.png).copy()
        draw = ImageDraw.Draw(image)
        scene.texts.append({'text': '上一轮内容至此结束', 'bounds': [40, 350, 430, 390], 'confidence': .98})
        draw.rectangle([44, 355, 425, 385], fill='#333333')
        if scene is after:
            draw.rectangle([270, 504, 450, 532], fill='#606060')  # Visible new divider.
            for row, name in zip(scene.texts[-6:-1], names):
                x, y, r, b = row['bounds']
                draw.rectangle((x, y, r, b), fill='white')
                box = (150, y, min(650, 150+r-x), b)
                draw.rectangle((box[0]+2, y+3, box[2]-2, b-4), fill='#444446')
                row.update(text=name, bounds=box)
        scene.png = _png(image)
    return before, after


def prepare(tmp_path):
    origin, scene = reset_pair()
    witness = direct.reset_welcome_witness(origin, scene)
    assert witness
    task = {'entry': direct.ID, 'index': 1, 'text': '本次提问', 'phase': 'open',
            'direct_open': {'confirmation_dispatched': True, 'stage': 'session_boundary', 'reset_welcome': witness}}
    state = {'entry': direct.ID, 'index': 1, 'tasks': [task], 'phase': 'open'}
    engine = StepEngine(None, RuleStore(tmp_path/'rules'), tmp_path/'batch', state)
    with pytest.raises(NeedsRepair) as exc:
        engine._request(SESSION_STEP, '核对新会话', engine.decorate(scene), (), '缺少证据')
    return engine, scene, Path(exc.value.request)


def test_agent_corrects_anchor_on_same_frozen_request_then_reuses_rule(tmp_path):
    engine, scene, file = prepare(tmp_path)
    handoff = handoff_summary(engine.run_dir)
    ids = {row.get('label'): row['id'] for row in handoff['candidates']}
    base = ['repair', str(file), '--sha256', handoff['sha256']]
    def preview(*args):
        return cli.execute(cli.parser().parse_args([*base, '--preview', *args]))
    def files():
        return {f: (f.stat().st_mtime_ns, f.read_bytes()) for f in tmp_path.rglob('*') if f.is_file()}
    originals = files()
    assert preview()['scene_check'] == 'failed'
    bad = preview('--candidate', ids['AI医生'], '--role', 'session_welcome')
    assert bad['roles']['session_welcome']['unique'] and bad['scene_check'] == 'failed'
    assert '标题' in bad['missing_evidence'][0] and bad['next'] == 'revise_or_inspect'
    choice = ['--candidate', ids['初次见面的提问方向'], '--role', 'session_welcome']
    good = preview(*choice)
    assert good['scene_check'] == 'passed' and good['scope'] == 'saved_frame' and files() == originals
    recipe = cli.execute(cli.parser().parse_args([*base, *choice]))['recipe']
    engine._accept_perception(recipe, engine.decorate(scene), file)
    assert not engine.store.get(direct.PACKAGE, 'perception.session_welcome')
    assert direct.clean_session(engine.decorate(scene))
    engine.commit_flows({'direct_submission': {'session_evidence': 'welcome'}})
    assert preview(*choice)['next'] == 'archived_request'
    with pytest.raises(ValueError, match='旧修复'):
        submit_repair(file, sha256=handoff['sha256'], plan=recipe)
    # A fresh, un-taught task has its own reset transition and reuses the saved selector.
    origin, second = reset_pair()
    fresh = StepEngine(None, engine.store, tmp_path/'fresh', {'index': 2, 'entry': direct.ID, 'phase': 'open', 'tasks': [
        {'index': 2, 'entry': direct.ID, 'direct_open': {'reset_welcome': direct.reset_welcome_witness(origin, second)}}]})
    assert direct.clean_session(fresh.decorate(second)) and fresh.state.get('agent_requests', 0) == 0


def test_role_cannot_authenticate_unrelated_copy_or_occlusion(tmp_path):
    engine, scene, file = prepare(tmp_path)
    plan = {'perception': {'session_welcome': {'kind': 'ocr', 'pattern': '^初次见面的提问方向$'}}}
    assert not direct.clean_session(bind_roles(plan, scene))  # No observed reset receipt.
    bound = bind_roles(plan, engine.decorate(scene))
    altered = [{**row, 'text': '另一次咨询的回答'} if row['text'] == '即刻开始一段全新的咨询' else row for row in bound.texts]
    assert not direct.clean_session(replace(bound, texts=altered))
    image = _image(bound.png).copy()
    ImageDraw.Draw(image).rectangle((410, 1150, 650, 1230), fill='black')
    assert not direct.clean_session(replace(bound, png=_png(image)))
    origin, confirmed = reset_pair()
    assert not direct.reset_welcome_witness(confirmed, confirmed)
    assert not direct.reset_welcome_witness(replace(origin, texts=[]), confirmed)
    assert not direct.reset_welcome_witness(replace(origin, package='wrong.app'), confirmed)
    image = _image(origin.png).copy()
    ImageDraw.Draw(image).rectangle((270, 504, 450, 532), fill='grey')
    assert not direct.reset_welcome_witness(replace(origin, png=_png(image)), confirmed)


def test_preview_uncertainty_and_match_counts(tmp_path):
    engine, _, file = prepare(tmp_path)
    request = read_json(file)
    def trial(pattern):
        return submit_repair(file, sha256=request['sha256'], preview=True,
            plan={'perception': {'session_welcome': {'kind': 'ocr', 'pattern': pattern}}})
    assert trial('不存在')['roles']['session_welcome'] == {'matches': 0, 'unique': False}
    ambiguous = trial('示例')
    assert ambiguous['roles']['session_welcome']['matches'] == 2 and ambiguous['scene_check'] == 'rejected'
    with pytest.raises(ValueError, match='SHA256'):
        submit_repair(file, sha256='old', preview=True)
    assert not (file.parent/'response.json').exists()
    write_json(file, {**request, 'uncertain_send': True, 'allowed_resolutions': ['resume']})
    restricted = trial('初次见面')
    assert restricted['scene_check'] == 'rejected' and restricted['next'] == 'resume'
    with pytest.raises(ValueError, match='只读'):
        submit_repair(file, sha256=request['sha256'], candidate='n1', role='session_welcome')


def test_live_role_drift_generates_a_new_actionable_request(tmp_path):
    machine, scene, first = prepare(tmp_path)
    spec = {'perception': {'session_welcome': {'kind': 'ocr', 'pattern': '^初次见面的提问方向$'}}}
    submit_repair(first, sha256=read_json(first)['sha256'], plan=spec)
    changed = replace(scene, texts=[r for r in scene.texts if '初次见面' not in r['text']])
    with pytest.raises(NeedsRepair) as error:
        machine._accept_perception(spec, changed, first)
    assert read_json(first)['status'] == 'rejected' and machine.state.get('pending_request') == error.value.request
    second = read_json(error.value.request)
    assert second['request_id'] != read_json(first)['request_id'] and second['step'] == SESSION_STEP
    assert '现场未找到页面角色' in second['reason'] and not machine.state.get('flow_candidates')


def test_ocr_annotation_is_idempotent_even_on_older_saved_duplicate():
    scene = welcome_scene()
    plan = {'perception': {'answer_page': {'kind': 'ocr', 'pattern': '^小荷AI医生$'}}}
    first = bind_roles(plan, scene)
    node = next(n for n in first.nodes if n.get('collector_role') == 'answer_page')
    duplicate = replace(first, nodes=[*first.nodes, dict(node)])
    again = bind_roles(plan, duplicate, require=True)
    assert [n for n in again.nodes if n.get('collector_role') == 'answer_page'] == [node]
    assert direct.chat_state(again)


def test_cli_teaches_reset_once_and_fresh_batch_completes_without_inference(tmp_path, monkeypatch):
    from contextlib import nullcontext
    from geo_collector import capture, device
    import json

    class WelcomePhone(DirectPhone):
        def observe(self, ocr=False):
            if self.stage in {'chat', 'clean'}:
                before, after = reset_pair(self.draft)
                scene = before if self.stage == 'chat' else after
                for node in scene.nodes:
                    if node.get('class_name', '').endswith('EditText'):
                        node['focused'] = self.focused
                return scene
            if self.stage == 'answer':
                self.marker = False
                self.old_same_question = True  # Render the sole live user bubble at the top of this short answer.
            return super().observe(ocr=ocr)
        def close(self):
            pass

    phone = WelcomePhone(stage='chat')
    monkeypatch.setattr(device, 'Device', lambda **kw: phone)
    monkeypatch.setattr(cli, 'DeviceLease', lambda *args: nullcontext())
    monkeypatch.setattr('geo_collector.recovery.time.sleep', lambda _: None)
    original = capture.AnswerCapture.__init__
    def init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self.quiet_seconds = 0
    monkeypatch.setattr(capture.AnswerCapture, '__init__', init)
    args = ['run', '--question', '完整测试问题', '--entry', direct.ID, '--rule-dir', str(tmp_path/'rules')]
    def execute(*argv):
        return cli.execute(cli.parser().parse_args(list(argv)))
    with pytest.raises(NeedsRepair) as stop:
        execute(*args, '--output', str(tmp_path/'first'))
    file = Path(stop.value.request)
    req = read_json(file)
    assert req['step'] == SESSION_STEP
    case = handoff_summary(req['run_directory'])
    candidate = next(c['id'] for c in case['candidates'] if c.get('label') == '初次见面的提问方向')
    trial = ['repair', str(file), '--sha256', req['sha256'], '--role', 'session_welcome', '--candidate', candidate]
    assert execute(*trial, '--preview')['scene_check'] == 'passed'
    execute(*trial)
    assert execute('resume', req['run_directory'])['complete']
    assert phone.calls.count(('send', '完整测试问题')) == 1
    phone = WelcomePhone(stage='chat')
    assert execute(*args, '--output', str(tmp_path/'second'))['complete']
    fresh = next((tmp_path/'second').glob('*/调试产物/run.json'))
    assert read_json(fresh)['agent_requests'] == 0 and phone.calls.count(('send', '完整测试问题')) == 1
    replay = [json.loads(s) for s in (fresh.parent/'events.jsonl').read_text().splitlines()]
    assert any(e['event'] == 'session_welcome_verified' and e['source'] == 'saved' for e in replay)


@pytest.mark.parametrize('unloaded,background_only', [(False, False), (True, False), (False, True)])
def test_photo_preview_identifies_a_placeholder_without_testing_clipped_carousel_text(unloaded, background_only):
    import xml.etree.ElementTree as ET
    from geo_collector.capture import product_image_evidence
    from test_capture import DrawerDevice
    phone = DrawerDevice(unloaded=unloaded)
    phone.drawer = phone.drawer_expanded = True
    scene = phone.observe()
    viewport = (0, 110, 360, 760)
    tree = ET.Element('hierarchy')
    def native(parent, name, bounds):
        l, t, r, b = bounds
        scene.nodes.append({'package': scene.package, 'class_name': 'android.widget.'+name, 'text': '', 'bounds': bounds})
        return ET.SubElement(parent, 'node', {'package': scene.package, 'class': 'android.widget.'+name,
                             'bounds': f'[{l},{t}][{r},{b}]', 'scrollable': 'false'})
    recycler = native(tree, 'RecyclerView', viewport)
    card = native(recycler, 'FrameLayout', (10, 620, 350, 760))
    native(tree if background_only else card, 'HorizontalScrollView', (10, 650, 350, 760))
    scene.xml = ET.tostring(tree, encoding='unicode')
    report = product_image_evidence(scene, viewport)
    # The compact-row fallback would invent this patch in the cropped card's text area.
    guessed = (20, 657, 85, 722)
    assert (guessed in report['regions']) == background_only
    if not background_only:
        assert bool(report['unready_regions']) == unloaded and report['ready'] != unloaded


@pytest.mark.parametrize('fault', [None, 'unbound', 'pixels', 'extra_label', 'draft', 'no_baseline'])
def test_shrunk_draft_requires_original_viewport_and_bound_divider(fault):
    phone = DirectPhone(stage='clean')
    baseline = phone.observe()
    phone.focused, phone.draft = True, '新的完整问题'
    current = phone.observe()
    plan = {'perception': {'session_divider': {'kind': 'ocr', 'pattern': '^已开始新会话$'}}}
    for scene in (current, baseline):
        next(r for r in scene.texts if r['text'] == '已开始新会话')['bounds'] = [260, 1100, 460, 1140]
        image = _image(scene.png).copy()
        ImageDraw.Draw(image).rectangle((267, 1105, 451, 1134), fill='#606060')
        scene.png = _png(image)
    next(n for n in current.nodes if n.get('scrollable'))['bounds'] = [0, 170, 720, 1167]
    if fault != 'unbound':
        current = bind_roles(plan, current, require=True)
    if fault == 'pixels':
        image = _image(current.png).copy()
        ImageDraw.Draw(image).rectangle((140, 800, 620, 885), fill='#080808')
        current.png = _png(image)
    if fault == 'extra_label':
        current.texts.append({'text': '另一条消息', 'bounds': [300, 1144, 460, 1163], 'confidence': .99})
    if fault == 'no_baseline':
        baseline = None
    kind, missing = direct.draft_session_verdict(current, '别的提问' if fault == 'draft' else phone.draft, baseline)
    assert (kind == 'aligned_divider') == (fault is None)
    assert bool(missing) == bool(fault)


def test_preview_point_validates_template_or_control_offline_without_writing_assets(tmp_path):
    phone = DirectPhone(stage='chat')
    phone.show_session_label = False
    scene = phone.observe()
    image = _image(scene.png).copy()
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((390, 92, 438, 132), radius=6, outline='black', width=3)
    draw.line((414, 100, 414, 124), fill='black', width=3)
    draw.line((402, 112, 426, 112), fill='black', width=3)
    scene.png = _png(image)

    state = {'entry': direct.ID, 'index': 1, 'tasks': [{'entry': direct.ID, 'index': 1, 'text': '测试题'}], 'phase': 'open'}
    engine = StepEngine(None, RuleStore(tmp_path / 'rules'), tmp_path / 'batch', state)
    with pytest.raises(NeedsRepair) as exc:
        engine._request(direct.ID + '.new_session', '点击新建会话', scene, (), '找不到新会话控件')
    req_file = Path(exc.value.request)
    req = read_json(req_file)

    def snapshot():
        return {f: (f.stat().st_mtime_ns, f.read_bytes()) for f in tmp_path.rglob('*') if f.is_file()}

    before_files = snapshot()
    ok = cli.execute(cli.parser().parse_args(['repair', str(req_file), '--sha256', req['sha256'], '--preview', '--point', '414', '112']))
    assert ok['next'] == 'submit_then_resume'
    assert ok['inferred_target']['kind'] == 'template'
    assert snapshot() == before_files, 'offline --preview --point must never write template files'

    # Tapping the "发送" button via --preview --point is rejected offline by check_exploration.
    bad = cli.execute(cli.parser().parse_args(['repair', str(req_file), '--sha256', req['sha256'], '--preview', '--point', '655', '1475']))
    assert bad['scene_check'] == 'rejected' and '发送' in bad['missing_evidence'][0]
    assert snapshot() == before_files
