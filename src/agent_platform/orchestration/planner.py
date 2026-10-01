from .router import select_agents

def build_plan(task: str) -> list[dict]:
    roles = select_agents(task)
    return [{"agent": role, "depends_on": [] if index == 0 else [roles[index - 1]], "status": "pending"}
            for index, role in enumerate(roles)]
