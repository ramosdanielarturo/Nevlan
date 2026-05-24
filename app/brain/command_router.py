import yaml
import re
from typing import List, Dict, Optional, Tuple, Any
from enum import Enum
from pathlib import Path
from difflib import SequenceMatcher

# Import MissionStore to access available missions
# Assuming MissionStore is a singleton or can be instantiated easily
# Adjust import based on actual file structure
try:
    from app.services.missions.store import MissionStore, Mission
except ImportError:
    # Fallback or mock if store not available yet
    class Mission:
        def __init__(self, name, aliases=None):
            self.name = name
            self.aliases = aliases or []

    class MissionStore:
        _instance = None
        def __new__(cls):
            if cls._instance is None:
                cls._instance = super(MissionStore, cls).__new__(cls)
                cls._instance.missions = {} 
            return cls._instance
        
        def get_all_missions(self) -> List[Mission]:
            return list(self.missions.values())

class RouteType(Enum):
    CONTROL = "CONTROL"        # stop, sleep, close
    MISSIONS_ADMIN = "MISSIONS_ADMIN" # list, create, delete, etc.
    MISSION_RUN = "MISSION_RUN"  # execute a specific mission
    SKILL_RUN = "SKILL_RUN"    # direct skill execution (playbook)
    SCROLL = "SCROLL"          # scroll / reading mode
    LLM = "LLM"                # fallback to general agent

class RouteDecision:
    def __init__(self, route_type: RouteType, confidence: float, target: Optional[str] = None, metadata: Dict[str, Any] = None):
        self.route_type = route_type
        self.confidence = confidence
        self.target = target
        self.metadata = metadata or {}

    def __repr__(self):
        return f"RouteDecision({self.route_type}, conf={self.confidence}, target={self.target})"

