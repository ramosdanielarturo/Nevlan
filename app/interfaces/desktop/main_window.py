"""
Nevlan — Desktop GUI (The Pill)
-------------------------------------------
Le enseñas el proceso. Le dices cuándo. Nevlan lo hace.
- Soluciona: Mute button real.
- Soluciona: Comandos de sueño por texto.
- Soluciona: Prioridad de reflejos sobre LLM.
"""

import sys
import io
import os
import re
import time
import string
import random
import unicodedata
from difflib import SequenceMatcher
import speech_recognition as sr
import pyttsx3
from collections import deque
from pathlib import Path

from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QFrame, QVBoxLayout, 
                             QHBoxLayout, QLabel, QPushButton, QLineEdit, QTextEdit)
from PyQt6.QtCore import (Qt, pyqtSignal, pyqtSlot, QThread, QMutex, QWaitCondition,
                          QRect, QTimer, QObject, QThreadPool, QRunnable)
from PyQt6.QtGui import (QColor, QPainter, QPen, QLinearGradient)

# --- IMPORTS DE NUESTRA ARQUITECTURA ---
from app.core.config import settings
from app.core.logger import log
# Intentamos importar el bus, si no existe no pasa nada (fallback)
try:
    from app.runtime.bus import bus
    from app.contracts.events import SystemEvent
except ImportError:
    bus = None
from app.brain.agent import agent
from app.interfaces.desktop.agent_worker import AgentWorker

# Fix encoding para Windows consolas
try:
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
except Exception: pass

# --- IMPORT NUEVA UI (Fase 1: Live Narrator) ---
from app.interfaces.desktop.recording_overlay import ActionRecorderOverlay


# --- 1. ESTILOS Y DISEÑO ---
class NevlanDesign:
    BG_TOP = QColor(25, 25, 32, 250)
    BG_BTM = QColor(10, 10, 15, 255)
    BORDER = QColor(255, 255, 255, 35)
    STATE_COLORS = {
        "IDLE": QColor(255, 255, 255, 50),
        "LISTENING": QColor(0, 206, 201),
        "THINKING": QColor(162, 155, 254),
        "SPEAKING": QColor(85, 239, 196),
        "RISK": QColor(255, 118, 117),
        "SLEEP": QColor(40, 40, 40),
        "PAUSED": QColor(255, 140, 0),
        "ERROR": QColor(255, 0, 0)
    }

# --- 2. TRABAJADORES DE AUDIO ---

class STTTask(QRunnable):
    """Tarea asíncrona para enviar audio a Google sin bloquear el micrófono."""
    def __init__(self, recognizer, audio, callback, was_system, timestamp, listen_time):
        super().__init__()
        self.recognizer = recognizer
        self.audio = audio
        self.callback = callback
        self.was_system = was_system
        self.timestamp = timestamp
        self.listen_time = listen_time

    def run(self):
        start_stt = time.time()
        try:
            text = self.recognizer.recognize_google(self.audio, language="es-MX")
            stt_time = time.time() - start_stt
            if text:
                log.info(f"Escucha: {self.listen_time:.2f}s | Google: {stt_time:.2f}s | Texto: {text}")
                self.callback(text, self.was_system, self.timestamp, self.listen_time)
        except Exception as e:
            pass

class EventBridge(QObject):
    """Puente para recibir eventos del Bus (Background) y enviarlos a PyQt."""
    new_chat_msg = pyqtSignal(str, str) 
    status_update = pyqtSignal(str)     
    visual_state = pyqtSignal(str)      
    confirmation_req = pyqtSignal(str, str, str)
    mission_review_req = pyqtSignal(str)
    ui_action_req = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        if bus:
            bus.subscribe("agent.user_input", self._on_user)
            bus.subscribe("agent.think", self._on_think)
            bus.subscribe("agent.final", self._on_final)
            bus.subscribe("agent.tool_call", self._on_tool)
            bus.subscribe("agent.waiting_confirmation", self._on_confirm)
            bus.subscribe("agent.error", self._on_error)
            bus.subscribe("mission.review_requested", self._on_mission_review)
            bus.subscribe("ui.action", self._on_ui_action)
            bus.subscribe("mission.annotate", self._on_mission_annotate)
            bus.subscribe("mission.replay_start", self._on_replay_event)
            bus.subscribe("mission.step_start", self._on_replay_event)
            bus.subscribe("mission.step_ok", self._on_replay_event)
            bus.subscribe("mission.replay_complete", self._on_replay_event)

    def _on_replay_event(self, event):
        """Muestra progreso de ejecución en el chat."""
        name = event.name
        p = event.payload
        if name == "mission.replay_start":
            msg = f"🚀 Iniciando automatización: {p.get('mission_name', '?')}"
        elif name == "mission.step_start":
            idx = p.get('step_index', '?')
            total = p.get('total_steps', '?')
            strat = p.get('strategy', '?')
            msg = f"  ▶ Paso {int(idx)+1 if isinstance(idx,int) else idx}/{total}: {strat}"
        elif name == "mission.step_ok":
            idx = p.get('step_index', '?')
            msg = f"  ✅ Paso {int(idx)+1 if isinstance(idx,int) else idx} completado"
        elif name == "mission.replay_complete":
            status = p.get('status', '?')
            icon = "🎉" if status == "success" else "❌"
            msg = f"{icon} Automatización finalizada: {status}"
        else:
            return
        self.new_chat_msg.emit("tool", msg)

    def _on_mission_annotate(self, event):
        text = event.payload.get("text")
        if text:
            # Reutilizamos ui_action_req pasando un payload compuesto
            self.ui_action_req.emit(f"annotate|{text}")

    def _on_ui_action(self, event):
        cmd = event.payload.get("command")
        if cmd:
            self.ui_action_req.emit(cmd)

    def _on_user(self, event):
        self.visual_state.emit("THINKING")
        self.new_chat_msg.emit("user", str(event.payload.get("text", "")))

    def _on_think(self, event):
        loop = event.payload.get("loop", 0)
        self.status_update.emit(f"🧠 Pensando... ({loop})")

    def _on_final(self, event):
        self.visual_state.emit("IDLE")
        self.new_chat_msg.emit("assistant", str(event.payload.get("text", "")))
        self.status_update.emit("✅ Listo")

    def _on_tool(self, event):
        tool = event.payload.get("tool", "unknown")
        self.status_update.emit(f"🛠️ Usando {tool}...")
        
    def _on_confirm(self, event):
        self.visual_state.emit("RISK")
        p = event.payload
        self.confirmation_req.emit(p.get("token"), p.get("tool"), p.get("reason"))
        self.new_chat_msg.emit("system", f"⚠️ CONFIRMAR: {p.get('tool')}\nRazón: {p.get('reason')}")

    def _on_error(self, event):
        self.visual_state.emit("ERROR")
        self.new_chat_msg.emit("system", f"❌ Error: {event.payload.get('error')}")

    def _on_mission_review(self, event):
        m_id = event.payload.get("mission_id")
        log.info(f"EventBridge _on_mission_review received mission_id: {m_id}")
        if m_id:
            self.mission_review_req.emit(m_id)

