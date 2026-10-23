import json
import re
import httpx


def clean_json(text):
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("AI出力はJSONオブジェクトである必要があります。")
    return value


class LLM:
    def __init__(self, profile, secret=""):
        self.profile = profile
        endpoint = profile["endpoint"].rstrip("/")
        if not endpoint.startswith(("http://", "https://")):
            raise ValueError("LLM URLはhttp/httpsで指定してください。")
        self.client = httpx.Client(base_url=endpoint + "/", timeout=profile.get("timeout", 180),
                                   headers={"Authorization": "Bearer " + secret} if secret else {}, trust_env=False)

    def close(self):
        self.client.close()

    def test(self):
        if self.profile.get("provider") == "anthropic":
            return {"text": self.complete("接続試験です。OKと答えてください。", "短く回答してください。", 32)}
        r = self.client.get("models")
        r.raise_for_status()
        ids = [m["id"] for m in r.json().get("data", [])]
        if self.profile["model_id"] not in ids:
            raise ValueError("設定モデルIDが接続先の一覧にありません。")
        return {"models": ids}

    def complete(self, prompt, system, max_tokens=None):
        limit = max_tokens or self.profile.get("max_tokens", 4096)
        if self.profile.get("provider") == "anthropic":
            headers = {"anthropic-version": "2023-06-01"}
            auth = self.client.headers.get("Authorization", "")
            if auth:
                headers["x-api-key"] = auth.removeprefix("Bearer ")
            response = self.client.post("messages", headers=headers, json={
                "model": self.profile["model_id"], "system": system, "max_tokens": limit,
                "messages": [{"role": "user", "content": prompt}]})
            response.raise_for_status()
            return "\n".join(x.get("text", "") for x in response.json()["content"] if x["type"] == "text")
        response = self.client.post("chat/completions", json={
            "model": self.profile["model_id"], "temperature": 0.2, "max_tokens": limit,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "chat_template_kwargs": {"enable_thinking": False}})
        response.raise_for_status()
        content = response.json()["choices"][0]["message"].get("content")
        if not content:
            raise ValueError("AIから本文が返りませんでした。出力上限を確認してください。")
        return content
