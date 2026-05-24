from typing import Optional, Tuple
from app.services.missions.store import mission_store
from difflib import SequenceMatcher

class MissionMatcher:
    def match(self, text: str, threshold: float = 0.8) -> Tuple[Optional[str], float]:
        """
        Returns (mission_name, confidence_score)
        """
        missions = mission_store.list_all()
        best_mission = None
        best_score = 0.0
        
        text_lower = text.lower()
        
        for mission in missions:
            # Check name
            score = SequenceMatcher(None, text_lower, mission.name.lower()).ratio()
            if score > best_score:
                best_score = score
                best_mission = mission.name
                
            # Check aliases
            if hasattr(mission, 'aliases') and mission.aliases:
                for alias in mission.aliases:
                    alias_score = SequenceMatcher(None, text_lower, alias.lower()).ratio()
                    if alias_score > best_score:
                        best_score = alias_score
                        best_mission = mission.name
        
        if best_score >= threshold:
            return best_mission, best_score
            
        return None, best_score