class TTSWorker(QThread):
    """Motor de voz robusto v2 (Con Mute Inmediato)."""
    start_speak_signal = pyqtSignal()
    end_speak_signal = pyqtSignal()
    error_signal = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.queue = deque()
        self.condition = QWaitCondition()
        self.mutex = QMutex()
        self.running = True
        self.enabled = True 
        self.immediate_mute = False
        self.engine = None 


    def stop_speaking(self):
        """Ordena silencio inmediato."""
        self.mutex.lock()
        self.queue.clear()        # Borrar frases pendientes
        self.immediate_mute = True # Bandera roja para el motor
        
        # Intentar detener el motor actual si existe
        if self.current_engine:
            try: self.current_engine.stop()
            except: pass
            
        self.condition.wakeAll()
        self.mutex.unlock()

    @pyqtSlot(bool)
    def set_enabled(self, enabled):
        """Interruptor Maestro con seguridad de hilos."""
        self.mutex.lock()
        self.enabled = enabled
        if enabled:
            # IMPORTANTE: Reactivar inmediatamente bajo llave
            self.immediate_mute = False
        self.mutex.unlock()
        
        if not enabled:
            self.stop_speaking()
    def say(self, text):
        # Si está muteado, ignoramos la petición de hablar
        # Lectura sin bloqueo para retorno rápido, re-chequeo dentro del lock abajo
        if not self.enabled or not text: return
        
        self.mutex.lock()
        # Doble chequeo por si cambió mientras esperábamos el lock
        if self.enabled:
            # Aseguramos que no haya banderas de silencio residuales
            self.immediate_mute = False
            
            clean = text.replace("*", "").replace("#", "").replace("`", "")
            if len(clean) > 800: clean = clean[:800] + "..."
            self.queue.append(clean)
            self.condition.wakeOne()
        self.mutex.unlock() 

    def _on_word(self, name, location, length):
        """Chequeo de seguridad en cada palabra hablada."""
        # Si activaron el Mute a mitad de una frase, paramos el motor AQUÍ (hilo seguro)
        if self.immediate_mute and self.current_engine:
            try: self.current_engine.stop()
            except: pass
    def run(self):
        # No usamos _init_engine global
        while self.running:
            self.mutex.lock()
            # Esperar si no hay nada que decir
            while self.running and not self.queue:
                self.condition.wait(self.mutex)
            
            # Sacar texto solo si seguimos activos y sin mute
            text = self.queue.popleft() if self.queue else None
            should_speak = self.enabled and not self.immediate_mute
            self.mutex.unlock()

            if text and self.running and should_speak:
                self.signal_emitted = False
                try:
                    self.start_speak_signal.emit()
                    
                    # ONE-SHOT ENGINE: Instancia desechable
                    self.current_engine = pyttsx3.init()
                    self.current_engine.setProperty('rate', 160)
                    self.current_engine.connect('started-word', self._on_word)
                    
                    # CERO LATENCIA: Usamos evento para detectar el fin exacto
                    def on_finish(name, completed):
                        if not self.signal_emitted:
                            self.end_speak_signal.emit()
                            self.signal_emitted = True
                        
                    self.current_engine.connect('finished-utterance', on_finish)
                    
                    self.current_engine.say(text)
                    self.current_engine.runAndWait()
                    
                    # Limpieza explícita
                    try: self.current_engine.stop()
                    except: pass
                    del self.current_engine
                    self.current_engine = None

                except Exception as e:
                    pass
                finally:
                    self.current_engine = None
                    # RED DE SEGURIDAD: Si on_finish no disparó, emitimos aquí.
                    # Esto garantiza que Arthur NUNCA se quede sordo.
                    if not self.signal_emitted:
                        self.end_speak_signal.emit()
                        self.signal_emitted = True


