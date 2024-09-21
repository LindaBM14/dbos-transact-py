import asyncio
import datetime
import os
import threading
import time
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Sequence, Set, TypedDict, cast

import psycopg
import sqlalchemy as sa
import sqlalchemy.dialects.postgresql as pg
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

import dbos.utils as utils
from dbos.error import DBOSNonExistentWorkflowError, DBOSWorkflowConflictIDError

from .dbos_config import ConfigFile
from .logger import dbos_logger
from .schemas.system_database import SystemSchema


class WorkflowStatusString(Enum):
    """Enumeration of values allowed for `WorkflowSatusInternal.status`."""

    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    ERROR = "ERROR"
    RETRIES_EXCEEDED = "RETRIES_EXCEEDED"
    CANCELLED = "CANCELLED"
    ENQUEUED = "ENQUEUED"


WorkflowStatuses = Literal[
    "PENDING", "SUCCESS", "ERROR", "RETRIES_EXCEEDED", "CANCELLED", "ENQUEUED"
]


class WorkflowInputs(TypedDict):
    args: Any
    kwargs: Any


class WorkflowStatusInternal(TypedDict):
    workflow_uuid: str
    status: WorkflowStatuses
    name: str
    class_name: Optional[str]
    config_name: Optional[str]
    output: Optional[str]  # JSON (jsonpickle)
    error: Optional[str]  # JSON (jsonpickle)
    executor_id: Optional[str]
    app_version: Optional[str]
    app_id: Optional[str]
    request: Optional[str]  # JSON (jsonpickle)
    recovery_attempts: Optional[int]
    authenticated_user: Optional[str]
    assumed_role: Optional[str]
    authenticated_roles: Optional[str]  # JSON list of roles.
    queue_name: Optional[str]


class RecordedResult(TypedDict):
    output: Optional[str]  # JSON (jsonpickle)
    error: Optional[str]  # JSON (jsonpickle)


class OperationResultInternal(TypedDict):
    workflow_uuid: str
    function_id: int
    output: Optional[str]  # JSON (jsonpickle)
    error: Optional[str]  # JSON (jsonpickle)


class GetEventWorkflowContext(TypedDict):
    workflow_uuid: str
    function_id: int
    timeout_function_id: int


class GetWorkflowsInput:
    """
    Structure for argument to `get_workflows` function.

    This specifies the search criteria for workflow retrieval by `get_workflows`.

    Attributes:
       name(str):  The name of the workflow function
       authenticated_user(str):  The name of the user who invoked the function
       start_time(str): Beginning of search range for time of invocation, in ISO 8601 format
       end_time(str): End of search range for time of invocation, in ISO 8601 format
       status(str): Current status of the workflow invocation (see `WorkflowStatusString`)
       application_version(str): Application version that invoked the workflow
       limit(int): Limit on number of returned records

    """

    def __init__(self) -> None:
        self.name: Optional[str] = None  # The name of the workflow function
        self.authenticated_user: Optional[str] = None  # The user who ran the workflow.
        self.start_time: Optional[str] = None  # Timestamp in ISO 8601 format
        self.end_time: Optional[str] = None  # Timestamp in ISO 8601 format
        self.status: Optional[WorkflowStatuses] = None
        self.application_version: Optional[str] = (
            None  # The application version that ran this workflow. = None
        )
        self.limit: Optional[int] = (
            None  # Return up to this many workflows IDs. IDs are ordered by workflow creation time.
        )


class GetWorkflowsOutput:
    def __init__(self, workflow_uuids: List[str]):
        self.workflow_uuids = workflow_uuids


class WorkflowInformation(TypedDict, total=False):
    workflow_uuid: str
    status: WorkflowStatuses  # The status of the workflow.
    name: str  # The name of the workflow function.
    workflow_class_name: str  # The class name holding the workflow function.
    workflow_config_name: (
        str  # The name of the configuration, if the class needs configuration
    )
    authenticated_user: str  # The user who ran the workflow. Empty string if not set.
    assumed_role: str
    # The role used to run this workflow.  Empty string if authorization is not required.
    authenticated_roles: List[str]
    # All roles the authenticated user has, if any.
    input: Optional[WorkflowInputs]
    output: Optional[str]
    error: Optional[str]
    request: Optional[str]


dbos_null_topic = "__null__topic__"
buffer_flush_batch_size = 100
buffer_flush_interval_secs = 1.0


