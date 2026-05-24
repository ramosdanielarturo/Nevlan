
import time
import threading
from app.services.missions import recorder
from app.contracts.mission import Step, EventType, KeyboardAction, AnnotationType

def test_recorder_annotations():
    print("🎥 Probando MissionRecorder con Anotaciones Pendientes...")
    
    # 1. Iniciar grabación
    mission = recorder.start_recording("Test Annotation Mission")
    rec = recorder.get_recorder()
    
    print("   Grabación iniciada.")
    
    # 2. Agregar anotación (sin step target) -> Debería ser pendiente
    rec.add_annotation("variable", "mi_variable", target_step_id=None)
    print("   Anotación agregada (debe estar pendiente).")
    
    # Verificar que está pendiente (acceso privado para test)
    if len(rec._pending_annotations) == 1:
        print("   ✅ Anotación pendiente verificada interna.")
    else:
        print("   ❌ Error: La anotación no se guardó como pendiente.")

    # 3. Simular evento (Mouse Click o Key Press)
    # Hack: llamamos directo a _on_event para no necesitar listeners reales ni mover mouse
    step = Step(
        event_type=EventType.KEYBOARD_TYPE_TEXT,
        keyboard_action=KeyboardAction(keys=["t", "e", "s", "t"])
    )
    rec._on_event(step)
    print("   Evento simulado (Keyboard Type).")

    # 4. Detener grabación
    mission = recorder.stop_recording()
    print(f"   Grabación detenida. Pasos: {len(mission.steps)}")
    
    # 5. Verificar que la anotación se pegó al paso
    if not mission.steps:
        print("   ❌ Error: No se grabaron pasos.")
        return

    recorded_step = mission.steps[0]
    annotations = mission.annotations
    
    if not annotations:
        print("   ❌ Error: No hay anotaciones en la misión.")
    else:
        ann = annotations[0]
        if ann.target_step_id == recorded_step.id:
            print(f"   ✅ ÉXITO: La anotación '{ann.value}' se vinculó al paso {recorded_step.id}")
        else:
            print(f"   ❌ Error: La anotación apunta a {ann.target_step_id}, pero el paso es {recorded_step.id}")

if __name__ == "__main__":
    test_recorder_annotations()
