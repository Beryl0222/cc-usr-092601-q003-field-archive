"""领域错误类型。"""

from __future__ import annotations


class FieldArchiveError(Exception):
    """所有领域错误的基类。"""


class ContractViolation(FieldArchiveError):
    """事件信封不符合交换契约。"""


class VersionConflict(FieldArchiveError):
    """并发写入：聚合版本已被其他提交推进。"""


class ConflictError(FieldArchiveError):
    """业务冲突（已被他人认领、业务键指纹/授权范围不一致等）。"""


class CertificationError(FieldArchiveError):
    """培训资质缺失或已过期。"""


class ConsentError(FieldArchiveError):
    """授权依据不足或材料缺少监护授权/独立复核。"""


class ReviewError(FieldArchiveError):
    """复核状态不允许该操作。"""
