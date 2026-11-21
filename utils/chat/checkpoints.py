"""每次生成使用私有 LangGraph 检查点，仅在业务终态事务中发布会话的新状态。"""
from collections import defaultdict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import Connection, OperationalError


class CheckpointPublicationError(RuntimeError):
    """发布失败时，终态事务必须回滚，允许执行器另存一次失败结果。"""


def _copy_checkpoint(target: BaseCheckpointSaver, parent: RunnableConfig, saved: CheckpointTuple) -> None:
    checkpoint = saved.checkpoint
    config = target.put(parent, checkpoint, saved.metadata, checkpoint["channel_versions"])
    # put_writes 按任务内的序号保存同一节点的多个写入，必须按原顺序一起复制。
    by_task = defaultdict(list)
    for task, channel, value in saved.pending_writes or ():
        by_task[task].append((channel, value))
    for task, writes in by_task.items():
        target.put_writes(config, writes, task)


class AttemptCheckpoints:
    def __init__(self, thread_id: str, attempt_id: str):
        self.attempt_id = attempt_id
        self.config: RunnableConfig = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
        self._parent = self.config
        self.saver = InMemorySaver()

    def load(self, source: PostgresSaver) -> None:
        saved = source.get_tuple(self.config)
        if saved is not None:
            _copy_checkpoint(self.saver, self.config, saved)
            self._parent = saved.config

    def publish(self, conn: Connection) -> None:
        """调用方在此连接上持有本次有效生成的行锁，并负责提交或回滚。"""
        try:
            saved = self.saver.get_tuple(self.config)
            if saved is None or saved.config == self._parent:
                raise ValueError("生成结束时缺少本次会话检查点")
            _copy_checkpoint(PostgresSaver(conn), self._parent, saved)
        except OperationalError:
            raise  # 连接故障由执行器重试整个终态事务。
        except Exception as exc:
            raise CheckpointPublicationError("无法发布本次会话检查点") from exc
