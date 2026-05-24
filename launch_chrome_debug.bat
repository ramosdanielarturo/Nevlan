@echo off
echo ========================================
echo  ArthurOS - Chrome con Debugging
echo ========================================
echo.
echo Cerrando Chrome...
taskkill /F /IM chrome.exe >nul 2>&1
timeout /t 2 /nobreak >nul
echo.
echo Abriendo Chrome con debugging habilitado...
echo Tu perfil, extensiones y sesiones se mantendran.
echo.
start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222
echo.
echo Chrome abierto con debugging en puerto 9222
echo Ahora puedes usar ArthurOS con tu Chrome!
echo.
pause
