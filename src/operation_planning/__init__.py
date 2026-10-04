"""示范运营资源编排后端。

公开入口：
* :class:`~operation_planning.service.PlanningService` —— 业务门面；
* :class:`~operation_planning.repository.Repository` —— SQLite 持久化；
* :func:`~operation_planning.api.create_server` —— HTTP JSON 接口。
"""

from .domain import (
    Assignment,
    BlockedWindow,
    ChargingBay,
    ChargingSession,
    Interval,
    RouteSegment,
    SafetyOfficer,
    TaskDemand,
    Vehicle,
)
from .engine import PlanResult, Problem, Scheduler, solve
from .errors import (
    ConflictError,
    LeaseError,
    NotFoundError,
    PlanningError,
    StateError,
    ValidationError,
)
from .repository import Repository
from .service import PlanningService

__all__ = [
    "Assignment",
    "BlockedWindow",
    "ChargingBay",
    "ChargingSession",
    "Interval",
    "RouteSegment",
    "SafetyOfficer",
    "TaskDemand",
    "Vehicle",
    "PlanResult",
    "Problem",
    "Scheduler",
    "solve",
    "ConflictError",
    "LeaseError",
    "NotFoundError",
    "PlanningError",
    "StateError",
    "ValidationError",
    "Repository",
    "PlanningService",
]
