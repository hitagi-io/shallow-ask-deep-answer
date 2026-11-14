"""Agent 与生成执行器共享的最终结果；提示文案不承担状态协议。"""
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


ResultKind = Literal["answered", "clarification", "no_evidence", "answer_rejected", "dependency_failed"]


class AnswerOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    result_kind: ResultKind
    error_code: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_error(self) -> Self:
        failed = self.result_kind in ("answer_rejected", "dependency_failed")
        if failed != (self.error_code is not None):
            raise ValueError("失败结果必须带错误码，正常结果不能带错误码")
        return self

    @property
    def failed(self) -> bool:
        return self.error_code is not None
