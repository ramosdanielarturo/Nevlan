# 🌐 ArthurOS - Integración con Chrome

## ¿Qué cambió?

ArthurOS ahora **preserva tu sesión de Chrome** en lugar de cerrarla y abrir una nueva ventana "test".

### Antes ❌
- ArthurOS cerraba tu Chrome actual
- Abría un nuevo Chrome sin tus extensiones ni sesiones
- Perdías todo tu trabajo

### Ahora ✅
- ArthurOS se conecta a tu Chrome existente
- Mantiene tus extensiones, sesiones y pestañas
- No interrumpe tu trabajo

## 🚀 Cómo usar

### Opción 1: Script automático (Recomendado)
1. Haz doble clic en `launch_chrome_debug.bat`
2. El script cerrará Chrome y lo reabrirá con debugging habilitado
3. ¡Listo! Ahora puedes usar ArthurOS

### Opción 2: Manual
1. Cierra Chrome completamente
2. Abre CMD (Win+R → cmd)
3. Ejecuta:
   ```cmd
   "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222
   ```
4. ¡Listo! Ahora puedes usar ArthurOS

## 🔍 Verificar que funciona

1. Ejecuta `python test_chrome_profile.py`
2. Verifica que:
   - Chrome tiene tus extensiones
   - Estás logueado en tus cuentas
   - El texto "test" aparece en el buscador de Google (esto es solo para la prueba)

## 💡 Notas importantes

- **Solo necesitas abrir Chrome con debugging UNA VEZ** al inicio del día
- ArthurOS se conectará automáticamente a ese Chrome
- Si cierras Chrome, necesitas volver a abrirlo con debugging
- El puerto 9222 debe estar libre (no uses otro programa que lo ocupe)

## 🐛 Solución de problemas

### "Chrome no tiene debugging habilitado"
- Cierra Chrome completamente
- Usa `launch_chrome_debug.bat` o el comando manual

### "Puerto 9222 ocupado"
- Cierra todas las instancias de Chrome
- Verifica que no haya procesos `chrome.exe` en el Administrador de Tareas
- Vuelve a intentar

### "No se puede conectar"
- Verifica que Chrome esté abierto con el flag `--remote-debugging-port=9222`
- Revisa los logs en `var/logs/arthur.log`
