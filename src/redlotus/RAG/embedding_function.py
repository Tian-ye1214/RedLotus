from __future__ import annotations

from typing import Any

import httpx
from redlotus.infra import logger
from redlotus.config import app_config
from redlotus.config.app_config import get_env, settings
from redlotus.infra.shared_http import get_client, openai_base_url

_HTTP_KEY = "rag"


def _require_rag_model(role: str) -> str:
    name = settings()["RAG_models"][role].strip()
    if not name:
        raise RuntimeError(f"config.json 的 RAG_models 中缺少 {role!r}。")
    return name


def _get_shared_client() -> httpx.AsyncClient:
    """当前事件循环的 embedding/rerank 连接池；配置地址变化时使用新池。"""
    config = settings()["rag_service"]
    kwargs = dict(
        base_url=openai_base_url(get_env("SILICONFLOW_BASE", warn=False)),
        http2=config["http2"],
        timeout=config["timeout"],
    )
    return get_client(f"{_HTTP_KEY}:{kwargs}", lambda: httpx.AsyncClient(**kwargs))


def _require_rag_api() -> None:
    missing = app_config.missing_rag_api_keys()
    if missing:
        raise RuntimeError("缺少 RAG API 配置: " + ", ".join(missing))


async def _rag_api_post(endpoint: str, body: dict[str, Any]) -> dict[str, Any]:
    """RAG 接口统一 POST：构造鉴权头、校验状态、解析 JSON。"""
    response = await _get_shared_client().post(
        endpoint,
        headers={
            "Authorization": f"Bearer {get_env('SILICONFLOW_KEY', warn=False).strip()}",
            "Content-Type": "application/json",
        },
        json=body,
    )
    response.raise_for_status()
    return response.json()


async def embed_texts(
    texts: str | list[str],
    *,
    model: str | None = None,
) -> list[list[float]]:
    """异步获取文本向量；支持单条字符串或多条批量，超过上限自动分批请求。"""
    _require_rag_api()
    if isinstance(texts, str):
        texts = [texts]
    logger.debug("RAG embed: batch_size=%d", len(texts))
    model = model or _require_rag_model("embedding")
    batch_size = int(settings()["rag_service"]["embedding_batch_size"])
    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        body = {"model": model, "input": texts[start : start + batch_size]}
        data = await _rag_api_post("/embeddings", body)
        items = data.get("data") or []
        n = len(items)
        if n > 1 and [x.get("index", 0) for x in items] != list(range(n)):
            items = sorted(items, key=lambda x: x.get("index", 0))
        vectors.extend(row["embedding"] for row in items)
    return vectors


async def rerank_documents(
    query: str,
    documents: list[str],
    *,
    top_n: int | None = None,
) -> list[dict[str, Any]]:
    """调用与 OpenAI 兼容的 /v1/rerank，返回按相关度排序的结果（含原始下标与分数）。"""
    _require_rag_api()
    if not documents:
        return []
    logger.debug("RAG rerank: n_docs=%d, top_n=%s", len(documents), top_n)
    model = _require_rag_model("reranker")
    body: dict[str, Any] = {
        "model": model,
        "query": query,
        "documents": documents,
        "return_documents": False,
    }
    if top_n is not None:
        body["top_n"] = top_n

    data = await _rag_api_post("/rerank", body)

    return [
        dict(
            index=int(row["index"]),
            text=documents[int(row["index"])],
            relevance_score=float(row.get("relevance_score", 0)),
        )
        for row in data.get("results", [])
    ]
