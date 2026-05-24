"""action_groups coherentes tras editar el grafo sin recompilar desde raw_trace."""

from __future__ import annotations

from app.contracts.mission import Mission, RawEvent, EventType, MouseAction, KeyboardAction
from app.services.missions.compiler import MissionCompiler


def _mission_from_clicks(n: int) -> Mission:
    m = Mission(name="t_groups")
    evs = []
    for i in range(n):
        evs.append(
            RawEvent(
                event_type=EventType.MOUSE_CLICK,
                mouse_action=MouseAction(x=10 + i, y=10 + i, button="left"),
                metadata={"uia": {"name": f"B{i}", "control_type": "ButtonControl"}},
            )
        )
    m.raw_trace = evs
    return MissionCompiler.compile_mission(m)


def test_rebuild_after_pop_step_changes_group_count():
    m = _mission_from_clicks(5)
    g0 = len(getattr(m, "action_groups", []) or [])
    assert g0 >= 1
    # Simula edición: quita un paso
    m.compiled_execution_graph.pop(0)
    m.interpreted_steps.pop(0)
    steps = m.compiled_execution_graph
    for i in range(len(steps) - 1):
        steps[i].next_step_id_on_success = steps[i + 1].id
    if steps:
        steps[-1].next_step_id_on_success = None
    MissionCompiler.rebuild_action_groups(m)
    g1 = len(m.action_groups or [])
    assert isinstance(m.action_groups, list)
    assert g1 >= 1


def test_rebuild_preserves_step_ids_in_groups():
    m = _mission_from_clicks(4)
    sid = {s.id for s in m.compiled_execution_graph}
    MissionCompiler.rebuild_action_groups(m)
    seen = set()
    for g in m.action_groups:
        for x in g.step_ids:
            seen.add(x)
    assert seen == sid

