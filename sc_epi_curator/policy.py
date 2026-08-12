"""Risk and budget policy for evidence recovery and acquisition."""

from __future__ import annotations

from .models import (
    ActionBudget,
    BudgetUsage,
    PolicyDecision,
    ProposedAction,
)


class BudgetManager:
    def __init__(self, limits: ActionBudget) -> None:
        self.limits = limits
        self.usage = BudgetUsage()

    def evaluate(self, action: ProposedAction) -> PolicyDecision:
        reasons: list[str] = []
        if self.usage.requests + action.request_cost > self.limits.max_requests:
            reasons.append("request budget exceeded")
        if self.usage.bytes + action.byte_cost > self.limits.max_bytes:
            reasons.append("byte budget exceeded")
        if (
            self.usage.recovery_rounds + action.recovery_rounds
            > self.limits.max_recovery_rounds
        ):
            reasons.append("recovery-round budget exceeded")

        return PolicyDecision(
            allowed=not reasons,
            requires_approval=(
                not reasons and action.byte_cost >= self.limits.approval_download_bytes
            ),
            reasons=tuple(reasons),
        )

    def commit(self, action: ProposedAction) -> None:
        decision = self.evaluate(action)
        if not decision.allowed:
            raise PermissionError("; ".join(decision.reasons))
        self.usage.requests += action.request_cost
        self.usage.bytes += action.byte_cost
        self.usage.recovery_rounds += action.recovery_rounds

