def select_agents(task: str) -> list[str]:
    text = task.lower()
    stages = []
    discovery_first = any(word in text for word in (
        "self-improvement", "architecture", "architect", "analyze", "diagnose",
        "plan", "reviewer", "quick wins", "самоулучш", "архитект", "проанализ",
        "диагноз", "план", "строго по порядку",
    ))
    complex_task = discovery_first or any(word in text for word in (
        "feature", "integration", "integrat", "full-stack", "contract", "kafka",
        "функц", "интеграц",
    ))
    if complex_task:
        stages.extend(["analyst", "architect"])
    if not discovery_first and any(word in text for word in ("schema", "migration", "database", "sql", "таблиц", "миграц")):
        stages.append("database")
    if not discovery_first and any(word in text for word in ("front", "ui", "form", "twig", "react", "vue", "интерфейс", "форм")):
        stages.append("frontend")
    if not discovery_first and any(word in text for word in ("docker", "ci", "build", "pipeline", "devops")):
        stages.append("devops")
    if discovery_first or not any(role in stages for role in ("database", "frontend", "devops")) or any(word in text for word in ("backend", "api", "rest", "kafka", "service", "endpoint")):
        stages.append("backend")
    stages.extend(["qa", "reviewer"])
    return ["orchestrator", *dict.fromkeys(stages)]
