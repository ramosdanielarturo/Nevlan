import unittest
from app.brain.command_router import CommandRouter, RouteType

class TestScrollRouting(unittest.TestCase):
    def setUp(self):
        self.router = CommandRouter()

    def test_scroll_down_simple(self):
        decision = self.router.route("baja")
        self.assertEqual(decision.route_type, RouteType.SCROLL)
        self.assertEqual(decision.metadata["direction"], "down")
        self.assertEqual(decision.metadata["unit"], "page")

    def test_scroll_up_fast(self):
        decision = self.router.route("sube rapido")
        self.assertEqual(decision.route_type, RouteType.SCROLL)
        self.assertEqual(decision.metadata["direction"], "up")
        self.assertEqual(decision.metadata["speed"], "fast")

    def test_scroll_little(self):
        decision = self.router.route("baja un poco")
        self.assertEqual(decision.route_type, RouteType.SCROLL)
        self.assertEqual(decision.metadata["unit"], "px")
        self.assertEqual(decision.metadata["amount"], 300)

    def test_reading_mode(self):
        decision = self.router.route("activar modo lectura")
        self.assertEqual(decision.route_type, RouteType.SCROLL)
        self.assertEqual(decision.target, "reading_mode")
        self.assertEqual(decision.metadata["speed"], "slow")

    def test_scroll_multiple_pages(self):
        decision = self.router.route("desliza 2 pantallas")
        self.assertEqual(decision.route_type, RouteType.SCROLL)
        self.assertEqual(decision.metadata["amount"], 2)
        self.assertEqual(decision.metadata["unit"], "page")

if __name__ == '__main__':
    unittest.main()