class VoiceWorker(QThread):
    status_signal = pyqtSignal(str)
    visual_signal = pyqtSignal(str)
    speech_detected = pyqtSignal(str)
    request_tts = pyqtSignal(str)
    command_action = pyqtSignal(str, object)
    manual_input_signal = pyqtSignal(str) # Nueva señal para input manual seguro

    def __init__(self, parent_window=None):
        super().__init__()
        self.parent_window = parent_window  # Reference to ArthurPill for state flags
        
        # --- HARDENING: Anti-Echo Enhancements ---
        self.last_spoken_text = ""  # Track last TTS output for similarity detection
        self.tts_cooldown_until = 0.0  # Timestamp until which we ignore non-emergency audio
        self.running = True
        self.listening = False # MUTE DE ARRANQUE: Evita eco físico.
        self.sleep_mode = False
        self.r = sr.Recognizer()
        self.r.pause_threshold = 2.5 # SÚPER PACIENCIA: 2.5s para pensar
        self.r.non_speaking_duration = 0.5 
        self.r.energy_threshold = 250 # SENSIBILIDAD MÁXIMA PARA SUSURROS FINALES
        self.r.dynamic_energy_threshold = False 
        self.r.dynamic_energy_adjustment_damping = 0.1
        self.r.dynamic_energy_ratio = 1.5
        try:
            self.mic = sr.Microphone()
        except:
            self.mic = None
            log.error("No se detectó micrófono.")

        self._build_regex()
        self.files_context = deque()
        self.system_speaking = False
        self.speech_interfered = False # STICKY FLAG: Detecta si habló durante la escucha
        self.last_speech_time = 0.0
        
        # Conexión interna para procesar input manual en el hilo del worker
        self.manual_input_signal.connect(self.process_text, Qt.ConnectionType.QueuedConnection)

    @pyqtSlot()
    def start_system_speech(self):
        self.system_speaking = True
        self.speech_interfered = True 
        self.last_speech_time = time.time()
        
        # HARDENING: Audio cooldown (350ms firewall)
        self.tts_cooldown_until = time.time() + 0.35
        
        # Escudo de Habla: Máxima protección contra el eco de los altavoces
        self.r.energy_threshold = 2000 
        self.r.pause_threshold = 0.6
        self.r.non_speaking_duration = 0.2
        
    @pyqtSlot()
    def end_system_speech(self):
        self.system_speaking = False
        self.last_speech_time = time.time()
        
        # RESTAURAR SENSIBILIDAD MÁXIMA
        self.r.energy_threshold = 250
        self.r.pause_threshold = 2.5
        self.r.non_speaking_duration = 0.4
        
        # CRÍTICO: Reactivar el micrófono físicamente
        # Sin esto, Arthur deja de escuchar después de hablar
        if not self.sleep_mode:
            self.listening = True
            self.visual_signal.emit("LISTENING")
            self.status_signal.emit("👂 Escuchando")

    def _build_regex(self):
        # 1. Definimos el nombre del asistente (Disparador)
        # Aceptamos Nevlan, Arthur, Arturo, Art, Artur, Artu.
        names = r"(?:nevlan|arthur|arturo|art|artur|artu)"

        # 2. Listas de Palabras (Expandidas por el usuario)
        wake_words = [
            "despierta", "despertate", "despertar", "levanta", "levantate", "arriba", "amanece",
            "activa", "activate", "activar", "reactiva", "reactivate", "reanuda", "reanudate",
            "vuelve", "volver", "regresa", "regresá", "ven", "vente", "inicio", "inicia", "iniciar",
            "start", "on", "enciende", "encendete", "prende", "prendete", "hola", "hello", "hi",
            "oye", "oime", "hey", "escucha", "escuchame", "atiende", "atencion", "pon atencion",
            "te necesito", "estas ahi", "estas despierto", "manifiestate", "aparece", "asomate",
            "ready", "listo", "dale", "vamos", "work", "trabaja", "funciona", "online", "linea",
            "despierto", "activo", "habla", "hablame", "estas"
        ]

        sleep_words = [
            "descansa", "descansá", "duerme", "duermete", "dormite", "dormi", "pausa", "pausar",
            "pausate", "espera", "esperate", "aguarda", "aguanta", "standby", "stand-by", "stand by",
            "reposo", "reposa", "off", "apagate", "desconecta", "desconectate",
            "hiberna", "hibernar", "suspende", "suspendete", "stop", "detente", "quieto",
            "silencio", "callate", "mutear", "mute", "sordo", "inactivo", "desactiva", "desactivate",
            "dormir", "sueño", "siesta", "echate", "relajate", "relax", "calmate", "tranquilo",
            "espera un momento", "dame un tiempo", "quedate ahi"
        ]

        stop_words = [
            "callate", "calla", "silencio", "shh", "shhh", "basta", "stop", "para", "parale",
            "detente", "alto", "corta", "cortar", "corte", "mute", "mutear", "cerrar pico",
            "la boca", "no sigas", "interrumpe", "interrupcion", "cancela", "cancelar",
            "aborta", "abortar", "quiet", "shut up", "be quiet", "pause", "pausar", "estop",
            "ya", "suficiente", "nada mas", "hasta ahi", "dejalo", "omitir", "saltar", "te callas"
        ]

        close_words = [
            "cierra", "cierrate", "cerrar", "salir", "sal", "salte", "exit", "quit", "finalizar",
            "finaliza", "termina", "terminá", "terminar", "fin", "acabar", "acaba", "acabá",
            "adios", "bye", "chao", "chau", "hasta luego", "nos vemos", "goodbye", "see you",
            "apagate", "shutdown", "kill", "matar", "muerete", "morite", "destruye",
            "autodestruccion", "vete", "andate", "largo", "fuera", "safa", "terminate", "close",
            "sistema fuera", "abortar", "cerrar sistema", "bye bye", "hasta mañana", "apagar sistema"
        ]

        # Función auxiliar para crear el patrón: (Arthur COMANDO) | (COMANDO Arthur)
        def make_pattern(word_list):
            # Regex con límites de palabra estrictos para detectar palabras cortas como "a"
            cmds = r"(?:" + "|".join(map(re.escape, word_list)) + r")"
            # Usamos \s+ para espacios y \b para límites de palabra
            return re.compile(rf"(?:{names}\s+{cmds}\b)|(?:\b{cmds}\s+{names})", re.IGNORECASE)

        # 3. Compilamos los Regex con Lógica de Mezcla (Fuzzy)
        # Este patrón busca el nombre Y el comando en cualquier lugar del bloque de texto
        def make_fuzzy_pattern(word_list):
            cmds = r"(?:" + "|".join(map(re.escape, word_list)) + r")"
            # Usamos re.DOTALL como flag en lugar de inline (?s) para evitar errores de posicion
            return re.compile(rf".*?{names}.*?{cmds}|.*?{cmds}.*?{names}", re.IGNORECASE | re.DOTALL)

        self.re_stop_fuzzy = make_fuzzy_pattern(stop_words)
        self.re_sleep_fuzzy = make_fuzzy_pattern(sleep_words)
        self.re_close_fuzzy = make_fuzzy_pattern(close_words)
        
        # 4. Patrón de Nombre Solo (Para permitir cualquier comando con nombre)
        self.re_name = re.compile(rf"{names}", re.IGNORECASE)

        # Patrones normales para cuando Arthur NO habla
        # CORRECCIÓN DE SINTAXIS: Usamos comillas simples para el join interno
        self.re_stop = re.compile(rf"(?:\b|^)(?:{'|'.join(map(re.escape, stop_words))})(?:\b|$|{names})", re.IGNORECASE)
        self.re_wake = make_pattern(wake_words)
        self.re_sleep = make_pattern(sleep_words)
        self.re_close = make_pattern(close_words)




    def add_file(self, path):
        self.files_context.append(path)

    def run(self):
        if not self.mic: return
        
        try:
            with self.mic as source:
                # Calibración inicial (1 segundo)
                self.r.adjust_for_ambient_noise(source, duration=1.0)
                log.info(f"Calibración inicial lista. Threshold: {self.r.energy_threshold}")
                
                while self.running:
                    if not self.listening:
                        time.sleep(0.5)
                        continue
                    
                    self.speech_interfered = self.system_speaking
                    # PROTECCIÓN DINÁMICA: 
                    # - 3.5s si Arthur habla (Casi frases enteras para asegurar overlap completo)
                    # - 20.0s si tú hablas (Tiempo infinito para pensar)
                    limit = 3.5 if self.system_speaking else 20.0
                    
                    try:
                        start_listen = time.time()
                        # Escucha con timeout de seguridad (Acepta cualquier voz en 1.5s o reintenta)
                        audio = self.r.listen(source, timeout=1.5 if not self.system_speaking else None, phrase_time_limit=limit)
                        listen_duration = time.time() - start_listen
                        
                        was_comp = self.system_speaking or self.speech_interfered
                        ts = time.time()
                        
                        # FEEDBACK INSTANTÁNEO: Mostrar que "escuchó" antes de ir a la nube
                        if not was_comp:
                            self.visual_signal.emit("THINKING")
                            self.status_signal.emit(f"🧠 Escuchado ({listen_duration:.1f}s)...")
                        
                        # LANZAMIENTO ASÍNCRONO
                        task = STTTask(self.r, audio, self.process_text, was_comp, ts, listen_duration)
                        QThreadPool.globalInstance().start(task)
                        
                    except sr.WaitTimeoutError:
                        pass
                    except Exception as e:
                        time.sleep(0.01)
        except Exception as e:
            log.error(f"Falla crítica en hilo de voz: {e}")

    def process_text(self, text, was_listening_to_system=False, capture_timestamp=None, listen_duration=0.0):
        """Procesa texto (Voz o Teclado) con lógica blindada de eventos."""
        # Clean punctuation: "Arthur, cierra!" -> "Arthur cierra"
        norm = text.lower()
        for char in string.punctuation:
            norm = norm.replace(char, " ")
        norm = " ".join(norm.split()) # Normalize spaces
        norm = ''.join(c for c in unicodedata.normalize("NFD", norm) if unicodedata.category(c) != "Mn")
        
        # --- SELECTIVE LISTENING FILTER (ANTI-ECHO) ---
        # Emergency words that ALWAYS execute, bypassing all filters
        EMERGENCY_WORDS = ["stop", "alto", "silencio", "callate", "para", "basta", 
                          "cierra", "cierrate", "cerrar", "adios", "bye", "salir"]
        has_emergency = any(word in norm for word in EMERGENCY_WORDS)
        
        # CRITICAL: Check for close command FIRST (before any filters)
        # Close commands must ALWAYS execute immediately
        if self.re_close.search(norm):
            log.info(f"🚨 CLOSE command detected (bypassing all filters): '{norm}'")
            self.command_action.emit("stop_tts", None)
            self.command_action.emit("close_app", None)
            return
        
        # HARDENING A: Audio cooldown firewall
        in_cooldown = time.time() < self.tts_cooldown_until
        if in_cooldown and not has_emergency:
            log.debug(f"🔥 Cooldown active - audio blocked: '{text[:50]}'")
            return
        
        # HARDENING B: Text similarity detection (anti-repetition)
        if self.last_spoken_text:
            # 1. Similarity Ratio (Fuzzy)
            similarity = SequenceMatcher(None, norm, self.last_spoken_text.lower()).ratio()
            if similarity > 0.65 and not has_emergency:
                log.debug(f"🔁 Echo detected by similarity ({similarity:.2f}): '{text[:50]}'")
                return

            # 2. Strict Inclusion (Subtitle Check) - "Límite de ejecución" inside "Parece que no puedo obtener..."
            # Agregamos longitud mínima para no ignorar "si", "no", etc.
            if len(norm) > 5 and norm in self.last_spoken_text.lower() and not has_emergency:
                 log.debug(f"🔁 Echo detected by inclusion: '{text[:50]}'")
                 return
        
        # Check if system is busy (speaking or thinking)
        is_system_busy = False
        if self.parent_window:
            is_system_busy = self.parent_window.is_speaking or self.parent_window.is_thinking
        
        # If system is busy and no emergency word detected, discard audio (assume echo)
        if is_system_busy and not has_emergency:
            log.debug(f"🛡️ Audio discarded - system busy: '{text[:50]}'")
            return
        
        # If emergency word detected while busy, log and proceed
        if is_system_busy and has_emergency:
            log.info(f"⚡ Emergency command during busy state: '{text}'")
        
        # 0. FILTRO DE ECO / BARGE-IN CON LÓGICA DE "COLA" (TAIL CHECK)
        # Un eco puro muere cuando Arthur muere.
        # Si la grabación sigue viva > 0.8s después de que Arthur muere, es el Usuario.
        
        real_barge_in = was_listening_to_system or self.system_speaking
        capture_end = capture_timestamp if capture_timestamp else time.time()
        
        time_since_speech_end = capture_end - self.last_speech_time
        
        # Si la grabación terminó mucho después (>0.8s) de que Arthur se callara
        # SIGNIFICA QUE ALGUIEN SIGUIÓ HABLANDO. (Usuario)
        if real_barge_in and not self.system_speaking and time_since_speech_end > 0.8:
            real_barge_in = False
            log.debug(f"Barge-in anulado: Cola de audio detectada (+{time_since_speech_end:.2f}s). Probable usuario.")
            
        # REGLA DE SEGURIDAD ABSOLUTA
        # Si ya pasó 1.5s desde que se calló, liberamos todo.
        if (time.time() - self.last_speech_time) > 1.5:
             real_barge_in = False

        if real_barge_in:
            # Si es un barge-in real, aplicamos el filtro de nombre
            is_interrupt = (self.re_stop_fuzzy.search(norm) or 
                            self.re_sleep_fuzzy.search(norm) or 
                            self.re_close_fuzzy.search(norm))
            
            has_name = self.re_name.search(norm)
            
            if not is_interrupt and not has_name:
                log.debug(f"ECO BLOQUEADO: '{norm}' (Barge-in detectado sin nombre)")
                return 
            
            log.info(f"BARGE-IN PERMITIDO: '{norm}'")

        # COOLDOWN CERO: Arthur escucha siempre
        # Eliminamos bloqueos por tiempo para máxima reactividad
        
        # --- 1. REFLEJOS (Prioridad Máxima - NO Publicar al chat) ---
        
        # STOP (Detener Voz + Cancelar Worker)
        if self.re_stop.search(norm):
            self.command_action.emit("stop_tts", None)
            self.command_action.emit("cancel_agent", None)  # NEW: Cancel in-flight agent
            self.visual_signal.emit("IDLE")
            # Continuamos para registrarlo en el chat

        # WAKE (Despertar)
        if self.re_wake.search(norm):
            if self.sleep_mode:
                self.command_action.emit("stop_tts", None)
                self.sleep_mode = False
                self.visual_signal.emit("LISTENING")
                self.status_signal.emit("👂 Escuchando")
            # Continuamos para registrarlo en el chat

        # SLEEP (Dormir)
        if self.re_sleep.search(norm):
            if not self.sleep_mode:
                self.command_action.emit("stop_tts", None)
                self.sleep_mode = True
                self.visual_signal.emit("SLEEP")
                self.status_signal.emit("💤 Standby")
            # Continuamos para registrarlo en el chat


        # Si está dormido, ignoramos todo lo demás
        if self.sleep_mode:
            return

        # RECORDING REFLEXES (Grabación por Voz Flexible)
        def _has(phrases):
            norm_words = set(norm.split())
            return any(all(w in norm_words for w in p.split()) for p in phrases)

        if _has(["inicia grabacion", "empezar grabacion", "graba mision", "iniciar grabacion", "graba proceso", "grabar proceso", "iniciar proceso"]):
            # Extraer nombre del proceso del comando de voz
            skip_words = {"inicia", "iniciar", "empezar", "graba", "grabar", "grabacion", "mision", "proceso", "la", "una", "nueva", "nuevo"}
            words = norm.split()
            name_words = []
            found_content = False
            for w in words:
                if w in skip_words and not found_content:
                    continue
                found_content = True
                name_words.append(w)
            mission_name = " ".join(name_words).strip() if name_words else ""
            self.command_action.emit(f"start_recording|{mission_name}" if mission_name else "start_recording", None)
            return
            
        if _has(["finaliza grabacion", "deten grabacion", "termina grabacion", "guardar mision", "guarda mision", "detener grabacion", "finalizar proceso", "guardar proceso"]):
            self.command_action.emit("stop_recording", None)
            return

        if _has(["pausa grabacion", "pausar grabacion"]):
            self.command_action.emit("pause_recording", None)
            return
            
        if _has(["reanuda grabacion", "volver grabar", "reanudar grabacion"]):
            self.command_action.emit("resume_recording", None)
            return

        # AUTOMATION PLAYBACK REFLEXES
        if _has(["ejecuta", "ejecutar", "corre", "correr", "reproduce", "reproducir", "lanza"]):
            # Extraer nombre de la automatización quitando palabras de comando
            skip_words = {"ejecuta", "ejecutar", "corre", "correr", "reproduce", "reproducir", 
                         "lanza", "la", "el", "mision", "automatizacion", "proceso"}
            words = norm.split()
            # Encontrar donde empieza el nombre real (después de los comandos)
            name_words = []
            found_content = False
            for w in words:
                if w in skip_words and not found_content:
                    continue
                found_content = True
                name_words.append(w)
            name = " ".join(name_words).strip()
            
            # Si solo quedan palabras de comando sin nombre, intentar con todo lo que queda
            if not name:
                # Fallback: quitar solo el primer verbo
                name = " ".join(words[1:]).strip()
            
            if name:
                self.command_action.emit(f"run_mission|{name}", None)
                return

        # --- 2. CEREBRO (Agent) ---
        # NOTA: NO publicamos al bus. El Agent ya publica agent.user_input y agent.final internamente.
        if not bus:
            self.speech_detected.emit(text) 
            
        self.visual_signal.emit("THINKING")
        self.status_signal.emit("🧠 Procesando...")
        
        file_msg = ""
        while self.files_context:
            f = self.files_context.popleft()
            file_msg += f"\n[Usuario adjuntó archivo: {f}]"
        
        full_query = text + file_msg
        
        # --- 3. RESPUESTA (BACKGROUND PROCESSING) ---
        # Si es un comando de Wake/Sleep, no necesitamos que Arthur hable la respuesta del LLM
        # ya que la acción es mecánica, pero queremos ver el texto en el chat.
        is_internal = self.re_stop.search(norm) or self.re_wake.search(norm) or self.re_sleep.search(norm)
        
        # Avoid sending to LLM if it's a known reflexive action (including recording logic above)
        is_reflex = is_internal or any(x in norm for x in ["grabacion", "mision", "graba", "grabar", "proceso", "automatizacion"])
        
        if is_reflex:
            # For internal commands, we still process but don't speak
            # This maintains chat history without audio output
            try:
                response = agent.process_user_input(full_query)
                # Don't emit TTS for internal commands
            except Exception as e:
                log.error(f"Internal command error: {e}")
        else:
            # For normal queries, use background worker
            if self.parent_window:
                self.parent_window.invoke_agent(full_query)
            else:
                # Fallback if no parent window (shouldn't happen)
                try:
                    response = agent.process_user_input(full_query)
                    self.request_tts.emit(response)
                except Exception as e:
                    self.request_tts.emit(f"Error: {e}")
        
        # RESTAURACIÓN VISUAL: Volvemos al estado base
        # Note: For background processing, state will be updated by agent callbacks
        if is_internal:
            state = "SLEEP" if self.sleep_mode else "LISTENING"
            self.visual_signal.emit(state)
            self.status_signal.emit("👂 Escuchando" if not self.sleep_mode else "💤 Standby")


