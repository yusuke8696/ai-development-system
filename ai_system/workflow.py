import uuid

WORKFLOWS = {
    "waterfall": {"name": "ウォーターフォール開発", "steps": [
        ("requirements", "要件定義", "general"), ("architecture", "構成設計", "design"),
        ("design", "詳細設計", "design"), ("implementation", "実装", "coding"),
        ("verification", "テスト・検証", "review"), ("delivery", "完了報告", "general")]},
    "agile": {"name": "アジャイル開発", "steps": [
        ("vision", "プロダクト目標", "general"), ("backlog", "バックログ整理", "general"),
        ("iteration_plan", "反復計画", "design"), ("implementation", "実装", "coding"),
        ("verification", "受入テスト", "review"), ("retrospective", "振り返り", "general")]},
    "aidlc": {"name": "AI-DLC開発", "steps": [
        ("intent", "初期化・意図整理", "general"), ("adaptive_plan", "構想・適応計画", "design"),
        ("requirements", "Inception / 要件整理", "general"), ("design", "作業単位設計", "design"),
        ("implementation", "Construction / 実装", "coding"),
        ("verification", "検証", "review"), ("delivery", "Operation / 運用・引継ぎ", "general")]},
}


def plan(project_id, workflow, scope=1, prior=None):
    result = []
    dep = prior
    for kind, title, role in WORKFLOWS[workflow]["steps"]:
        task = {"id": str(uuid.uuid4()), "project_id": project_id, "scope": scope,
                "kind": kind, "title": title, "role": role, "state": "ready" if not dep else "planned",
                "depends_on": [dep] if dep else [], "artifacts": [], "pr": None, "issue": None,
                "template_id": kind.replace("_", "-"), "owner": None, "run_id": None}
        result.append(task)
        dep = task["id"]
    result.append({"id": str(uuid.uuid4()), "project_id": project_id, "scope": scope,
                   "kind": "leader_review", "title": "リーダーによるGitHubレビュー・承認",
                   "role": "leader", "state": "planned", "depends_on": [dep],
                   "artifacts": [], "pr": None, "issue": None, "owner": None, "run_id": None})
    return result


def evaluate_reviews(pr, reviews, leader_id):
    """No UI operation can manufacture this result; caller supplies fresh GitHub data."""
    if not leader_id:
        return False, "リーダー未指定"
    if pr.get("user", {}).get("id") == leader_id:
        return False, "PR作者と別のリーダーが必要です。"
    latest = {}
    for review in sorted(reviews, key=lambda r: (r.get("submitted_at") or "", r["id"])):
        state = review.get("state")
        if state in ("COMMENTED", "PENDING"):
            continue
        latest[review["user"]["id"]] = review
    if any(r.get("state") == "CHANGES_REQUESTED" for r in latest.values()):
        return False, "未解消の変更要求があります。"
    leader = latest.get(leader_id, {})
    if leader.get("user", {}).get("type") != "User" or leader.get("state") != "APPROVED":
        return False, "指定リーダーの有効な承認待ち"
    if leader.get("commit_id") != pr["head"]["sha"]:
        return False, "対象版が変わりました。再承認が必要です。"
    if pr.get("state") == "closed" and not pr.get("merged"):
        return False, "PRは未マージのまま閉じられています。"
    return True, leader
