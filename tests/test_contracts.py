import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from operation_planning.contracts import OperationRequest, PlanState, TimeWindow


class PlanningContractTests(unittest.TestCase):
    def test_request_retains_confirmed_state(self):
        window = TimeWindow(datetime(2026, 10, 21, tzinfo=timezone.utc), datetime(2026, 10, 21, 2, tzinfo=timezone.utc))
        request = OperationRequest("OP-2", "shuttle", window, 50, PlanState.CONFIRMED)
        self.assertEqual(request.state.value, "confirmed")

    def test_priority_range_is_checked(self):
        window = TimeWindow(datetime(2026, 10, 21, tzinfo=timezone.utc), datetime(2026, 10, 21, 1, tzinfo=timezone.utc))
        with self.assertRaises(ValueError):
            OperationRequest("OP-3", "test", window, 120)


if __name__ == "__main__":
    unittest.main()
