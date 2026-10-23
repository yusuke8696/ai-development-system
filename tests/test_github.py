import json
import httpx
import pytest
from ai_system.github import GitHub, repository
from ai_system.store import Conflict


@pytest.mark.parametrize("url", ["yusuke8696/ai-development-system", "https://github.com/yusuke8696/ai-development-system.git"])
def test_repository_url(url):
    assert repository(url) == "yusuke8696/ai-development-system"


def test_repository_rejects_credentials():
    with pytest.raises(ValueError):
        repository("https://token@github.com/owner/repo")


def test_claim_create_only_and_error():
    refs = {}
    def server(req):
        path = req.url.path
        if path.endswith("/git/ref/heads/main"):
            return httpx.Response(200, json={"object": {"sha": "base"}})
        if path.endswith("/git/commits/base"):
            return httpx.Response(200, json={"tree": {"sha": "tree"}})
        if path.endswith("/git/trees"):
            return httpx.Response(201, json={"sha": "new-tree"})
        if path.endswith("/git/commits"):
            return httpx.Response(201, json={"sha": "new-commit"})
        if path.endswith("/git/refs"):
            body = json.loads(req.content)
            if body["ref"] in refs:
                return httpx.Response(422, json={})
            refs[body["ref"]] = body["sha"]
            return httpx.Response(201, json={})
        raise AssertionError(path)
    gh = GitHub(transport=httpx.MockTransport(server))
    project = {"repo": "owner/repo", "base_branch": "main"}
    task = {"id": "one", "title": "task"}
    assert gh.claim(project, task, {"user_id": 1})[0] == "ai-work/one"
    with pytest.raises(Conflict):
        gh.claim(project, task, {"user_id": 2})


def test_paginated_reviews():
    def server(req):
        page = req.url.params["page"]
        return httpx.Response(200, json=[{"id": i} for i in range(100)] if page == "1" else [{"id": 101}])
    gh = GitHub(transport=httpx.MockTransport(server))
    assert len(gh.pages("/repos/o/r/pulls/1/reviews")) == 101
