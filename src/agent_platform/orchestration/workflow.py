from __future__ import annotations
import uuid
import re
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from .planner import build_plan
from ..memory.working_state import WorkingState
from ..memory.engram import EngramAdapter, may_contain_secret, memory_safe_text
from ..tracing.tracer import Tracer

class WorkflowControl:
    """Thread-safe steering applied at stage boundaries."""
    def __init__(self):
        self._lock = threading.Lock()
        self._guidance: list[str] = []
        self._cancelled = threading.Event()

    def add_guidance(self, message: str) -> None:
        with self._lock:
            self._guidance.append(message.strip()[:4000])

    def consume_guidance(self) -> list[str]:
        with self._lock:
            messages, self._guidance = self._guidance, []
        return messages

    def cancel(self) -> None:
        self._cancelled.set()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

def emit(callback: Callable[[dict], None] | None, event: dict) -> None:
    if callback:
        callback(event)

def summarize_stage(role: str, result) -> str:
    parts = [f"{role}: {result.status}; {result.summary[:700]}"]
    if result.changed_files:
        parts.append("files=" + ", ".join(result.changed_files[:8]))
    if result.test_results:
        parts.append("tests=" + "; ".join(result.test_results[:3]))
    if result.issues:
        parts.append("issues=" + "; ".join(result.issues[:5]))
    return " | ".join(parts)[:1200]

def task_for_stage(role: str, task: str, plan: list[dict], handoff: list[str]) -> str:
    planned = ", ".join(item["agent"] for item in plan)
    prior = "\n".join(handoff[-4:])
    if role == "orchestrator":
        return (f"Evaluate this task and proposed plan: {task}. Planned stages: {planned}. "
                "Identify assumptions, missing stages, and whether this plan preserves required ordering.")
    instructions = {
        "analyst": "Produce a concise diagnosis with prioritized evidence. Do not implement.",
        "architect": "Use the analyst handoff to propose Quick Wins, Medium, and Long-term work. Do not implement.",
        "reviewer": "Review the prior plan or implementation for feasibility, regressions, token savings, and compatibility. Do not implement.",
        "qa": "Verify the implemented changes and record observed tests or clear blockers.",
    }
    lead = instructions.get(role, "Implement only reviewer-approved Quick Wins relevant to your role.")
    return f"{lead}\n\nORIGINAL TASK\n{task}\n\nPLANNED STAGES\n{planned}\n\nPRIOR STAGE HANDOFF\n{prior or 'None yet.'}"

