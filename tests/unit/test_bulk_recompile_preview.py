from app.contracts.mission import (
    Mission, MissionStatus, RawEvent, EventType,
    MouseAction, KeyboardAction,
)


def _click_edit() -> RawEvent:
    return RawEvent(
        event_type=EventType.MOUSE_CLICK,
        mouse_action=MouseAction(x=100, y=200, button="left"),
        metadata={
            "uia": {
                "name": "Email",
                "automation_id": "email",
                "control_type": "EditControl",
            }
        },
    )


def _key(*keys: str) -> RawEvent:
    return RawEvent(
        event_type=EventType.KEYBOARD_KEY_PRESS,
        keyboard_action=KeyboardAction(keys=list(keys)),
    )


def _trace_click_type_tab():
    return [
        _click_edit(),
        _key("a"),
        _key("Key.tab"),
    ]


def test_preview_returns_compiled_mission_in_memory():
    from app.services.missions.bulk_recompile import preview_recompile_candidates

    m = Mission(name="bulk", status=MissionStatus.DRAFT)
    m.raw_trace = _trace_click_type_tab()
    rows = preview_recompile_candidates([m])
    assert len(rows) == 1
    assert rows[0].ok is True
    assert rows[0].compiled_mission is not None
    assert rows[0].steps_after >= 1


def test_empty_trace_preview_produces_empty_graph_but_no_raise():
    """``raw_trace`` vacío ⇒ ``recompile_mission`` no rompe — fila válida pero 0 pasos."""
    from app.services.missions.bulk_recompile import preview_recompile_candidates

    good = Mission(name="g", status=MissionStatus.DRAFT)
    good.raw_trace = _trace_click_type_tab()

    empty = Mission(name="b", status=MissionStatus.APPROVED)
    empty.raw_trace = []

    rows = preview_recompile_candidates([good, empty])
    assert rows[0].ok is True
    assert rows[1].ok is True
    assert rows[1].steps_after == 0

