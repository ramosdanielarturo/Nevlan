from app.contracts.mission import CompiledStep, ActionStrategy, TargetContext
from app.services.missions.compiler import (
    _group_pattern_ctrl_l_search,
    _group_pattern_form_click_type_tab,
)


def test_ctrl_l_bar_pattern_detected():
    cs = CompiledStep(
        action_strategy=ActionStrategy.SEND_HOTKEY,
        action_payload={"combo": True, "keys": ["ctrl", "l"]},
    )
    sid_to = {cs.id: cs}
    assert _group_pattern_ctrl_l_search(sid_to, [cs.id]) is True


def test_form_click_type_tab_pattern_heuristic():
    a = CompiledStep(
        action_strategy=ActionStrategy.CLICK,
        target_context=TargetContext(
            uia_data={"control_type": "EditControl", "name": "User"},
        ),
    )
    b = CompiledStep(
        action_strategy=ActionStrategy.SET_FIELD_VALUE,
        action_payload={"text": "x"},
        target_context=TargetContext(),
    )
    tab = CompiledStep(
        action_strategy=ActionStrategy.SEND_HOTKEY,
        action_payload={"keys": ["tab"]},
    )
    seq = [a, b, tab]
    sid_to = {x.id: x for x in seq}
    assert _group_pattern_form_click_type_tab(sid_to, [x.id for x in seq]) is True
