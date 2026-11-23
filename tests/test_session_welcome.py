"""Synthetic UI evidence only. Real device frames and learned selectors stay local."""
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import ImageDraw

from geo_collector.goals import BEFORE_SEND, SESSION_STEP, bind_roles, repair_context
from geo_collector.images import _image, _png
from geo_collector.platforms import douyin_direct as direct
from geo_collector.recovery import NeedsRepair, StepEngine, load_observation, save_observation
from geo_collector.rules import RuleStore
from test_douyin_direct import DirectPhone
from test_workflow import state


PLAN = {'perception': {'session_welcome': {'kind': 'ocr', 'pattern': '^示例问题$'}}}


def welcome_scene(scale=1, draft=''):
    phone = DirectPhone(scale=scale, stage='clean')
    phone.draft, phone.focused = draft, bool(draft)
    o = phone.observe()
    im = _image(o.png).copy()
    draw = ImageDraw.Draw(im)
    def box(bounds):
        return tuple(round(v * scale) for v in bounds)
    draw.rectangle(box((0, 541, 720, 1370)), fill=(243, 243, 244))
    draw.rounded_rectangle(box((21, 651, 670, 1010)), radius=round(18*scale), fill='white')
    body = (0, 635, 720, 1025)
    nodes = [*o.nodes, {**o.nodes[0], 'bounds': body, 'class_name': 'android.view.ViewGroup'}]
    lines = [('AI医生', (84, 578, 185, 610)),
             ('您好，我是小荷健康推出的AI健康咨询助手，', (45, 680, 650, 713)),
             ('可以为您提供全天24小时的健康帮助，和我对话吧！', (45, 738, 655, 773)),
             ('示例问题', (45, 805, 160, 837)),
             ('·示例一', (45, 875, 194, 909)),
             ('·示例二', (45, 946, 193, 980))]
    texts = list(o.texts)
    for label, bounds in lines:
        l, t, r, b = bounds
        # Render visible ink on the same line as the synthetic OCR.
        draw.rectangle(box((l+2, t+3, r-2, b-4)), fill=(68, 68, 70))
        texts.append({'text': label, 'bounds': box(bounds), 'confidence': .99})
    return replace(o, nodes=nodes, texts=texts, png=_png(im))


@pytest.mark.parametrize('scale', [1, 1.5])
def test_welcome_requires_bound_heading_complete_evidence_and_offline_replay(tmp_path, scale):
    raw = welcome_scene(scale)
    assert not direct.clean_session(raw)
    annotated = bind_roles(PLAN, raw, require=True)
    assert direct.session_evidence(annotated) == 'welcome'
    save_observation(tmp_path, annotated)
    assert direct.session_evidence(load_observation(tmp_path)) == 'welcome'
    drafted = bind_roles(PLAN, welcome_scene(scale, '本题'))
    assert direct.clean_session(drafted, '本题') and not direct.clean_session(drafted, '另一题')


@pytest.mark.parametrize('bad', ['user', 'unread_user', 'other_answer', 'no_card', 'clipped', 'outside_ink',
                                 'extra_ink', 'no_ink', 'second_card', 'wrong_heading', 'wrong_app', 'wrong_divider'])
def test_role_cannot_override_conflicting_evidence(bad):
    raw = welcome_scene()
    plan = PLAN
    if bad in {'user', 'unread_user', 'outside_ink', 'extra_ink', 'no_ink', 'second_card'}:
        im = _image(raw.png).copy()
        draw = ImageDraw.Draw(im)
        if bad in {'user', 'unread_user'}:
            draw.rectangle((500, 1120, 680, 1180), fill=(40, 200, 160))
            if bad == 'user':
                raw.texts.append({'text': '已提过的问题', 'bounds': (515, 1130, 665, 1170), 'confidence': .98})
        elif bad == 'no_ink':
            draw.rectangle((40, 800, 175, 845), fill='white')
        else:
            position = (430, 1080, 620, 1130) if bad != 'extra_ink' else (460, 840, 600, 861)
            draw.rectangle(position, fill='white' if bad == 'second_card' else 'black')
        raw.png = _png(im)
    elif bad == 'other_answer':
        raw.texts[-5]['text'] = '你好，我是小荷。接下来是上一题已经生成的回答。'
    elif bad == 'no_card':
        raw.nodes.pop()
    elif bad == 'clipped':
        raw.nodes[-1]['bounds'] = (0, 635, 720, 1400)
    elif bad == 'wrong_heading':
        plan = {'perception': {'session_welcome': {'kind': 'ocr', 'pattern': 'AI医生$','region': [0, .3, .4, .5]}}}
    elif bad == 'wrong_app':
        raw.package = 'other.app'
    elif bad == 'wrong_divider':
        raw.texts = [line for line in raw.texts if line['text'] != '已开始新会话']
    assert not direct.clean_session(bind_roles(plan, raw))