class CommandRouter:
    def __init__(self, config_path: str = "app/config/verbs_es.yaml"):
        self.config = self._load_config(config_path)
        self.mission_store = MissionStore()
        self.STOP_WORDS = {"el", "la", "los", "las", "un", "una", "de", "en", "a", "por", "para", "con"}
        
    def _load_config(self, path: str) -> dict:
        try:
            # Adjust path to be relative to project root if needed
            real_path = Path(path)
            if not real_path.exists():
                # Try finding it relative to this file
                real_path = Path(__file__).parent.parent / "config" / "verbs_es.yaml"
            
            if real_path.exists():
                with open(real_path, 'r', encoding='utf-8') as f:
                    return yaml.safe_load(f)
        except Exception as e:
            print(f"Warning: Could not load router config: {e}")
        
        # Default fallback
        return {
            "mission_run_verbs": ["abre", "entra", "ve", "pon", "lanza", "inicia", "ejecuta"],
            "mission_run_phrases": ["abre mision", "ejecuta la mision"]
        }

    def _normalize(self, text: str) -> str:
        text = text.lower().strip()
        # Remove wakewords
        text = re.sub(r'^(arthur|arturo|artur)\s+', '', text)
        # Remove special chars but keep spaces
        text = re.sub(r'[^\w\s]', '', text)
        return text.strip()

    def _calculate_similarity(self, a: str, b: str) -> float:
        return SequenceMatcher(None, a, b).ratio()

    def route(self, text: str, ctx: dict = None) -> RouteDecision:
        normalized_text = self._normalize(text)
        
        # 1. Control Commands (Highest Priority)
        control_decision = self._check_control(normalized_text)
        if control_decision:
            return control_decision

        # 2. Scroll / Navigation (High Priority)
        scroll_decision = self._check_scroll(normalized_text)
        if scroll_decision:
            return scroll_decision

        # 3. Mission Admin
        # Expanded keywords for robustness
        msg = normalized_text
        if any(x in msg for x in ["lista misiones", "listar misiones", "ver misiones", "mis misiones"]):
            return RouteDecision(RouteType.MISSIONS_ADMIN, 1.0, target="list")
            
        if any(x in msg for x in ["grabar mision", "graba mision", "nueva mision", "inicia grabacion", "empezar grabacion", "iniciar grabacion"]):
             return RouteDecision(RouteType.MISSIONS_ADMIN, 1.0, target="record")
             
        if any(x in msg for x in [
            "guardar mision", "guarda mision",
            "guarda la mision", "guarda esta mision",
            "guardar esta mision", "salvar mision", "guardar grabacion",
        ]):
             return RouteDecision(RouteType.MISSIONS_ADMIN, 1.0, target="save")

        # 4. Mission Run (Fuzzy Match)
        mission_decision = self._check_mission_run(normalized_text)
        if mission_decision:
            return mission_decision

        # 5. Skill/Playbook Run
        skill_decision = self._check_skill_run(normalized_text)
        if skill_decision:
            return skill_decision

        # 6. LLM Fallback (Lowest)
        return RouteDecision(RouteType.LLM, 1.0)

    def _check_control(self, text: str) -> Optional[RouteDecision]:
        if text.startswith("stop") or text == "detente" or text == "para":
            return RouteDecision(RouteType.CONTROL, 1.0, target="STOP")
        if text.startswith("sleep") or text == "duermete":
            return RouteDecision(RouteType.CONTROL, 1.0, target="SLEEP")
        if text.startswith("close") or text == "cierra arthuros":
            return RouteDecision(RouteType.CONTROL, 1.0, target="CLOSE")
        return None

    def _check_scroll(self, text: str) -> Optional[RouteDecision]:
        """
        Detecta intenciones de scroll:
        - baja, sube, desliza, desplázate, scroll, rueda, avanza, retrocede
        - "modo lectura", "leer cómodamente"
        """
        # Keywords
        scroll_verbs = ["baja", "sube", "desliza", "desplaza", "scroll", "rueda", "avanza", "retrocede", "continua", "sigue"]
        reading_mode_phrases = ["modo lectura", "leer comodamente", "lectura automatica"]
        
        # Check reading mode first
        for phrase in reading_mode_phrases:
            if phrase in text:
                return RouteDecision(RouteType.SCROLL, 1.0, target="reading_mode", 
                                     metadata={"mode": "reading", "speed": "slow"})

        # Check scroll commands
        words = text.split()
        if not words: return None
        
        action = None
        direction = "down" # default
        
        if any(v in text for v in scroll_verbs) or "barra desplazadora" in text:
            # Determine direction
            if "sube" in text or "arriba" in text or "retrocede" in text:
                direction = "up"
            
            # Determine "amount" / "unit"
            unit = "page" # default reasonable amount
            amount = 0.8  # slightly less than full page to keep context
            
            if "poco" in text or "poquito" in text:
                unit = "px"
                amount = 300
            elif "pantalla" in text or "pagina" in text:
                if "2" in text or "dos" in text: amount = 2
                elif "3" in text or "tres" in text: amount = 3
                else: amount = 1
                unit = "page"
            elif "inicio" in text or "principio" in text:
                unit = "percentage"
                amount = 0 # scroll to top
                direction = "absolute"
            elif "final" in text or "fin" in text or "fondo" in text:
                unit = "percentage"
                amount = 100 # scroll to bottom
                direction = "absolute"
            
            # Speed
            speed = "normal"
            if "lento" in text or "despacio" in text: speed = "slow"
            if "rapido" in text: speed = "fast"
            
            return RouteDecision(RouteType.SCROLL, 0.95, target="scroll_action",
                                 metadata={
                                     "direction": direction,
                                     "unit": unit,
                                     "amount": amount,
                                     "speed": speed
                                 })
                                 
        return None

    def _check_mission_run(self, text: str) -> Optional[RouteDecision]:
        # Extract potential mission name by removing verbs
        verbs = self.config.get("mission_run_verbs", [])
        phrases = self.config.get("mission_run_phrases", [])
        
        # Check specific phrases first
        cleaned_text = text
        for phrase in phrases:
            if text.startswith(phrase):
                cleaned_text = text[len(phrase):].strip()
                break
        else:
            # Check individual verbs
            words = text.split()
            if words and words[0] in verbs:
                cleaned_text = " ".join(words[1:]).strip()

        if not cleaned_text:
            return None

        # Fuzzy match against stored missions
        missions = self.mission_store.list_all()
        best_match = None
        best_score = 0.0

        for mission in missions:
            # Check name
            score = self._calculate_similarity(cleaned_text, mission.name.lower())
            if score > best_score:
                best_score = score
                best_match = mission

            # Check aliases
            if hasattr(mission, 'aliases'):
                for alias in mission.aliases:
                    score = self._calculate_similarity(cleaned_text, alias.lower())
                    if score > best_score:
                        best_score = score
                        best_match = mission

        # Thresholds
        if best_score >= 0.88:
             return RouteDecision(RouteType.MISSION_RUN, best_score, target=best_match.name)
        elif best_score >= 0.75:
            # Ambiguous / Low confidence - return it but logic will need to ask user
             return RouteDecision(RouteType.MISSION_RUN, best_score, target=best_match.name, 
                                  metadata={"ambiguous": True, "suggestion": best_match.name})
        
        return None

    def _check_skill_run(self, text: str) -> Optional[RouteDecision]:
        # Basic heuristic for playbooks/skills
        # e.g. "busca X en google", "reproduce Y en youtube"
        if "google" in text and "busca" in text:
            return RouteDecision(RouteType.SKILL_RUN, 0.9, target="google_search", metadata={"query": text})
        if ("youtube" in text or "cancion" in text) and ("reproduce" in text or "pon" in text):
             return RouteDecision(RouteType.SKILL_RUN, 0.9, target="youtube_play", metadata={"query": text})
        return None
