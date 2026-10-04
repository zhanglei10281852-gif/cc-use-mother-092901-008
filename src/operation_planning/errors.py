"""服务层异常类型。"""


class PlanningError(Exception):
    """所有业务错误的基类。"""


class NotFoundError(PlanningError):
    """需求或方案不存在。"""


class ValidationError(PlanningError):
    """输入不满足领域约束。"""


class ConflictError(PlanningError):
    """并发确认或资源占用冲突。"""


class LeaseError(PlanningError):
    """租约缺失、过期或持有者不匹配。"""


class StateError(PlanningError):
    """对象当前状态不允许该操作（例如行程已开始）。"""