def test_welcome_promoted_only_if_used_for_completed_direct_submission(tmp_path):
    store = RuleStore(tmp_path / 'rules')
    engine = StepEngine(None, store, tmp_path / 'run', {'entry': direct.ID})
    scene = welcome_scene()
    key = 'perception.session_welcome'
    def candidate():
        engine.state['flow_candidates'] = {key: {'recipe': PLAN, 'context': engine._context(scene)}}
        assert direct.clean_session(engine.decorate(scene))
        assert not store.get(direct.PACKAGE, key)
    candidate()
    engine.commit_flows({'direct_submission': {'session_evidence': 'blank'}})
    candidate()
    engine.commit_flows({'direct_submission': {'session_evidence': 'welcome'}})
    fresh = StepEngine(None, store, tmp_path / 'fresh', {'entry': direct.ID})
    assert direct.clean_session(fresh.decorate(scene)) and not fresh.state.get('agent_requests')
    fresh.state['entry'] = 'douyin'
    assert not direct.clean_session(fresh.decorate(scene))


def test_send_rechecks_original_session_before_dispatch(tmp_path):
    phone = DirectPhone(stage='answer')
    phone.question, phone.draft, phone.focused = '另一个用户问题', '本题', True
    s = state(direct.ID, question='本题', phase='input')
    task = s['tasks'][0]
    task['direct_open'] = {'stage': 'ready', 'session_confirmed': True, 'confirmation_dispatched': True}
    s.update(entry=direct.ID, index=task['index'])
    engine = StepEngine(phone, RuleStore(tmp_path / 'rules'), tmp_path, s)
    with pytest.raises(NeedsRepair):
        direct.submit(engine, task)
    import json
    request = json.loads(Path(s['pending_request']).read_text())
    assert request['step'] == BEFORE_SEND and request['readonly']
    assert request['allowed_resolutions'] == ['resume', 'repair.perception']
    assert task['phase'] == 'input' and not s.get('inflight')
    assert not any(c[0] in ('input', 'send') for c in phone.calls)
    details = repair_context(s, SESSION_STEP, '缺少证据')
    assert details['objective']['required_evidence'] and details['checkpoint']['confirmation_dispatched']
    assert details['checkpoint']['question_dispatch_pending']


def test_return_from_drawer_resolves_outstanding_back_before_opening_chat(tmp_path):
    from geo_collector.storage import write_json
    phone = DirectPhone(stage='home')
    run = state(direct.ID, phase='open')
    task = run['tasks'][0]
    step = direct.ID + '.close_products'
    file = tmp_path / 'request.json'
    write_json(file, {'step': step, 'entry': direct.ID, 'question_index': 1, 'status': 'pending'})
    run.update(entry=direct.ID, index=1, pending_request=str(file), inflight={'step': step, 'risk': 'tap'})
    task['direct_open'] = {'stage': 'session_request'}
    engine = StepEngine(phone, RuleStore(tmp_path / 'rules'), tmp_path, run)
    direct.open(engine, task)
    assert task['direct_open']['stage'] == 'ready' and not run.get('inflight')
    assert not run.get('pending_request') and phone.calls.count(('new_session',)) == 1
    assert not any(call[0] in ('back', 'send', 'name_search') for call in phone.calls)


def test_compact_welcome_decision_survives_keyboard_reflow():
    from geo_collector.supervisor import decision_plan
    target = {'kind': 'ocr', 'pattern': '^示例问题$', 'region': [.05, .7, .5, .75]}
    context = {'prompt': {'step': SESSION_STEP, 'allowed_resolutions': ['repair.perception']}, 'targets': {'o1': target}}
    decision = dict(decision='perception', candidate='o1', role='session_welcome', direction=None, distance=None, reason='示例标题')
    assert decision_plan(decision, context) == PLAN
    context['prompt']['step'] = 'capture.review'
    with pytest.raises(ValueError):
        decision_plan(decision, context)
