"""Read-only Agent feedback from the request's frozen scene. Never calls a device or store."""
from __future__ import annotations

from .goals import BEFORE_SEND, SESSION_STEP, bind_roles, role_matches, validate_perception
from .handoff import scene_candidates
from pathlib import Path

from .rules import apply_layout, locate


class OfflineRejection(ValueError):
    """Candidate failed on recorded evidence before any dispatch or checkpoint write."""


def candidate_plan(request, observation, candidate, role):
    from .supervisor import decision_plan
    inventory = {key: getattr(observation, key) for key in ('package', 'logical_size', 'physical_size', 'nodes', 'texts')}
    targets, candidates, _ = scene_candidates(request, inventory, limit=48)
    prompt = {**request, 'candidates': candidates, 'uncertain': bool(request.get('uncertain_send') or request.get('uncertain_action'))}
    decision = {'decision': 'perception' if role else 'tap', 'candidate': candidate, 'role': role,
                'direction': None, 'distance': None, 'reason': '原始接管现场中的候选'}
    return decision_plan(decision, {'targets': targets, 'prompt': prompt})


def _screen_check(request, scene):
    from . import capture
    from .platforms import douyin_direct as direct
    step = request['step']
    task = {'entry': request.get('entry'), 'text': request.get('question') or '', 'index': request.get('question_index')}
    if scene.package != request.get('package', scene.package):
        return False, ['前台应用与原请求不一致'], {}
    if step in {SESSION_STEP, BEFORE_SEND} and task['entry'] == direct.ID:
        if step == BEFORE_SEND:
            baseline = (Path(request['run_directory']) / '调试产物' / direct.ID / f"{task['index']:03d}" / 'new-session'
                        if request.get('run_directory') and isinstance(task['index'], int) else None)
            _, fault = direct.draft_session_verdict(scene, task['text'], baseline)
            missing = [fault] if fault else []
            field = (direct.chat_state(scene) or {}).get('input') or {}
            if not field.get('focused') or field.get('text') != task['text'] or not direct.send_target(scene):
                missing.append('发送前需要完整原题、输入焦点及唯一发送控件')
        else:
            missing = direct.session_missing(scene, task['text'])
        return not missing, missing, {}
    if step in {'products.images_wait', 'layout.product_images'}:
        drawer = capture.drawer_state(scene)
        if not drawer or not drawer['expanded']:
            return False, ['缺少展开的药品列表及可复验视口'], {}
        evidence = capture.product_image_evidence(scene, drawer['viewport'])
        missing = []
        if evidence['loading_indicator']:
            missing.append('列表中仍有加载或重试提示')
        if not evidence['regions']:
            missing.append('没有识别出完整药图区域')
        if evidence['unready_regions']:
            missing.append('列出的药图区域未通过原图像素就绪检查')
        return evidence['ready'], missing, {'image_evidence': evidence}
    # Report only checks which the collector can actually make from ONE frame.
    # A valid role is often useful for capture.review, but seams/end require resume.
    if step == 'layout.answer_container':
        try:
            ok = bool(capture.answer_viewport(scene))
        except capture.CaptureError:
            ok = False
    elif step == 'layout.product_list':
        ok = bool(capture.drawer_state(scene))
    elif step == 'capture.answer_page':
        ok = capture.answer_page_ready(scene)
    elif step == 'citation.expand':
        try:
            ok = bool(capture.citation_state(scene, capture.answer_viewport(scene))['expanded'])
        except capture.CaptureError:
            ok = False
    else:
        return None, request.get('missing_evidence', []), {}
    return ok, [] if ok else request.get('objective', {}).get('required_evidence') or [request.get('goal') or step], {}


def preview_plan(request, observation, plan=None, *, point_override=None):
    from .recovery import check_exploration, validate_repair_plan
    allowed = request.get('allowed_resolutions', ['resume'])
    result = {'status': 'preview', 'request_id': request.get('request_id'), 'sha256': request.get('sha256'),
              'step': request['step'], 'scope': 'saved_frame', 'allowed_resolutions': allowed,
              'roles': {}, 'scene_check': 'requires_resume', 'missing_evidence': []}
    scene = observation
    try:
        if plan is not None:
            if isinstance(plan, dict) and 'perception' in plan:
                for role, target in validate_perception(plan)['perception'].items():
                    boxes = {tuple(n['bounds']) for n in role_matches(target, observation, observation.nodes)}
                    result['roles'][role] = {'matches': len(boxes), 'unique': len(boxes) == 1}
            validate_repair_plan(request, observation, plan, skip_locate=point_override is not None)
            if 'perception' in plan:
                scene = bind_roles(plan, observation, require=True)
            elif 'layout' in plan:
                if request['step'] != 'layout.' + plan['layout']['role']:
                    raise ValueError('布局角色与当前请求不符')
                scene = apply_layout(plan, observation)
            else:
                action = plan['actions'][0]
                if not request['step'].endswith('.send'):
                    point = (point_override if point_override is not None
                             else locate(action['target'], scene, Path(request['rule_assets'])) if 'target' in action else None)
                    state = {**request, 'products_resume': request.get('checkpoint', {}).get('products_phase')}
                    check_exploration(action, point, scene, state)
                if point_override is not None:
                    result['inferred_target'] = action.get('target')
                result.update(next='submit_then_resume', missing_evidence=['首步定位通过。动作效果需要恢复后核验'])
                return result
        ok, missing, details = _screen_check(request, scene)
        result.update(scene_check={True: 'passed', False: 'failed', None: 'requires_resume'}[ok],
                      missing_evidence=missing, **details)
        if ok is False and plan and 'perception' in plan:
            result['next'] = 'revise_or_inspect'
        elif plan:
            result['next'] = 'submit_then_resume'
        elif ok is False and set(allowed) != {'resume'}:
            result['next'] = 'inspect_or_preview_candidate'
        else:
            result['next'] = 'resume'
    except (ValueError, RuntimeError) as error:
        result.update(scene_check='rejected', missing_evidence=[str(error)],
                      next='resume' if allowed == ['resume'] else 'revise_or_inspect')
    return result
