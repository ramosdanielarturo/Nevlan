def test_vibe_edit_llm_requires_openai(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPEN_AI_API_KEY", raising=False)

    from app.contracts.mission import (
        Mission, MissionStatus, CompiledStep, InterpretedStep, ActionStrategy,
    )
    from app.services.missions.vibe_service import apply_vibe_edit

    m = Mission(name="z", status=MissionStatus.APPROVED)
    m.compiled_execution_graph = [
        CompiledStep(action_strategy=ActionStrategy.CLICK),
    ]
    m.interpreted_steps = [InterpretedStep(description="click")]

    r = apply_vibe_edit(m, 0, "zzz___long_unlikely_local_parse___zzz___force_llm______")
    assert r.get("ok") is False
    err = (r.get("error") or "")
    assert "OPENAI_API_KEY" in err


def test_vibe_kb_still_ok_without_llm(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPEN_AI_API_KEY", raising=False)

    from app.contracts.mission import (
        Mission, MissionStatus, CompiledStep, InterpretedStep, ActionStrategy,
    )
    from app.services.missions.vibe_service import apply_vibe_edit

    m = Mission(name="z", status=MissionStatus.APPROVED)
    m.compiled_execution_graph = [CompiledStep(action_strategy=ActionStrategy.CLICK)]
    m.interpreted_steps = [InterpretedStep(description="x")]

    r = apply_vibe_edit(m, 0, "Ctrl+S")
    assert r.get("ok") is True
