from __future__ import annotations
import asyncio
import json
import os
from pathlib import Path

import chainlit as cl

from agent_platform.cli import run_agent
from agent_platform.config import load_config
from agent_platform.memory.engram import EngramAdapter
from agent_platform.orchestration.chat_orchestrator import decide_message
from agent_platform.orchestration.workflow import WorkflowControl, execute_workflow

WORKSPACE = Path(os.environ.get("AGENT_PLATFORM_WORKSPACE", ".")).resolve()

def workflow_memory():
    config = load_config()
    settings = config["memory"]["engram"]
    if not settings["enabled"] or not settings["auto_save_completed"]:
        return None
    return EngramAdapter(WORKSPACE, WORKSPACE / settings["data_dir"])

async def show_status() -> None:
    path = WORKSPACE / ".state" / "current-task.json"
    if not path.exists():
        await cl.Message(content="Активных или завершённых workflow пока нет.").send()
        return
    state = json.loads(path.read_text(encoding="utf-8"))
    stages = "\n".join(f"- {item['agent']}: {item['status']}" for item in state.get("execution_plan", []))
    await cl.Message(content=f"Статус: **{state.get('status', 'unknown')}**\n\n{stages}").send()

async def run_background(task: str, control: WorkflowControl) -> None:
    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()
    steps: dict[str, cl.Step] = {}

    def on_event(event: dict) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, event)

    worker = asyncio.create_task(asyncio.to_thread(
        execute_workflow, task, WORKSPACE, run_agent, workflow_memory(), on_event, control))
    try:
        while not worker.done() or not queue.empty():
            try:
                event = await asyncio.wait_for(queue.get(), timeout=0.25)
            except asyncio.TimeoutError:
                continue
            kind = event["type"]
            if kind == "workflow_started":
                await cl.Message(content="План: " + " → ".join(event["plan"])).send()
            elif kind == "stage_started":
                step = cl.Step(name=event["agent"], type="tool")
                step.input = "Этап запущен"
                await step.send()
                steps[event["agent"]] = step
            elif kind in {"stage_finished", "stage_failed"}:
                step = steps.get(event["agent"])
                text = event.get("summary") or event.get("error") or event.get("status", "")
                if step:
                    step.output = text
                    await step.update()
                else:
                    await cl.Message(content=f"{event['agent']}: {text}").send()
            elif kind == "workflow_cancelled":
                await cl.Message(content="Задача остановлена пользователем.").send()
            elif kind == "workflow_finished":
                files = "\n".join(f"- `{item}`" for item in event["changed_files"])
                issues = "\n".join(f"- {item}" for item in event["issues"])
                body = f"Workflow завершён со статусом **{event['status']}**."
                if files:
                    body += "\n\nИзменённые файлы:\n" + files
                if issues:
                    body += "\n\nНерешённые вопросы:\n" + issues
                await cl.Message(content=body).send()
        await worker
    except Exception as exc:
        await cl.Message(content=f"Ошибка workflow: `{type(exc).__name__}: {str(exc)[:500]}`").send()
    finally:
        cl.user_session.set("active_control", None)
        cl.user_session.set("active_task", None)

@cl.on_chat_start
async def start():
    cl.user_session.set("history", [])
    cl.user_session.set("active_control", None)
    await cl.Message(content=(
        f"Локальный оркестратор готов. Рабочая папка: `{WORKSPACE}`\n\n"
        "Опишите задачу или задайте вопрос. Во время работы новые сообщения будут добавлены "
        "как уточнения к следующему этапу. Команды: `/status`, `/stop`, `/help`."
    )).send()

@cl.on_message
async def message(message: cl.Message):
    text = message.content.strip()
    control: WorkflowControl | None = cl.user_session.get("active_control")
    if text == "/status":
        await show_status()
        return
    if text == "/stop":
        if control:
            control.cancel()
            await cl.Message(content="Остановка запрошена. Текущий агент завершит вызов, затем workflow остановится.").send()
        else:
            await cl.Message(content="Активной задачи нет.").send()
        return
    if text == "/help":
        await cl.Message(content=("Напишите задачу обычным текстом. `/status` показывает этапы, `/stop` "
                                  "останавливает workflow. Вопрос во время работы завершайте знаком `?`.")).send()
        return
    if control:
        if text.endswith("?"):
            try:
                decision = await asyncio.to_thread(decide_message, text, WORKSPACE,
                                                   cl.user_session.get("history") or [])
                await cl.Message(content=decision.response or
                                 "Этот вопрос связан с выполняемой задачей; добавьте уточнение без знака вопроса.").send()
            except Exception as exc:
                await cl.Message(content=f"Не удалось ответить: `{type(exc).__name__}: {str(exc)[:300]}`").send()
            return
        control.add_guidance(text)
        await cl.Message(content="Уточнение принято и будет передано следующему этапу workflow.").send()
        return

    history = cl.user_session.get("history") or []
    try:
        decision = await asyncio.to_thread(decide_message, text, WORKSPACE, history)
    except Exception as exc:
        await cl.Message(content=f"Оркестратор LM Studio недоступен: `{type(exc).__name__}: {str(exc)[:400]}`").send()
        return
    history.append({"role": "user", "content": text})
    if decision.kind == "chat":
        answer = decision.response or "Уточните вопрос, пожалуйста."
        history.append({"role": "assistant", "content": answer})
        cl.user_session.set("history", history[-12:])
        await cl.Message(content=answer).send()
        return

    task = decision.task or text
    answers = []
    for question in decision.questions:
        response = await cl.AskUserMessage(content=question, timeout=86400).send()
        if response and response.get("output"):
            answers.append(f"{question}\nОтвет: {response['output']}")
    if answers:
        task += "\n\nUSER CLARIFICATIONS\n" + "\n\n".join(answers)
    control = WorkflowControl()
    cl.user_session.set("active_control", control)
    background = asyncio.create_task(run_background(task, control))
    cl.user_session.set("active_task", background)
    await cl.Message(content="Задача принята. Workflow запущен в фоне; чат остаётся доступен.").send()
