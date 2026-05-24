import time
from app.contracts.mission import Mission, Step, EventType, KeyboardAction, Annotation, AnnotationType
from app.services.missions.player import MissionPlayer, replay_mission

def test_playbook_variable():
    print("🎭 Probando Playbood Variable Substitution...")
    
    # 1. Crear Misión Mock
    mission = Mission(name="Test Variable Playbook")
    
    # Paso 1: Escribir texto placeholder
    step1 = Step(
        event_type=EventType.KEYBOARD_TYPE_TEXT,
        keyboard_action=KeyboardAction(keys=["h", "o", "l", "a"])
    )
    mission.steps.append(step1)
    
    # Anotación: ese paso es una variable "mensaje"
    ann = Annotation(
        annotation_type=AnnotationType.VARIABLE,
        value="mensaje",
        target_step_id=step1.id
    )
    mission.annotations.append(ann)
    
    # 2. Ejecutar con variable
    print("▶️  Ejecutando con variable mensaje='Mundo Real'")
    
    # Mockeamos pyautogui para no escribir de verdad, o confiamos en que escribirá en la consola si tiene foco
    # Para este test unitario, lo ideal sería mockear pyautogui, pero 
    # vmaos a confiar en el log output que ya implementamos en player.py
    
    player = MissionPlayer(mission)
    player.start_execution()
    
    # Injectamos un mock de pyautogui en el modulo player para verificar la llamada
    import app.services.missions.player as player_module
    
    class MockPyAutoGUI:
        def typewrite(self, text, interval=0.0):
            print(f"   [MOCK] typewrite('{text}')")
            if text == "Mundo Real":
                print("   ✅ VERIFICADO: Se sustituyó la variable correctamente.")
            else:
                print(f"   ❌ ERROR: Se escribió '{text}' en vez de 'Mundo Real'")
                
        def click(self, x, y): pass
        def rightClick(self, x, y): pass
        def doubleClick(self, x, y): pass
        def scroll(self, x, y): pass
        def press(self, key): pass

    # Monkey patch
    original_pyautogui = None
    try:
        import pyautogui
        original_pyautogui = pyautogui
    except ImportError:
        pass
        
    player_module.pyautogui = MockPyAutoGUI()
    
    try:
        player.replay(variables={"mensaje": "Mundo Real"})
    finally:
        # Restore (aunque el script muere aqui)
        if original_pyautogui:
            player_module.pyautogui = original_pyautogui

if __name__ == "__main__":
    test_playbook_variable()