def execute_workflow(task: str, workspace: Path, runner, memory: EngramAdapter | None = None,
                     on_event: Callable[[dict], None] | None = None,
                     control: WorkflowControl | None = None) -> dict:
    task_id = uuid.uuid4().hex
    plan = build_plan(task)
    state = {"task_id": task_id, "objective": task, "status": "running", "execution_plan": plan,
             "agent_status": {}, "changed_files": [], "test_results": [], "unresolved_issues": [],
             "issues_by_agent": {}, "rework": [], "selected_skills": [], "guidance": [],
             "memory": {"saved": False, "error": None, "skipped_reason": None},
             "started_at": datetime.now(timezone.utc).isoformat()}
    storage = WorkingState(workspace / ".state" / "current-task.json")
    tracer = Tracer(workspace)
    tracer.write({"event": "workflow_started", "task_id": task_id, "objective": task,
                  "planned_agents": [stage["agent"] for stage in plan]})
    storage.save(state)
    emit(on_event, {"type": "workflow_started", "task_id": task_id,
                    "plan": [item["agent"] for item in plan]})
    review_summary = ""
    handoff = []
    for stage in plan:
        if control and control.cancelled:
            state["status"] = "cancelled"
            storage.save(state)
            emit(on_event, {"type": "workflow_cancelled", "task_id": task_id})
            return state
        role = stage["agent"]
        guidance = control.consume_guidance() if control else []
        if guidance:
            state["guidance"].extend(guidance)
        stage["status"] = "running"
        tracer.write({"event": "workflow_stage", "task_id": task_id, "agent": role, "status": "running"})
        storage.save(state)
        emit(on_event, {"type": "stage_started", "task_id": task_id, "agent": role})
        try:
            stage_task = task_for_stage(role, task, plan, handoff)
            if guidance:
                stage_task += "\n\nNEW USER GUIDANCE\n" + "\n".join(guidance)
            result, selected = runner(role, stage_task, workspace, task_id)
        except Exception as exc:
            stage["status"] = "failed"
            state["agent_status"][role] = "failed"
            state["unresolved_issues"].append(f"{role}: {exc}")
            tracer.write({"event": "workflow_stage", "task_id": task_id, "agent": role,
                          "status": "failed", "error": str(exc)[:300]})
            storage.save(state)
            emit(on_event, {"type": "stage_failed", "task_id": task_id,
                            "agent": role, "error": str(exc)[:300]})
            break
        stage["status"] = result.status
        state["agent_status"][role] = result.status
        if role == "reviewer":
            review_summary = result.summary
        handoff.append(summarize_stage(role, result))
        state["changed_files"] = sorted(set(state["changed_files"] + result.changed_files))
        state["test_results"].extend(result.test_results)
        state["issues_by_agent"][role] = result.issues
        state["unresolved_issues"] = [issue for issues in state["issues_by_agent"].values() for issue in issues]
        state["selected_skills"] = sorted(set(state["selected_skills"] + selected))
        tracer.write({"event": "workflow_stage", "task_id": task_id, "agent": role,
                      "status": result.status, "changed_files": result.changed_files,
                      "test_results": result.test_results, "issues": result.issues})
        storage.save(state)
        emit(on_event, {"type": "stage_finished", "task_id": task_id, "agent": role,
                        "status": result.status, "summary": result.summary[:700],
                        "changed_files": result.changed_files, "test_results": result.test_results,
                        "issues": result.issues})
        if result.status not in ("completed", "success"):
            break
    if plan[-1]["status"] in ("completed", "success") and state["issues_by_agent"].get("reviewer"):
        implementation = next((item["agent"] for item in reversed(plan)
            if item["agent"] in {"backend", "frontend", "database", "devops"}), None)
        if implementation:
            for role in (implementation, "qa", "reviewer"):
                try:
                    result, selected = runner(role, task + "\nFix reviewer findings: " +
                        "; ".join(state["issues_by_agent"].get("reviewer", [])), workspace, task_id)
                except Exception as exc:
                    state["rework"].append({"agent": role, "status": "failed", "error": str(exc)[:300]})
                    break
                state["rework"].append({"agent": role, "status": result.status})
                state["issues_by_agent"][role] = result.issues
                if role == "reviewer":
                    review_summary = result.summary
                state["changed_files"] = sorted(set(state["changed_files"] + result.changed_files))
                state["test_results"].extend(result.test_results)
                state["selected_skills"] = sorted(set(state["selected_skills"] + selected))
                state["unresolved_issues"] = [issue for issues in state["issues_by_agent"].values() for issue in issues]
                storage.save(state)
                if result.status not in ("completed", "success"):
                    break
    state["status"] = ("completed" if all(stage["status"] in ("completed", "success") for stage in plan)
                       and not state["unresolved_issues"]
                       and all(item["status"] in ("completed", "success") for item in state["rework"])
                       else "incomplete")
    if state["status"] == "completed" and memory is not None and memory.available:
        if may_contain_secret(task, strict=True) or may_contain_secret(review_summary, strict=True):
            state["memory"]["skipped_reason"] = "sensitive_content"
        else:
            words = re.findall(r"[A-Za-zА-Яа-я][\w-]{1,24}", task)[:10]
            title = memory_safe_text("Completed: " + " ".join(words), 120)
            note = (f"Reviewer completed successfully. Files changed: {len(state['changed_files'])}; "
                    f"test results: {len(state['test_results'])}. "
                    "Verify details against .ai/ and Git before reuse.")
            try:
                state["memory"]["saved"] = memory.remember(title, note)
            except Exception as exc:
                state["memory"]["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    storage.save(state)
    tracer.write({"event": "workflow_finished", "task_id": task_id, "status": state["status"],
                  "changed_files": state["changed_files"], "test_results": state["test_results"],
                  "unresolved_issues": state["unresolved_issues"]})
    emit(on_event, {"type": "workflow_finished", "task_id": task_id, "status": state["status"],
                    "changed_files": state["changed_files"], "test_results": state["test_results"],
                    "issues": state["unresolved_issues"]})
    return state
