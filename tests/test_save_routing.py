import unittest
from app.brain.command_router import CommandRouter, RouteType

class TestMissionSavingRouting(unittest.TestCase):
    def setUp(self):
        self.router = CommandRouter()

    def test_save_mission_command(self):
        decision = self.router.route("guarda la mision")
        self.assertEqual(decision.route_type, RouteType.MISSIONS_ADMIN)
        self.assertEqual(decision.target, "save")

    def test_save_this_mission(self):
        decision = self.router.route("guarda esta mision")
        self.assertEqual(decision.route_type, RouteType.MISSIONS_ADMIN)
        self.assertEqual(decision.target, "save")

if __name__ == '__main__':
    unittest.main()
