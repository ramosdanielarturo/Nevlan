def test_overlay_deprecated_module_reexports_live_overlay():
    from app.interfaces.desktop.overlay import recording_overlay as legacy

    assert legacy.ActionRecorderOverlay is not None
    assert legacy.RecordingOverlay is legacy.ActionRecorderOverlay


def test_canonical_overlay_import():
    from app.interfaces.desktop.recording_overlay import ActionRecorderOverlay

    assert ActionRecorderOverlay.__name__ == "ActionRecorderOverlay"
