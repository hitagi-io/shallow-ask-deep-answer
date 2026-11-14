"""按已完成阶段及阶段内工作量计算文件进度，不按等待时间推测完成度。"""
from collections.abc import Callable
from typing import Literal, Self, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator


IndexStage = Literal["preparing", "parsing", "splitting", "embedding", "writing", "verifying", "finalizing"]
INDEX_STAGES: tuple[IndexStage, ...] = get_args(IndexStage)
# 传输与七个入库阶段等权；百分比表示阶段完成度，不代表耗时比例。
FILE_PROGRESS_STAGE_COUNT = len(INDEX_STAGES) + 1


class IndexProgress(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    stage: IndexStage
    completed: int = Field(ge=0, strict=True)
    total: int = Field(gt=0, strict=True)

    @model_validator(mode="after")
    def check_completed(self) -> Self:
        if self.completed > self.total:
            raise ValueError("已处理数量不能超过总数量")
        return self

    @property
    def percent(self) -> int:
        offset = INDEX_STAGES.index(self.stage) + 1
        # 最后一次完成确认由任务状态提交，处理中始终小于 100%。
        return min(99, (offset * self.total + self.completed) * 100 // (FILE_PROGRESS_STAGE_COUNT * self.total))


ProgressCallback = Callable[[IndexProgress], None]
UnitProgressCallback = Callable[[int, int], None]