class SystemDatabase:

    def __init__(self, config: ConfigFile):
        self.config = config

        sysdb_name = (
            config["database"]["sys_db_name"]
            if "sys_db_name" in config["database"] and config["database"]["sys_db_name"]
            else config["database"]["app_db_name"] + SystemSchema.sysdb_suffix
        )

        # If the system database does not already exist, create it
        postgres_db_url = sa.URL.create(
            "postgresql+psycopg",
            username=config["database"]["username"],
            password=config["database"]["password"],
            host=config["database"]["hostname"],
            port=config["database"]["port"],
            database="postgres",
        )
        engine = sa.create_engine(postgres_db_url)
        with engine.connect() as conn:
            conn.execution_options(isolation_level="AUTOCOMMIT")
            if not conn.execute(
                sa.text("SELECT 1 FROM pg_database WHERE datname=:db_name"),
                parameters={"db_name": sysdb_name},
            ).scalar():
                conn.execute(sa.text(f"CREATE DATABASE {sysdb_name}"))
        engine.dispose()

        system_db_url = sa.URL.create(
            "postgresql+psycopg",
            username=config["database"]["username"],
            password=config["database"]["password"],
            host=config["database"]["hostname"],
            port=config["database"]["port"],
            database=sysdb_name,
        )

        # Run a schema migration for the system database
        migration_dir = os.path.join(
            os.path.dirname(os.path.realpath(__file__)), "migrations"
        )
        alembic_cfg = Config()
        alembic_cfg.set_main_option("script_location", migration_dir)
        alembic_cfg.set_main_option(
            "sqlalchemy.url", system_db_url.render_as_string(hide_password=False)
        )
        command.upgrade(alembic_cfg, "head")

        self.notification_conn: Optional[psycopg.connection.Connection] = None
        self.notifications_map: Dict[str, threading.Condition] = {}
        self.workflow_events_map: Dict[str, threading.Condition] = {}

        # Initialize the workflow status and inputs buffers
        self._workflow_status_buffer: Dict[str, WorkflowStatusInternal] = {}
        self._workflow_inputs_buffer: Dict[str, str] = {}
        # Two sets for tracking which single-transaction workflows have been exported to the status table
        self._exported_temp_txn_wf_status: Set[str] = set()
        self._temp_txn_wf_ids: Set[str] = set()
        self._is_flushing_status_buffer = False

        # Now we can run background processes
        self._run_background_processes = True

        # Create a connection pool for the system database
        self.engine = sa.create_engine(
            system_db_url, pool_size=20, max_overflow=5, pool_timeout=30
        )
        self.async_engine = create_async_engine(
            system_db_url, pool_size=20, max_overflow=5, pool_timeout=30
        )

    # Destroy the pool when finished
    def destroy(self) -> None:
        self.wait_for_buffer_flush()
        self._run_background_processes = False
        if self.notification_conn is not None:
            self.notification_conn.close()
        self.engine.dispose()
        asyncio.run(self.async_engine.dispose())

    def wait_for_buffer_flush(self) -> None:
        # Wait until the buffers are flushed.
        while self._is_flushing_status_buffer or not self._is_buffers_empty:
            dbos_logger.debug("Waiting for system buffers to be exported")
            time.sleep(1)

    async def update_workflow_status(
        self,
        status: WorkflowStatusInternal,
        replace: bool = True,
        in_recovery: bool = False,
        conn: Optional[AsyncConnection] = None,
    ) -> None:
        cmd = pg.insert(SystemSchema.workflow_status).values(
            workflow_uuid=status["workflow_uuid"],
            status=status["status"],
            name=status["name"],
            class_name=status["class_name"],
            config_name=status["config_name"],
            output=status["output"],
            error=status["error"],
            executor_id=status["executor_id"],
            application_version=status["app_version"],
            application_id=status["app_id"],
            request=status["request"],
            authenticated_user=status["authenticated_user"],
            authenticated_roles=status["authenticated_roles"],
            assumed_role=status["assumed_role"],
            queue_name=status["queue_name"],
        )
        if replace:
            cmd = cmd.on_conflict_do_update(
                index_elements=["workflow_uuid"],
                set_=dict(
                    status=status["status"],
                    output=status["output"],
                    error=status["error"],
                ),
            )
        elif in_recovery:
            cmd = cmd.on_conflict_do_update(
                index_elements=["workflow_uuid"],
                set_=dict(
                    recovery_attempts=SystemSchema.workflow_status.c.recovery_attempts
                    + 1,
                ),
            )
        else:
            cmd = cmd.on_conflict_do_nothing()

        if conn is not None:
            await conn.execute(cmd)
        else:
            async with self.async_engine.begin() as c:
                await c.execute(cmd)

        # Record we have exported status for this single-transaction workflow
        if status["workflow_uuid"] in self._temp_txn_wf_ids:
            self._exported_temp_txn_wf_status.add(status["workflow_uuid"])

    async def set_workflow_status(
        self,
        workflow_uuid: str,
        status: WorkflowStatusString,
        reset_recovery_attempts: bool,
    ) -> None:
        async with self.async_engine.begin() as c:
            stmt = (
                sa.update(SystemSchema.workflow_status)
                .where(SystemSchema.workflow_inputs.c.workflow_uuid == workflow_uuid)
                .values(
                    status=status,
                )
            )
            await c.execute(stmt)

            if reset_recovery_attempts:
                stmt = (
                    sa.update(SystemSchema.workflow_status)
                    .where(
                        SystemSchema.workflow_inputs.c.workflow_uuid == workflow_uuid
                    )
                    .values(recovery_attempts=reset_recovery_attempts)
                )
                await c.execute(stmt)

    async def get_workflow_status(
        self, workflow_uuid: str
    ) -> Optional[WorkflowStatusInternal]:
        async with self.async_engine.begin() as c:
            row = (
                await c.execute(
                    sa.select(
                        SystemSchema.workflow_status.c.status,
                        SystemSchema.workflow_status.c.name,
                        SystemSchema.workflow_status.c.request,
                        SystemSchema.workflow_status.c.recovery_attempts,
                        SystemSchema.workflow_status.c.config_name,
                        SystemSchema.workflow_status.c.class_name,
                        SystemSchema.workflow_status.c.authenticated_user,
                        SystemSchema.workflow_status.c.authenticated_roles,
                        SystemSchema.workflow_status.c.assumed_role,
                        SystemSchema.workflow_status.c.queue_name,
                    ).where(
                        SystemSchema.workflow_status.c.workflow_uuid == workflow_uuid
                    )
                )
            ).fetchone()
            if row is None:
                return None
            status: WorkflowStatusInternal = {
                "workflow_uuid": workflow_uuid,
                "status": row[0],
                "name": row[1],
                "class_name": row[5],
                "config_name": row[4],
                "output": None,
                "error": None,
                "app_id": None,
                "app_version": None,
                "executor_id": None,
                "request": row[2],
                "recovery_attempts": row[3],
                "authenticated_user": row[6],
                "authenticated_roles": row[7],
                "assumed_role": row[8],
                "queue_name": row[9],
            }
            return status

    async def get_workflow_status_within_wf(
        self, workflow_uuid: str, calling_wf: str, calling_wf_fn: int
    ) -> Optional[WorkflowStatusInternal]:
        res = await self.check_operation_execution(calling_wf, calling_wf_fn)
        if res is not None:
            if res["output"]:
                resstat: WorkflowStatusInternal = utils.deserialize(res["output"])
                return resstat
            return None
        stat = await self.get_workflow_status(workflow_uuid)
        await self.record_operation_result(
            {
                "workflow_uuid": calling_wf,
                "function_id": calling_wf_fn,
                "output": utils.serialize(stat),
                "error": None,
            }
        )
        return stat

    async def get_workflow_status_w_outputs(
        self, workflow_uuid: str
    ) -> Optional[WorkflowStatusInternal]:
        async with self.async_engine.begin() as c:
            row = (
                await c.execute(
                    sa.select(
                        SystemSchema.workflow_status.c.status,
                        SystemSchema.workflow_status.c.name,
                        SystemSchema.workflow_status.c.request,
                        SystemSchema.workflow_status.c.output,
                        SystemSchema.workflow_status.c.error,
                        SystemSchema.workflow_status.c.config_name,
                        SystemSchema.workflow_status.c.class_name,
                        SystemSchema.workflow_status.c.authenticated_user,
                        SystemSchema.workflow_status.c.authenticated_roles,
                        SystemSchema.workflow_status.c.assumed_role,
                        SystemSchema.workflow_status.c.queue_name,
                    ).where(
                        SystemSchema.workflow_status.c.workflow_uuid == workflow_uuid
                    )
                )
            ).fetchone()
            if row is None:
                return None
            status: WorkflowStatusInternal = {
                "workflow_uuid": workflow_uuid,
                "status": row[0],
                "name": row[1],
                "config_name": row[5],
                "class_name": row[6],
                "output": row[3],
                "error": row[4],
                "app_id": None,
                "app_version": None,
                "executor_id": None,
                "request": row[2],
                "recovery_attempts": None,
                "authenticated_user": row[7],
                "authenticated_roles": row[8],
                "assumed_role": row[9],
                "queue_name": row[10],
            }
            return status

    async def await_workflow_result_internal(
        self, workflow_uuid: str
    ) -> dict[str, Any]:
        polling_interval_secs: float = 1.000

        while True:
            async with self.async_engine.begin() as c:
                row = (
                    await c.execute(
                        sa.select(
                            SystemSchema.workflow_status.c.status,
                            SystemSchema.workflow_status.c.output,
                            SystemSchema.workflow_status.c.error,
                        ).where(
                            SystemSchema.workflow_status.c.workflow_uuid
                            == workflow_uuid
                        )
                    )
                ).fetchone()
                if row is not None:
                    status = row[0]
                    if status == str(WorkflowStatusString.SUCCESS.value):
                        return {
                            "status": status,
                            "output": row[1],
                            "workflow_uuid": workflow_uuid,
                        }

                    elif status == str(WorkflowStatusString.ERROR.value):
                        return {
                            "status": status,
                            "error": row[2],
                            "workflow_uuid": workflow_uuid,
                        }

                else:
                    pass  # CB: I guess we're assuming the WF will show up eventually.

            time.sleep(polling_interval_secs)

    async def await_workflow_result(self, workflow_uuid: str) -> Any:
        stat = await self.await_workflow_result_internal(workflow_uuid)
        if not stat:
            return None
        status: str = stat["status"]
        if status == str(WorkflowStatusString.SUCCESS.value):
            return utils.deserialize(stat["output"])
        elif status == str(WorkflowStatusString.ERROR.value):
            raise utils.deserialize(stat["error"])
        return None

    async def get_workflow_info(
        self, workflow_uuid: str, get_request: bool
    ) -> Optional[WorkflowInformation]:
        stat = await self.get_workflow_status_w_outputs(workflow_uuid)
        if stat is None:
            return None
        info = cast(WorkflowInformation, stat)
        input = await self.get_workflow_inputs(workflow_uuid)
        if input is not None:
            info["input"] = input
        if not get_request:
            info.pop("request", None)

        return info

    async def update_workflow_inputs(
        self, workflow_uuid: str, inputs: str, conn: Optional[AsyncConnection] = None
    ) -> None:
        cmd = (
            pg.insert(SystemSchema.workflow_inputs)
            .values(
                workflow_uuid=workflow_uuid,
                inputs=inputs,
            )
            .on_conflict_do_nothing()
        )
        if conn is not None:
            await conn.execute(cmd)
        else:
            async with self.async_engine.begin() as c:
                await c.execute(cmd)

        if workflow_uuid in self._temp_txn_wf_ids:
            # Clean up the single-transaction tracking sets
            self._exported_temp_txn_wf_status.discard(workflow_uuid)
            self._temp_txn_wf_ids.discard(workflow_uuid)

    async def get_workflow_inputs(self, workflow_uuid: str) -> Optional[WorkflowInputs]:
        async with self.async_engine.begin() as c:
            row = (
                await c.execute(
                    sa.select(SystemSchema.workflow_inputs.c.inputs).where(
                        SystemSchema.workflow_inputs.c.workflow_uuid == workflow_uuid
                    )
                )
            ).fetchone()
            if row is None:
                return None
            inputs: WorkflowInputs = utils.deserialize(row[0])
            return inputs

    async def get_workflows(self, input: GetWorkflowsInput) -> GetWorkflowsOutput:
        query = sa.select(SystemSchema.workflow_status.c.workflow_uuid).order_by(
            SystemSchema.workflow_status.c.created_at.desc()
        )

        if input.name:
            query = query.where(SystemSchema.workflow_status.c.name == input.name)
        if input.authenticated_user:
            query = query.where(
                SystemSchema.workflow_status.c.authenticated_user
                == input.authenticated_user
            )
        if input.start_time:
            query = query.where(
                SystemSchema.workflow_status.c.created_at
                >= datetime.datetime.fromisoformat(input.start_time).timestamp()
            )
        if input.end_time:
            query = query.where(
                SystemSchema.workflow_status.c.created_at
                <= datetime.datetime.fromisoformat(input.end_time).timestamp()
            )
        if input.status:
            query = query.where(SystemSchema.workflow_status.c.status == input.status)
        if input.application_version:
            query = query.where(
                SystemSchema.workflow_status.c.application_version
                == input.application_version
            )
        if input.limit:
            query = query.limit(input.limit)

        async with self.async_engine.begin() as c:
            rows = await c.execute(query)
        workflow_uuids = [row[0] for row in rows]

        return GetWorkflowsOutput(workflow_uuids)

    async def get_pending_workflows(self, executor_id: str) -> list[str]:
        async with self.async_engine.begin() as c:
            rows = (
                await c.execute(
                    sa.select(SystemSchema.workflow_status.c.workflow_uuid).where(
                        SystemSchema.workflow_status.c.status
                        == WorkflowStatusString.PENDING.value,
                        SystemSchema.workflow_status.c.executor_id == executor_id,
                    )
                )
            ).fetchall()
            return [row[0] for row in rows]

    async def record_operation_result(
        self, result: OperationResultInternal, conn: Optional[AsyncConnection] = None
    ) -> None:
        error = result["error"]
        output = result["output"]
        assert error is None or output is None, "Only one of error or output can be set"
        sql = pg.insert(SystemSchema.operation_outputs).values(
            workflow_uuid=result["workflow_uuid"],
            function_id=result["function_id"],
            output=output,
            error=error,
        )
        try:
            if conn is not None:
                await conn.execute(sql)
            else:
                async with self.async_engine.begin() as c:
                    await c.execute(sql)
        except DBAPIError as dbapi_error:
            if dbapi_error.orig.sqlstate == "23505":  # type: ignore
                raise DBOSWorkflowConflictIDError(result["workflow_uuid"])
            raise

    async def check_operation_execution(
        self,
        workflow_uuid: str,
        function_id: int,
        conn: Optional[AsyncConnection] = None,
    ) -> Optional[RecordedResult]:
        sql = sa.select(
            SystemSchema.operation_outputs.c.output,
            SystemSchema.operation_outputs.c.error,
        ).where(
            SystemSchema.operation_outputs.c.workflow_uuid == workflow_uuid,
            SystemSchema.operation_outputs.c.function_id == function_id,
        )

        # If in a transaction, use the provided connection
        rows: Sequence[Any]
        if conn is not None:
            rows = (await conn.execute(sql)).all()
        else:
            async with self.async_engine.begin() as c:
                rows = (await c.execute(sql)).all()
        if len(rows) == 0:
            return None
        result: RecordedResult = {
            "output": rows[0][0],
            "error": rows[0][1],
        }
        return result

    async def send(
        self,
        workflow_uuid: str,
        function_id: int,
        destination_uuid: str,
        message: Any,
        topic: Optional[str] = None,
    ) -> None:
        topic = topic if topic is not None else dbos_null_topic
        async with self.async_engine.begin() as c:
            recorded_output = await self.check_operation_execution(
                workflow_uuid, function_id, conn=c
            )
            if recorded_output is not None:
                return  # Already sent before

            try:
                await c.execute(
                    pg.insert(SystemSchema.notifications).values(
                        destination_uuid=destination_uuid,
                        topic=topic,
                        message=utils.serialize(message),
                    )
                )
            except DBAPIError as dbapi_error:
                # Foreign key violation
                if dbapi_error.orig.sqlstate == "23503":  # type: ignore
                    raise DBOSNonExistentWorkflowError(destination_uuid)
                raise
            output: OperationResultInternal = {
                "workflow_uuid": workflow_uuid,
                "function_id": function_id,
                "output": None,
                "error": None,
            }
            await self.record_operation_result(output, conn=c)

    async def recv(
        self,
        workflow_uuid: str,
        function_id: int,
        timeout_function_id: int,
        topic: Optional[str],
        timeout_seconds: float = 60,
    ) -> Any:
        topic = topic if topic is not None else dbos_null_topic

        # First, check for previous executions.
        recorded_output = await self.check_operation_execution(
            workflow_uuid, function_id
        )
        if recorded_output is not None:
            if recorded_output["output"] is not None:
                return utils.deserialize(recorded_output["output"])
            else:
                raise Exception("No output recorded in the last recv")

        # Insert a condition to the notifications map, so the listener can notify it when a message is received.
        payload = f"{workflow_uuid}::{topic}"
        condition = threading.Condition()
        # Must acquire first before adding to the map. Otherwise, the notification listener may notify it before the condition is acquired and waited.
        condition.acquire()
        self.notifications_map[payload] = condition

        # Check if the key is already in the database. If not, wait for the notification.
        init_recv: Sequence[Any]
        async with self.async_engine.begin() as c:
            init_recv = (
                await c.execute(
                    sa.select(
                        SystemSchema.notifications.c.topic,
                    ).where(
                        SystemSchema.notifications.c.destination_uuid == workflow_uuid,
                        SystemSchema.notifications.c.topic == topic,
                    )
                )
            ).fetchall()

        if len(init_recv) == 0:
            # Wait for the notification
            # Support OAOO sleep
            actual_timeout = await self.sleep(
                workflow_uuid, timeout_function_id, timeout_seconds, skip_sleep=True
            )
            condition.wait(timeout=actual_timeout)
        condition.release()
        self.notifications_map.pop(payload)

        # Transactionally consume and return the message if it's in the database, otherwise return null.
        async with self.async_engine.begin() as c:
            oldest_entry_cte = (
                sa.select(
                    SystemSchema.notifications.c.destination_uuid,
                    SystemSchema.notifications.c.topic,
                    SystemSchema.notifications.c.message,
                    SystemSchema.notifications.c.created_at_epoch_ms,
                )
                .where(
                    SystemSchema.notifications.c.destination_uuid == workflow_uuid,
                    SystemSchema.notifications.c.topic == topic,
                )
                .order_by(SystemSchema.notifications.c.created_at_epoch_ms.asc())
                .limit(1)
                .cte("oldest_entry")
            )
            delete_stmt = (
                sa.delete(SystemSchema.notifications)
                .where(
                    SystemSchema.notifications.c.destination_uuid
                    == oldest_entry_cte.c.destination_uuid,
                    SystemSchema.notifications.c.topic == oldest_entry_cte.c.topic,
                    SystemSchema.notifications.c.created_at_epoch_ms
                    == oldest_entry_cte.c.created_at_epoch_ms,
                )
                .returning(SystemSchema.notifications.c.message)
            )
            rows = (await c.execute(delete_stmt)).fetchall()
            message: Any = None
            if len(rows) > 0:
                message = utils.deserialize(rows[0][0])
            await self.record_operation_result(
                {
                    "workflow_uuid": workflow_uuid,
                    "function_id": function_id,
                    "output": utils.serialize(
                        message
                    ),  # None will be serialized to 'null'
                    "error": None,
                },
                conn=c,
            )
        return message

    def _notification_listener(self) -> None:
        while self._run_background_processes:
            try:
                # since we're using the psycopg connection directly, we need a url without the "+pycopg" suffix
                url = sa.URL.create(
                    "postgresql", **self.engine.url.translate_connect_args()
                )
                # Listen to notifications
                self.notification_conn = psycopg.connect(
                    url.render_as_string(hide_password=False), autocommit=True
                )

                self.notification_conn.execute("LISTEN dbos_notifications_channel")
                self.notification_conn.execute("LISTEN dbos_workflow_events_channel")

                while self._run_background_processes:
                    gen = self.notification_conn.notifies(timeout=60)
                    for notify in gen:
                        channel = notify.channel
                        dbos_logger.debug(
                            f"Received notification on channel: {channel}, payload: {notify.payload}"
                        )
                        if channel == "dbos_notifications_channel":
                            if (
                                notify.payload
                                and notify.payload in self.notifications_map
                            ):
                                condition = self.notifications_map[notify.payload]
                                condition.acquire()
                                condition.notify_all()
                                condition.release()
                                dbos_logger.debug(
                                    f"Signaled notifications condition for {notify.payload}"
                                )
                        elif channel == "dbos_workflow_events_channel":
                            if (
                                notify.payload
                                and notify.payload in self.workflow_events_map
                            ):
                                condition = self.workflow_events_map[notify.payload]
                                condition.acquire()
                                condition.notify_all()
                                condition.release()
                                dbos_logger.debug(
                                    f"Signaled workflow_events condition for {notify.payload}"
                                )
                        else:
                            dbos_logger.error(f"Unknown channel: {channel}")
            except Exception as e:
                if self._run_background_processes:
                    dbos_logger.error(f"Notification listener error: {e}")
                    time.sleep(1)
                    # Then the loop will try to reconnect and restart the listener
            finally:
                if self.notification_conn is not None:
                    self.notification_conn.close()

    async def sleep(
        self,
        workflow_uuid: str,
        function_id: int,
        seconds: float,
        skip_sleep: bool = False,
    ) -> float:
        recorded_output = await self.check_operation_execution(
            workflow_uuid, function_id
        )
        end_time: float
        if recorded_output is not None:
            assert recorded_output["output"] is not None, "no recorded end time"
            end_time = utils.deserialize(recorded_output["output"])
        else:
            end_time = time.time() + seconds
            try:
                await self.record_operation_result(
                    {
                        "workflow_uuid": workflow_uuid,
                        "function_id": function_id,
                        "output": utils.serialize(end_time),
                        "error": None,
                    }
                )
            except DBOSWorkflowConflictIDError:
                pass
        duration = max(0, end_time - time.time())
        if not skip_sleep:
            time.sleep(duration)
        return duration

    async def set_event(
        self,
        workflow_uuid: str,
        function_id: int,
        key: str,
        message: Any,
    ) -> None:
        async with self.async_engine.begin() as c:
            recorded_output = await self.check_operation_execution(
                workflow_uuid, function_id, conn=c
            )
            if recorded_output is not None:
                return  # Already sent before

            await c.execute(
                pg.insert(SystemSchema.workflow_events)
                .values(
                    workflow_uuid=workflow_uuid,
                    key=key,
                    value=utils.serialize(message),
                )
                .on_conflict_do_update(
                    index_elements=["workflow_uuid", "key"],
                    set_={"value": utils.serialize(message)},
                )
            )
            output: OperationResultInternal = {
                "workflow_uuid": workflow_uuid,
                "function_id": function_id,
                "output": None,
                "error": None,
            }
            await self.record_operation_result(output, conn=c)

    async def get_event(
        self,
        target_uuid: str,
        key: str,
        timeout_seconds: float = 60,
        caller_ctx: Optional[GetEventWorkflowContext] = None,
    ) -> Any:
        get_sql = sa.select(
            SystemSchema.workflow_events.c.value,
        ).where(
            SystemSchema.workflow_events.c.workflow_uuid == target_uuid,
            SystemSchema.workflow_events.c.key == key,
        )
        # Check for previous executions only if it's in a workflow
        if caller_ctx is not None:
            recorded_output = await self.check_operation_execution(
                caller_ctx["workflow_uuid"], caller_ctx["function_id"]
            )
            if recorded_output is not None:
                if recorded_output["output"] is not None:
                    return utils.deserialize(recorded_output["output"])
                else:
                    raise Exception("No output recorded in the last get_event")

        payload = f"{target_uuid}::{key}"
        condition = threading.Condition()
        self.workflow_events_map[payload] = condition
        condition.acquire()

        # Check if the key is already in the database. If not, wait for the notification.
        init_recv: Sequence[Any]
        async with self.async_engine.begin() as c:
            init_recv = (await c.execute(get_sql)).fetchall()

        value: Any = None
        if len(init_recv) > 0:
            value = utils.deserialize(init_recv[0][0])
        else:
            # Wait for the notification
            actual_timeout = timeout_seconds
            if caller_ctx is not None:
                # Support OAOO sleep for workflows
                actual_timeout = await self.sleep(
                    caller_ctx["workflow_uuid"],
                    caller_ctx["timeout_function_id"],
                    timeout_seconds,
                    skip_sleep=True,
                )
            condition.wait(timeout=actual_timeout)

            # Read the value from the database
            async with self.async_engine.begin() as c:
                final_recv = (await c.execute(get_sql)).fetchall()
                if len(final_recv) > 0:
                    value = utils.deserialize(final_recv[0][0])
        condition.release()
        self.workflow_events_map.pop(payload)

        # Record the output if it's in a workflow
        if caller_ctx is not None:
            await self.record_operation_result(
                {
                    "workflow_uuid": caller_ctx["workflow_uuid"],
                    "function_id": caller_ctx["function_id"],
                    "output": utils.serialize(
                        value
                    ),  # None will be serialized to 'null'
                    "error": None,
                }
            )
        return value

    async def _flush_workflow_status_buffer(self) -> None:
        """Export the workflow status buffer to the database, up to the batch size"""
        if len(self._workflow_status_buffer) == 0:
            return

        # Record the exported status so far, and add them back on errors.
        exported_status: Dict[str, WorkflowStatusInternal] = {}
        async with self.async_engine.begin() as c:
            exported = 0
            status_iter = iter(list(self._workflow_status_buffer))
            wf_id: Optional[str] = None
            while (
                exported < buffer_flush_batch_size
                and (wf_id := next(status_iter, None)) is not None
            ):
                # Pop the first key in the buffer (FIFO)
                status = self._workflow_status_buffer.pop(wf_id, None)
                if status is None:
                    continue
                exported_status[wf_id] = status
                try:
                    await self.update_workflow_status(status, conn=c)
                    exported += 1
                except Exception as e:
                    dbos_logger.error(f"Error while flushing status buffer: {e}")
                    await c.rollback()
                    # Add the exported status back to the buffer, so they can be retried next time
                    self._workflow_status_buffer.update(exported_status)
                    break

    async def _flush_workflow_inputs_buffer(self) -> None:
        """Export the workflow inputs buffer to the database, up to the batch size."""
        if len(self._workflow_inputs_buffer) == 0:
            return

        # Record exported inputs so far, and add them back on errors.
        exported_inputs: Dict[str, str] = {}
        async with self.async_engine.begin() as c:
            exported = 0
            input_iter = iter(list(self._workflow_inputs_buffer))
            wf_id: Optional[str] = None
            while (
                exported < buffer_flush_batch_size
                and (wf_id := next(input_iter, None)) is not None
            ):
                if wf_id not in self._exported_temp_txn_wf_status:
                    # Skip exporting inputs if the status has not been exported yet
                    continue
                inputs = self._workflow_inputs_buffer.pop(wf_id, None)
                if inputs is None:
                    continue
                exported_inputs[wf_id] = inputs
                try:
                    await self.update_workflow_inputs(wf_id, inputs, conn=c)
                    exported += 1
                except Exception as e:
                    dbos_logger.error(f"Error while flushing inputs buffer: {e}")
                    await c.rollback()
                    # Add the exported inputs back to the buffer, so they can be retried next time
                    self._workflow_inputs_buffer.update(exported_inputs)
                    break

    def flush_workflow_buffers(self) -> None:
        """Flush the workflow status and inputs buffers periodically, via a background thread."""
        while self._run_background_processes:
            try:
                self._is_flushing_status_buffer = True
                # Must flush the status buffer first, as the inputs table has a foreign key constraint on the status table.
                asyncio.run(self._flush_workflow_status_buffer())
                asyncio.run(self._flush_workflow_inputs_buffer())
                self._is_flushing_status_buffer = False
                if self._is_buffers_empty:
                    # Only sleep if both buffers are empty
                    time.sleep(buffer_flush_interval_secs)
            except Exception as e:
                dbos_logger.error(f"Error while flushing buffers: {e}")
                time.sleep(buffer_flush_interval_secs)
                # Will retry next time

    def buffer_workflow_status(self, status: WorkflowStatusInternal) -> None:
        self._workflow_status_buffer[status["workflow_uuid"]] = status

    def buffer_workflow_inputs(self, workflow_id: str, inputs: str) -> None:
        # inputs is a serialized WorkflowInputs string
        self._workflow_inputs_buffer[workflow_id] = inputs
        self._temp_txn_wf_ids.add(workflow_id)

    @property
    def _is_buffers_empty(self) -> bool:
        return (
            len(self._workflow_status_buffer) == 0
            and len(self._workflow_inputs_buffer) == 0
        )

    async def enqueue(self, workflow_id: str, queue_name: str) -> None:
        async with self.async_engine.begin() as c:
            await c.execute(
                pg.insert(SystemSchema.job_queue)
                .values(
                    workflow_uuid=workflow_id,
                    queue_name=queue_name,
                )
                .on_conflict_do_nothing()
            )

    async def start_queued_workflows(
        self, queue_name: str, concurrency: Optional[int]
    ) -> List[str]:
        async with self.async_engine.begin() as c:
            query = sa.select(SystemSchema.job_queue.c.workflow_uuid).where(
                SystemSchema.job_queue.c.queue_name == queue_name
            )
            if concurrency is not None:
                query = query.order_by(
                    SystemSchema.job_queue.c.created_at_epoch_ms.asc()
                ).limit(concurrency)
            rows = (await c.execute(query)).fetchall()
            dequeued_ids: List[str] = [row[0] for row in rows]
            ret_ids = []
            for id in dequeued_ids:
                result = await c.execute(
                    SystemSchema.workflow_status.update()
                    .where(SystemSchema.workflow_status.c.workflow_uuid == id)
                    .where(
                        SystemSchema.workflow_status.c.status
                        == WorkflowStatusString.ENQUEUED.value
                    )
                    .values(status=WorkflowStatusString.PENDING.value)
                )
                if result.rowcount > 0:
                    ret_ids.append(id)
            return ret_ids

    async def remove_from_queue(self, workflow_id: str) -> None:
        async with self.async_engine.begin() as c:
            await c.execute(
                sa.delete(SystemSchema.job_queue).where(
                    SystemSchema.job_queue.c.workflow_uuid == workflow_id
                )
            )
