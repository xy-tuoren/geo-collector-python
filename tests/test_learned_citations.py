"""Learning a stable citation heading must not hide independent OCR evidence."""
from geo_collector.capture import answer_viewport, citation_state
import pytest

from test_capture import FakeDevice


def reference_page(scale=1):
    observation = FakeDevice(scale).observe()
    labels = [("参考5篇医学文献", (24, 230, 270, 250)),
              ("1.中西医结合防治小儿腹泻专家共识", (24, 257, 330, 275)),
              ("2.感染性腹泻诊治质量改进专家共识", (24, 282, 330, 300)),
              ("3.诺如病毒胃肠炎诊疗方案(2025年版)", (24, 310, 330, 330)),
              ("4.世界胃肠病学组织全球指南", (24, 340, 330, 360)),
              ("5.儿童急性腹泻药物经济学评价", (24, 370, 330, 390))]
    observation.texts = [{"text": text, "bounds": tuple(v * scale for v in box), "confidence": .99} for text, box in labels]
    observation.nodes.append({"text": labels[0][0], "bounds": labels[0][1], "package": observation.package,
                              "class_name": "collector.OCR", "collector_evidence": "ocr", "collector_role": "citations_header"})
    return observation


@pytest.mark.parametrize("scale", [1, 2])
def test_learned_heading_accepts_original_numbered_bibliography_without_row_roles(scale):
    observation = reference_page(scale)
    state = citation_state(observation, answer_viewport(observation), "参考5篇医学文献")
    assert state["present"] and state["expanded"]
    assert not any(n.get("collector_role") == "citations_rows" for n in observation.nodes)


@pytest.mark.parametrize("problem", ["heading_only", "ordinary_numbered_body", "detached_rows", "two_preview_rows", "wrong_heading"])
def test_learned_heading_alone_cannot_prove_a_reference_panel_expanded(problem):
    observation = reference_page()
    expected = "参考5篇医学文献"
    if problem == "heading_only":
        observation.texts = observation.texts[:1]
    elif problem == "ordinary_numbered_body":
        for row in observation.texts[1:]:
            row["text"] = row["text"][0] + ".这是一条普通回答建议"
    elif problem == "detached_rows":
        for row in observation.texts[1:]:
            box = row["bounds"]
            row["bounds"] = (box[0], box[1] + 200, box[2], box[3] + 200)
    elif problem == "two_preview_rows":
        observation.texts = observation.texts[:3] + [{"text": "轻度脱水的正文建议", "bounds": (24, 350, 330, 370), "confidence": .99}]
    else:
        expected = "参考3篇医学文献"
    state = citation_state(observation, answer_viewport(observation), expected)
    assert state["present"] and not state["expanded"]
