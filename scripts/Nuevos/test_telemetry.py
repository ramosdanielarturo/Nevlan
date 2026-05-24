
import time
from app.contracts.tool_result import ToolResult
from app.skills.registry import registry
from app.services.telemetry.metrics import telemetry

# Definimos tools dummy para probar el wrapper del registro
@registry.register(name="dummy.success")
def dummy_success(call_id: str) -> ToolResult:
    time.sleep(0.1)
    return ToolResult(call_id=call_id, result="OK")

@registry.register(name="dummy.fail")
def dummy_fail(call_id: str) -> ToolResult:
    time.sleep(0.1)
    raise ValueError("Simulated Failure")

def test_telemetry():
    print("📊 Probando Telemetría...")
    
    # 1. Ejecutar éxito
    print("   Ejecutando dummy.success...")
    # Recuperamos la función wrappeada desde el registro interno
    # Nota: registry._tools["dummy_success"].fn es la función DECORADA (wrapper)
    # Por diseño del registry.register, lo que se guarda en _tools es la función original? 
    # NO, el decorador devuelve el wrapper.
    # Pero registry.register guarda ToolDef(..., fn=fn).
    # Si fn es la función decorada, entonces al llamarla se ejecuta el wrapper.
    
    # Vamos a simular la ejecución via registry.execute para ser realistas
    from app.contracts.tool_call import ToolCall
    
    call1 = ToolCall(id="call_1", tool_name="dummy.success", arguments={})
    registry.execute(call1)
    
    # 2. Ejecutar fallo
    print("   Ejecutando dummy.fail...")
    call2 = ToolCall(id="call_2", tool_name="dummy.fail", arguments={})
    registry.execute(call2)

    # 3. Verificar Telemetría
    health = telemetry.get_system_health()
    print("\n📈 Reporte de Salud:")
    print(health)
    
    # Asserts básicos
    stats_success = telemetry.get_tool_stats("dummy_success")
    stats_fail = telemetry.get_tool_stats("dummy_fail")
    
    if stats_success and stats_fail:
        print(f"\n✅ Stats Success: {stats_success}")
        print(f"✅ Stats Fail: {stats_fail}")
        
        if stats_success['calls'] == 1 and stats_fail['calls'] == 1:
            print("✅ Conteo de llamadas correcto.")
        else:
            print("❌ Error en conteo de llamadas.")
            
        if "100.0%" in stats_success['success_rate'] and "0.0%" in stats_fail['success_rate']:
             print("✅ Tasas de éxito correctas.")
        else:
             print("❌ Error en tasas de éxito.")
    else:
        print("❌ No se encontraron stats para las tools dummy.")

if __name__ == "__main__":
    test_telemetry()
