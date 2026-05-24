from typing import Optional, Dict, Any

class SkillMatcher:
    def match(self, intent: str, entities: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        # This acts as a secondary router for specific hardcoded skills/playbooks
        # that are not full "Missions".
        
        intent = intent.lower()
        
        if "google" in intent:
             query = entities.get("query") or entities.get("text")
             if query:
                 return {
                     "skill": "web_search",
                     "provider": "google",
                     "args": {"query": query}
                 }
                 
        if "youtube" in intent:
            query = entities.get("query") or entities.get("song") or entities.get("text")
            if query:
                return {
                    "skill": "media_play",
                    "provider": "youtube", 
                    "args": {"query": query}
                }
                
        return None
