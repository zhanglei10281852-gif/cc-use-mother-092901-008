import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from operation_planning.contracts import OperationRequest, PlanState, TimeWindow


window = TimeWindow(datetime(2026, 10, 21, 8, tzinfo=timezone.utc), datetime(2026, 10, 21, 9, tzinfo=timezone.utc))
request = OperationRequest("OP-1", "public-demo", window, 90, PlanState.CANDIDATE)
print(json.dumps({"request": request.request_id, "priority": request.priority, "state": request.state.value}, ensure_ascii=False))