# --- 3. COMPONENTES UI ---

class AudioVisualizer(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(60, 24)
        self.bars = [3.0] * 5
        self.target_color = NevlanDesign.STATE_COLORS["IDLE"]
        self.current_color = self.target_color
        self.state_key = "IDLE"
        self.timer = QTimer(self); self.timer.timeout.connect(self.animate); self.timer.start(40)

    def set_state(self, key):
        self.state_key = key
        if key in NevlanDesign.STATE_COLORS: self.target_color = NevlanDesign.STATE_COLORS[key]

    def animate(self):
        r = self.current_color.red() + (self.target_color.red() - self.current_color.red()) * 0.15
        g = self.current_color.green() + (self.target_color.green() - self.current_color.green()) * 0.15
        b = self.current_color.blue() + (self.target_color.blue() - self.current_color.blue()) * 0.15
        self.current_color = QColor(int(r), int(g), int(b))
        
        if self.state_key in ["SLEEP", "PAUSED", "IDLE"]: self.bars = [3.0] * 5
        elif self.state_key == "ERROR": self.bars = [2.0] * 5
        elif self.state_key == "LISTENING": self.bars = [random.uniform(4, 20) for _ in range(5)]
        elif self.state_key == "THINKING":
            t = time.time() * 10
            self.bars = [6 + 4 * ((i + t) % 3) for i in range(5)]
        elif self.state_key == "SPEAKING": self.bars = [8 + 8 * random.random() for _ in range(5)]
        self.update()

    def paintEvent(self, e):
        p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(self.current_color); p.setPen(Qt.PenStyle.NoPen)
        w=6; sp=4; start=(self.width()-(len(self.bars)*(w+sp)))/2
        for i,h in enumerate(self.bars):
            p.drawRoundedRect(QRect(int(start+i*(w+sp)), int((self.height()-h)/2), w, int(h)), 2, 2)


class NevlanPill(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(320, 60)
        self.center_top()
        
        # --- STATE FLAGS FOR ANTI-ECHO AND RESPONSIVENESS ---
        self.is_speaking = False  # True when TTS is actively speaking
        self.is_thinking = False  # True when agent is processing
        self.current_run_id = 0   # Generation ID for logical cancellation
        self.agent_worker = None  # Reference to current agent worker thread
        
        # --- HARDENING: Worker Queue and Watchdog ---
        self.pending_query = None  # Queue for next query (single slot)
        self.watchdog_timer = QTimer()
        self.watchdog_timer.timeout.connect(self.on_watchdog_timeout)
        self.watchdog_timer.setSingleShot(True)

        self.central = QWidget(); self.setCentralWidget(self.central)
        self.layout = QVBoxLayout(self.central); self.layout.setContentsMargins(0,0,0,0); self.layout.setSpacing(0)
        
        self.header = QWidget(); self.header.setFixedHeight(60)
        hl = QHBoxLayout(self.header); hl.setContentsMargins(15, 5, 15, 5)
        
        self.lbl = QLabel("Nevlan"); self.lbl.setStyleSheet("color: white; font-weight: bold; font-size: 13px;")
        self.vis = AudioVisualizer()
        
        # Botones de control
        self.btn_missions = QPushButton("📋")
        self.btn_tts = QPushButton("🗣️"); self.btn_tts.setCheckable(True); self.btn_tts.setChecked(True)
        self.btn_mic = QPushButton("🎙️"); self.btn_mic.setCheckable(True); self.btn_mic.setChecked(True)
        st = "background: transparent; border: none; font-size: 16px; padding: 4px;"
        self.btn_missions.setStyleSheet(st); self.btn_tts.setStyleSheet(st); self.btn_mic.setStyleSheet(st)

        hl.addWidget(self.lbl); hl.addStretch(); hl.addWidget(self.btn_missions); hl.addWidget(self.vis); hl.addWidget(self.btn_tts); hl.addWidget(self.btn_mic)
        self.layout.addWidget(self.header)

        self.panel = QFrame(); self.panel.setVisible(False)
        self.pl = QVBoxLayout(self.panel); self.pl.setContentsMargins(10, 0, 10, 10)
        self.chat = QTextEdit(); self.chat.setReadOnly(True)
        self.chat.setStyleSheet("background: rgba(0,0,0,0.3); border-radius: 8px; color: #ddd; border: none;")
        self.input = QLineEdit(); self.input.setPlaceholderText("Escribe un comando...")
        self.input.setStyleSheet("background: rgba(255,255,255,0.1); border-radius: 12px; color: white; padding: 6px; border: 1px solid rgba(255,255,255,0.2);")
        
        self.pl.addWidget(self.chat); self.pl.addWidget(self.input)
        self.layout.addWidget(self.panel)

        self.bridge = EventBridge() 
        self.voice = VoiceWorker(self)  # Pass self as parent_window
        self.tts = TTSWorker()      

        # Conexiones
        self.bridge.new_chat_msg.connect(self.add_chat_msg)
        self.bridge.status_update.connect(self.lbl.setText)
        self.bridge.visual_state.connect(self.vis.set_state)
        self.bridge.visual_state.connect(self.sync_mic_button)
        self.bridge.mission_review_req.connect(self.show_mission_review)
        self.bridge.ui_action_req.connect(lambda c: self.handle_command(c, None))
        
        self.voice.status_signal.connect(self.lbl.setText)
        self.voice.visual_signal.connect(self.vis.set_state)
        self.voice.visual_signal.connect(self.sync_mic_button)
        # self.voice.speech_detected.connect... YA NO ES NECESARIO (Todo va por Bus)
        self.voice.request_tts.connect(self.tts.say)
        self.voice.command_action.connect(self.handle_command)
        
        # Conexiones Anti-Eco
        # IMPORTANTE: Usamos DirectConnection para que el flag se actualice
        # instantáneamente incluso si el VoiceWorker está bloqueado escuchando.
        self.tts.start_speak_signal.connect(self.voice.start_system_speech, Qt.ConnectionType.DirectConnection)
        self.tts.end_speak_signal.connect(self.voice.end_system_speech, Qt.ConnectionType.DirectConnection)
        
        # Conexiones de Estado para Flags
        self.tts.start_speak_signal.connect(self.on_tts_start, Qt.ConnectionType.DirectConnection)
        self.tts.end_speak_signal.connect(self.on_tts_end, Qt.ConnectionType.DirectConnection)
        
        # Botones UI
        self.btn_mic.clicked.connect(self.toggle_mic_mode)
        self.btn_tts.clicked.connect(self.toggle_voice) 
        self.btn_missions.clicked.connect(self.show_mission_list)
        self.input.returnPressed.connect(self.on_manual_input)

        self.tts.start()
        self.voice.start()
        self.setAcceptDrops(True)
        
        # --- UI Feedback Phase 1: Live Narrator ---
        self.narrator_overlay = ActionRecorderOverlay()
        self._overlay_shown = False
        
        # Timer to poll recording status and toggle overlay
        self.recorder_poll_timer = QTimer(self)
        self.recorder_poll_timer.timeout.connect(self._poll_recorder_state)
        self.recorder_poll_timer.start(1000) # Check every 1s
        
        # Secuencia de Arranque Coordinada
        # 1. Conectamos señal de fin de habla SOLO para el saludo inicial
        self.tts.end_speak_signal.connect(self.on_startup_speech_end)
        # 2. Conectamos señal de TTS para tracking de texto hablado
        self.tts.start_speak_signal.connect(self.on_tts_about_to_speak)
        # 3. Iniciamos saludo con delay para dar tiempo a la UI
        QTimer.singleShot(1000, lambda: self.tts.say("Sistemas en línea."))

    def on_startup_speech_end(self):
        """Se ejecuta una sola vez tras 'Sistemas en línea'."""
        try:
            self.tts.end_speak_signal.disconnect(self.on_startup_speech_end)
        except: pass
        
        # Activamos el oído FÍSICAMENTE solo ahora
        self.voice.listening = True
        
        # Sincronizamos botón UI
        if not self.btn_mic.isChecked():
            self.btn_mic.blockSignals(True)
            self.btn_mic.setChecked(True)
            self.btn_mic.blockSignals(False)
            
        self.vis.set_state("LISTENING")
        self.lbl.setText("👂 Escuchando")
    
    # --- STATE FLAG HANDLERS ---
    def on_tts_start(self):
        """Called when TTS starts speaking."""
        self.is_speaking = True
    
    def on_tts_end(self):
        """Called when TTS finishes speaking."""
        self.is_speaking = False
    
    def on_tts_about_to_speak(self):
        """Track what we're about to say for echo detection."""
        # Peek at the queue safely to know what will be spoken
        if self.tts.queue:
            try:
                # Normalizamos igual que en process_text para comparar peras con peras
                raw_text = self.tts.queue[0]
                norm = raw_text.lower()
                for char in string.punctuation:
                    norm = norm.replace(char, " ")
                norm = " ".join(norm.split())
                norm = ''.join(c for c in unicodedata.normalize("NFD", norm) if unicodedata.category(c) != "Mn")
                
                self.voice.last_spoken_text = norm
                log.info(f"Anti-Echo Armed: '{self.voice.last_spoken_text[:30]}...'")
            except Exception:
                pass
    
    # --- WATCHDOG AND STATE RECOVERY ---
    def on_watchdog_timeout(self):
        """
        Called if agent worker takes too long (stuck).
        
        This prevents the system from being permanently stuck in 'thinking' state.
        """
        log.warning("⚠️ Watchdog timeout - agent worker appears stuck")
        
        # Force state recovery
        self.is_thinking = False
        self.vis.set_state("LISTENING" if not self.voice.sleep_mode else "SLEEP")
        self.lbl.setText("⚠️ Timeout - reiniciado")
        
        # Invalidate current worker
        self.current_run_id += 1
        
        # Process pending query if any
        if self.pending_query:
            log.info("Processing queued request after timeout")
            query = self.pending_query
            self.pending_query = None
            QTimer.singleShot(500, lambda: self.invoke_agent(query))
    
    # --- AGENT WORKER METHODS (GENERATION ID SYSTEM) ---
    def invoke_agent(self, query: str):
        """
        Start agent processing in background thread with generation ID tracking.
        
        This allows the UI to remain responsive during agent "thinking" and
        enables logical cancellation via generation IDs.
        """
        # HARDENING: Worker queue - if already processing, queue this request
        if self.agent_worker and self.agent_worker.isRunning():
            log.debug(f"Worker busy - queuing new request (replacing any pending)")
            self.pending_query = query  # Single slot queue (latest wins)
            return
        
        # Create new worker with fresh generation ID
        self.current_run_id += 1
        self.agent_worker = AgentWorker(query, self.current_run_id)
        
        # Connect signals
        self.agent_worker.started.connect(self.on_agent_start)
        self.agent_worker.finished.connect(self.on_agent_finish)
        self.agent_worker.error.connect(self.on_agent_error)
        
        # Update state and start processing
        self.is_thinking = True
        self.agent_worker.start()
        
        log.info(f"Started AgentWorker with ID {self.current_run_id}")
    
    def on_agent_start(self):
        """Called when agent processing begins."""
        self.is_thinking = True
        
        # HARDENING: Start watchdog (5 minutes max para RPA replay) - MUST be in main thread
        self.watchdog_timer.start(300000)
        
        log.debug("Agent processing started")
    
    def on_agent_finish(self, response: str, run_id: int):
        """
        Called when agent processing completes.
        
        Validates generation ID before delivering response to prevent
        stale responses from being spoken after user has moved on.
        """
        # Stop watchdog
        self.watchdog_timer.stop()
        
        self.is_thinking = False
        
        # Validate generation ID
        if run_id != self.current_run_id:
            log.debug(f"Discarding stale response (ID {run_id} != {self.current_run_id})")
            return
        
        # Process valid response
        log.info(f"Agent response validated (ID {run_id})")
        
        # HARDENING: Track what we're about to say for echo detection
        self.voice.last_spoken_text = response
        
        self.tts.say(response)
        
        # HARDENING: Process pending query if any
        if self.pending_query:
            log.info("Processing queued request")
            query = self.pending_query
            self.pending_query = None
            QTimer.singleShot(100, lambda: self.invoke_agent(query))
    
    def on_agent_error(self, error_msg: str):
        """Called when agent processing encounters an error."""
        # Stop watchdog
        self.watchdog_timer.stop()
        
        self.is_thinking = False
        log.error(f"Agent error: {error_msg}")
        self.tts.say(f"Error: {error_msg}")
        
        # HARDENING: Process pending query if any
        if self.pending_query:
            log.info("Processing queued request after error")
            query = self.pending_query
            self.pending_query = None
            QTimer.singleShot(500, lambda: self.invoke_agent(query))

    def toggle_voice(self, checked):
        if checked:
            self.btn_tts.setText("🗣️")
            self.tts.set_enabled(True)
        else:
            self.btn_tts.setText("🔇")
            self.tts.set_enabled(False)

    def toggle_mic_mode(self, checked):
        self.voice.sleep_mode = not checked
        self.voice.listening = True 
        
        state = "LISTENING" if checked else "SLEEP"
        self.vis.set_state(state)
        self.lbl.setText("👂 Escuchando" if checked else "💤 Standby")

    def sync_mic_button(self, state):
        if state == "SLEEP":
            if self.btn_mic.isChecked():
                self.btn_mic.blockSignals(True)
                self.btn_mic.setChecked(False)
                self.btn_mic.blockSignals(False)
        elif state == "LISTENING":
            if not self.btn_mic.isChecked():
                self.btn_mic.blockSignals(True)
                self.btn_mic.setChecked(True)
                self.btn_mic.blockSignals(False)

    def center_top(self):
        g = QApplication.primaryScreen().geometry()
        self.move((g.width()-320)//2, int(g.height()*0.08))

    def on_manual_input(self):
        t = self.input.text()
        if t:
            self.input.clear()
            
            # UNIFIED COMMAND HANDLING:
            # Normalize text exactly like VoiceWorker to catch commands
            norm = t.lower()
            for char in string.punctuation:
                norm = norm.replace(char, " ")
            norm = " ".join(norm.split())
            norm = ''.join(c for c in unicodedata.normalize("NFD", norm) if unicodedata.category(c) != "Mn")
            
            # Check for critical commands (CLOSE)
            # Use regex from voice worker to ensure consistency
            if hasattr(self.voice, 're_close') and self.voice.re_close.search(norm):
                log.info(f"🚨 CLOSE command detected in MANUAL input: '{norm}'")
                self.handle_command("stop_tts", None)
                self.handle_command("close_app", None)
                return

            self.vis.set_state("THINKING")
            # NEW: Use background agent worker instead of blocking call
            self.invoke_agent(t)

    def handle_command(self, cmd, data):
        if cmd == "stop_tts":
            self.tts.stop_speaking()
            # Restaurar estado base
            state = "SLEEP" if self.voice.sleep_mode else "LISTENING"
            self.vis.set_state(state)
            self.lbl.setText("👂 Escuchando" if state == "LISTENING" else "💤 Standby")
        elif cmd == "cancel_agent":
            # HARDENING: Cancel in-flight agent work
            if self.agent_worker and self.agent_worker.isRunning():
                log.info("Cancelling agent worker via STOP command")
                
                # Notify agent to cleanup incomplete tool calls
                from app.brain.agent import agent
                agent.cancel_current_request()
                
                self.current_run_id += 1  # Invalidate current worker
                self.watchdog_timer.stop()  # Stop watchdog
                self.is_thinking = False
                # Clear pending queue
                self.pending_query = None
        elif cmd == "close_app":
            self.close()
        elif cmd == "start_recording" or cmd.startswith("start_recording|"):
            from app.services.missions.recorder import is_recording_active
            if is_recording_active():
                log.debug("Ignorado: ya hay grabación activa")
                return
            try:
                mission_name = ""
                if "|" in cmd:
                    _, mission_name = cmd.split("|", 1)
                from app.services.missions.recorder import start_recording
                start_recording(mission_name)
                display_name = mission_name or "nuevo proceso"
                self.add_chat_msg("system", f"▶️ Grabando proceso: {display_name}")
                self.tts.say(f"Grabando proceso")
            except Exception as e:
                log.error(f"Error iniciando grabación: {e}")
        elif cmd == "stop_recording":
            from app.services.missions.recorder import is_recording_active
            if not is_recording_active():
                log.debug("Ignorado: no hay grabación activa")
                return
            try:
                from app.services.missions.recorder import stop_and_compile
                m = stop_and_compile()
                if m:
                    self.add_chat_msg("system", "⏹️ Grabación finalizada")
            except Exception as e:
                log.error(f"Error parando grabación: {e}")
        elif cmd == "pause_recording":
            from app.services.missions.recorder import pause_recording
            pause_recording()
            self.tts.say("Grabación pausada")
        elif cmd == "resume_recording":
            from app.services.missions.recorder import resume_recording
            resume_recording()
            self.tts.say("Grabación reanudada")
        elif cmd.startswith("annotate|"):
            _, text = cmd.split("|", 1)
            from app.services.missions.recorder import get_recorder
            rec = get_recorder()
            if rec:
                rec.add_annotation("rule", text)
                self.add_chat_msg("system", f"📝 Anotación guardada: {text}")
        elif cmd.startswith("run_mission|"):
            _, name = cmd.split("|", 1)
            import threading
            def _run():
                try:
                    from app.services.missions.store import mission_store
                    log.info(f"Buscando automatización: '{name}'")
                    m = mission_store.find_by_name(name)
                    if m:
                        log.info(f"Automatización encontrada: '{m.name}' ({m.id}), {len(m.compiled_execution_graph)} pasos")
                        self.tts.say(f"Ejecutando automatización {m.name}")
                        from app.services.missions.player import replay_mission
                        from app.services.missions.execution_tracker import tracker
                        exec_idx = tracker.log_start(
                            m.id, m.name, len(m.compiled_execution_graph), "voice")
                        execution = replay_mission(m)
                        log.info(f"Automatización finalizada: {execution.status.value}")
                        if exec_idx >= 0:
                            err = execution.error_details or ""
                            tracker.log_complete(
                                exec_idx,
                                execution.status.value,
                                steps_executed=execution.steps_executed,
                                error=str(err),
                            )
                    else:
                        log.warning(f"Automatización no encontrada: '{name}'")
                        self.tts.say(f"No encontré la automatización llamada {name}")
                except Exception as e:
                    log.error(f"Error ejecutando automatización '{name}': {e}")
                    self.tts.say(f"Error ejecutando automatización: {e}")
            
            # Exec in background to prevent UI freeze
            threading.Thread(target=_run, daemon=True).start()
            self.add_chat_msg("assistant", f"▶️ Ejecutando automatización: {name}")

    def add_chat_msg(self, role, text):
        colors = {"user": "#00cec9", "assistant": "#ffffff", "system": "#ff7675", "tool": "#a29bfe"}
        icons = {"user": "👤", "assistant": "🤖", "system": "⚠️", "tool": "🛠️"}
        c = colors.get(role, "#ffffff")
        p = icons.get(role, "•")
        formatted = text.replace("\n", "<br>")
        self.chat.append(f"<div style='margin-bottom:6px;'><b style='color:{c}'>{p}</b> {formatted}</div>")
        self.chat.verticalScrollBar().setValue(self.chat.verticalScrollBar().maximum())

    def mouseDoubleClickEvent(self, e):
        vis = not self.panel.isVisible()
        self.panel.setVisible(vis)
        self.setFixedSize(320, 400 if vis else 60)

    def mousePressEvent(self, e): 
        if e.button() == Qt.MouseButton.LeftButton: self.old_pos = e.globalPosition().toPoint()
    def mouseMoveEvent(self, e):
        if hasattr(self, 'old_pos') and self.old_pos:
            self.move(self.pos() + e.globalPosition().toPoint() - self.old_pos)
            self.old_pos = e.globalPosition().toPoint()
    def mouseReleaseEvent(self, e): self.old_pos = None

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls(): e.accept()
    def dropEvent(self, e):
        for url in e.mimeData().urls():
            path = url.toLocalFile()
            self.voice.add_file(path)
            self.add_chat_msg("system", f"📎 Archivo adjunto: {Path(path).name}")

    def paintEvent(self, e):
        p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        grad = QLinearGradient(0, 0, 0, self.height())
        grad.setColorAt(0, NevlanDesign.BG_TOP); grad.setColorAt(1, NevlanDesign.BG_BTM)
        p.setBrush(grad); p.setPen(QPen(NevlanDesign.BORDER, 1))
        p.drawRoundedRect(self.rect(), 15, 15)

    def _poll_recorder_state(self):
        """Revisa constantemente si hay una grabación activa para mostrar los subtítulos vivos."""
        try:
            from app.services.missions.recorder import get_recorder
            recorder = get_recorder()
            
            is_recording = recorder is not None and recorder.is_recording()
            
            if is_recording and not self._overlay_shown:
                self.narrator_overlay.show_overlay()
                self._overlay_shown = True
            elif not is_recording and self._overlay_shown:
                self.narrator_overlay.hide_overlay()
                self._overlay_shown = False
        except Exception as e:
            log.warning(f"Error checking recorder state for overlay: {e}")

    def closeEvent(self, e):
        """Limpieza al cerrar la aplicación (Rescate de Crash)."""
        try:
            self.narrator_overlay.close()
            
            # Notificar hilos
            self.voice.running = False
            self.tts.running = False
            
            # Despertar TTS si está esperando en condition.wait
            self.tts.mutex.lock()
            self.tts.condition.wakeAll()
            self.tts.mutex.unlock()
            
            # Esperar cierre
            self.voice.wait(800)
            self.tts.wait(800)
        except:
            pass
        e.accept()

    def show_mission_review(self, mission_id):
        from app.services.missions import mission_store
        from app.interfaces.desktop.mission_review import MissionReviewDialog
        
        try:
            log.info(f"UI Thread received mission review request for: {mission_id}")
            mission = mission_store.load(mission_id)
            if not mission:
                # Fallback if load returns None but mission exists
                for m in mission_store.list_all():
                    if m.id == mission_id:
                        mission = m
                        break
                        
            if not mission: 
                log.error(f"Cannot show review, mission {mission_id} not found.")
                return
                
            log.info(f"Mission loaded: {mission.name}. Desplegando dialog.")
            # Traer la ventana principal al frente antes de la revisión (tras grabar
            # pudo quedar detrás o minimizada).
            try:
                self.raise_()
                self.activateWindow()
            except Exception:
                pass
            dialog = MissionReviewDialog(mission, self)
            dialog.exec()
            log.info("Dialog returned cleanly")
            try:
                self.raise_()
                self.activateWindow()
            except Exception:
                pass
        except Exception as ex:
            log.error(f"Error showing mission review: {ex}")

    def show_mission_list(self):
        try:
            from app.interfaces.desktop.automation_center import AutomationCenter
            center = AutomationCenter()
            center.show()
        except Exception as e:
            log.error(f"Error abriendo Centro de Automatizaciones: {e}")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = NevlanPill()
    w.show()
    sys.exit(app.exec())