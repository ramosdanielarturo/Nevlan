import unittest
from app.brain.command_router import CommandRouter, RouteType, RouteDecision

# Mock store
class MockMission:
    def __init__(self, name, aliases=None):
        self.name = name
        self.aliases = aliases or []

class MockStore:
    def list_all(self):
        return [
            MockMission("Cinemex Pedregal", aliases=["pedregal cinemex"]),
            MockMission("Reporte Semanal")
        ]

class TestCommandRouter(unittest.TestCase):
    def setUp(self):
        self.router = CommandRouter()
        self.router.mission_store = MockStore()

    def test_routing_mission_exact(self):
        decision = self.router.route("abre cinemex pedregal")
        self.assertEqual(decision.route_type, RouteType.MISSION_RUN)
        self.assertEqual(decision.target, "Cinemex Pedregal")

    def test_routing_mission_alias(self):
        decision = self.router.route("ejecuta pedregal cinemex")
        self.assertEqual(decision.route_type, RouteType.MISSION_RUN)
        self.assertEqual(decision.target, "Cinemex Pedregal")

    def test_control_stop(self):
        decision = self.router.route("stop mission")
        self.assertEqual(decision.route_type, RouteType.CONTROL)
        self.assertEqual(decision.target, "STOP")

    def test_skill_google(self):
        decision = self.router.route("busca algo en google")
        self.assertEqual(decision.route_type, RouteType.SKILL_RUN)
        self.assertEqual(decision.target, "google_search")

    def test_llm_fallback(self):
        decision = self.router.route("cuentame un chiste")
        self.assertEqual(decision.route_type, RouteType.LLM)

if __name__ == '__main__':
    unittest.main()
