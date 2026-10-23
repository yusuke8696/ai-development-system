import hashlib
import json
import re
from markdown_it import MarkdownIt

MD = MarkdownIt("commonmark", {"html": False}).enable("table")


def normalized(text):
    return re.sub(r"^\d+(?:\.\d+)*[.、)\s]*", "", text.strip()).strip()


def template_id(text):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", text):
        raise ValueError("IDは英小文字・数字・ハイフンで64文字以内にしてください。")
    return text


def validate_template(data):
    template_id(data["id"])
    if len(data.get("body", "").encode()) > 1024 * 1024:
        raise ValueError("テンプレートは1MiB以内にしてください。")
    if not data.get("body", "").strip():
        raise ValueError("Markdown本文が必要です。")
    variables = set(re.findall(r"{{\s*([^{}]+?)\s*}}", data["body"]))
    if variables - {"project.name", "document.title", "document.version"}:
        raise ValueError("未定義の変数があります。")
    for name in ("required_headings", "required_tables"):
        if not isinstance(data.get(name, []), list):
            raise ValueError(name + " は配列です。")
    for table in data.get("required_tables", []):
        if not isinstance(table, dict) or not isinstance(table.get("columns"), list) or not table.get("under_heading"):
            raise ValueError("表の規則にはunder_headingとcolumnsが必要です。")
    data["hash"] = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return data


def render_template(template, project, title):
    text = template["body"]
    values = {"project.name": project["name"], "document.title": title, "document.version": "1.0"}
    return re.sub(r"{{\s*([^{}]+?)\s*}}", lambda m: values[m[1]], text)


def validate_document(text, template):
    tokens = MD.parse(text)
    headings, tables, current = [], [], ""
    for i, token in enumerate(tokens):
        if token.type == "heading_open":
            current = normalized(tokens[i + 1].content)
            headings.append(current)
        if token.type == "thead_open":
            columns = []
            j = i + 1
            while tokens[j].type != "thead_close":
                if tokens[j].type == "th_open":
                    columns.append(tokens[j + 1].content.strip())
                j += 1
            tables.append((current, columns))
    errors = []
    wanted = template.get("required_headings", [])
    positions = []
    for title in wanted:
        title = normalized(title)
        if title not in headings:
            errors.append("必須見出しがありません: " + title)
        elif headings.count(title) > 1:
            errors.append("必須見出しが重複しています: " + title)
        else:
            positions.append(headings.index(title))
    if template.get("heading_order_required", True) and positions != sorted(positions):
        errors.append("必須見出しの順序が異なります。")
    for rule in template.get("required_tables", []):
        if not any(h == normalized(rule["under_heading"]) and cols == rule["columns"] for h, cols in tables):
            errors.append("必須表の列が異なります: " + rule["under_heading"])
    if re.search(r"{{[^{}]+}}", text):
        errors.append("未置換の変数があります。")
    return errors


def defaults():
    specs = {
        "requirements": ("要件定義書", ["目的", "対象範囲", "要件一覧", "未決事項"]),
        "architecture": ("構成設計書", ["構成概要", "構成要素", "データと連携", "制約と未決事項"]),
        "design": ("詳細設計書", ["処理設計", "データ設計", "インターフェース", "エラー処理"]),
        "vision": ("プロダクト目標", ["目的", "利用者", "成功条件", "未決事項"]),
        "backlog": ("プロダクトバックログ", ["目標", "バックログ", "受入条件", "未決事項"]),
        "iteration_plan": ("反復計画", ["反復目標", "対象タスク", "検証計画", "未決事項"]),
        "intent": ("開発意図", ["意図", "質問と回答", "制約", "未決事項"]),
        "adaptive_plan": ("適応的実行計画", ["目的", "採用工程と理由", "作業単位", "確認点"]),
        "verification": ("検証計画・結果", ["検証対象", "試験項目", "結果", "未解決事項"]),
        "retrospective": ("振り返り", ["成果", "課題", "改善策", "次の反復"]),
        "delivery": ("完了報告書", ["成果物", "検証結果", "運用と引継ぎ", "残課題"]),
    }
    for key, (name, headings) in specs.items():
        body = "# {{document.title}}\n\n" + "\n\n".join("## " + h + "\n<!-- 記載ガイド: 確定情報を記載。不足情報は質問する -->" for h in headings)
        yield validate_template({"id": key.replace("_", "-"), "name": name, "document_type": key,
                                 "body": body, "required_headings": headings, "required_tables": [],
                                 "heading_order_required": True, "builtin": True})
