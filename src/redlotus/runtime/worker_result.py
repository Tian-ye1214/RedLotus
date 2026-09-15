from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SubagentResult(BaseModel):
    """Validated child output; absence of evidence must never imply success."""

    model_config = ConfigDict(str_strip_whitespace=True)

    status: Literal["success", "failed", "cancelled", "needs_input"] = Field(
        description="Outcome of the whole delegated goal. If a required step failed and remains unresolved, use failed; preserve successful substeps in summary.",
    )
    summary: str = Field(min_length=1)
    artifacts: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    needs_user_confirmation: bool = False

    @property
    def success(self) -> bool:
        return self.status == "success"
