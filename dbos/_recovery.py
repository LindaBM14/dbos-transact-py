import os
import threading
import time
import traceback
from typing import TYPE_CHECKING, Any, List

from ._context import SetWorkflowRecovery
from ._core import execute_workflow_by_id
from ._error import DBOSWorkflowFunctionNotFoundError
from ._sys_db import GetPendingWorkflowsOutput

if TYPE_CHECKING:
    from ._dbos import DBOS, WorkflowHandle


def startup_recovery_thread(dbos: "DBOS", pending_workflows: List[GetPendingWorkflowsOutput]) -> None:
    """Attempt to recover local pending workflows on startup using a background thread."""
    stop_event = threading.Event()
    dbos.stop_events.append(stop_event)
    while not stop_event.is_set() and len(pending_workflows) > 0:
        try:
            for pending_workflow in list(pending_workflows):
                with SetWorkflowRecovery():
                    execute_workflow_by_id(dbos, pending_workflow.workflow_uuid)
                pending_workflows.remove(pending_workflow)
        except DBOSWorkflowFunctionNotFoundError:
            time.sleep(1)
        except Exception as e:
            dbos.logger.error(
                f"Exception encountered when recovering workflows: {traceback.format_exc()}"
            )
            raise e


def recover_pending_workflows(
    dbos: "DBOS", executor_ids: List[str] = ["local"]
) -> List["WorkflowHandle[Any]"]:
    workflow_handles: List["WorkflowHandle[Any]"] = []
    for executor_id in executor_ids:
        if executor_id == "local" and os.environ.get("DBOS__VMID"):
            dbos.logger.debug(
                f"Skip local recovery because it's running in a VM: {os.environ.get('DBOS__VMID')}"
            )
        dbos.logger.debug(f"Recovering pending workflows for executor: {executor_id}")
        pending_workflows = dbos._sys_db.get_pending_workflows(executor_id)
        dbos.logger.debug(f"Pending workflows: {pending_workflows}")

        for pending_workflow in pending_workflows:
            with SetWorkflowRecovery():
                handle = execute_workflow_by_id(dbos, pending_workflow.workflow_uuid)
            workflow_handles.append(handle)

    dbos.logger.info("Recovered pending workflows")
    return workflow_handles
