"""IndexDocument Lambda — ページごとの埋め込み JSON を OpenSearch に bulk index する。

embeddings/{domain}/{連番}_{title}/p{n}.json（models.Chunk 配列）を読み、chunk_id を
_id にして投入する（再実行は upsert＝重複しない）。ベクトルは chunk_embed で算出済み
（既定は Titan Text Embeddings V2）なので OpenSearch 側の neural/ML connector は使わず、
素の k-NN index に入れる。index が無ければハイブリッド検索用（knn_vector + kuromoji の BM25）
のマッピングで作成する。faiss + innerproduct（正規化済みベクトルでコサイン相当）。

入力 : models.IndexInput（bucket, doc_id, embeddings_prefix, page_count）
出力 : models.IndexOutput（doc_id, index, indexed）
認証 : Lambda 実行ロールの SigV4（urllib3 署名）。エンドポイントは OPENSEARCH_ENDPOINT。
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any, Iterator

from pydantic import ValidationError

import common
import config
import models

logger = common.get_logger(__name__)
cfg = config.index


@lru_cache(maxsize=1)
def _client() -> Any:
    """SigV4（urllib3 署名）付き OpenSearch クライアントを遅延生成・再利用。"""
    import boto3
    from opensearchpy import OpenSearch, Urllib3AWSV4SignerAuth, Urllib3HttpConnection

    if not cfg.opensearch_endpoint:
        raise common.PermanentError("OPENSEARCH_ENDPOINT が未設定です")
    host = cfg.opensearch_endpoint.replace("https://", "").replace("http://", "").strip("/")
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION", "ap-northeast-1")
    auth = Urllib3AWSV4SignerAuth(boto3.Session().get_credentials(), region, "es")
    return OpenSearch(
        hosts=[{"host": host, "port": 443}],
        http_auth=auth,
        use_ssl=True,
        verify_certs=True,
        connection_class=Urllib3HttpConnection,
        pool_maxsize=20,
        timeout=30,
    )


def _index_body() -> dict:
    """ハイブリッド検索用マッピング: knn_vector（意味）+ text(kuromoji の BM25, 字句）。"""
    return {
        "settings": {"index.knn": True},
        "mappings": {"properties": {
            "embedding": {
                "type": "knn_vector",
                "dimension": config.embed.embed_dimension,
                "method": {"name": "hnsw", "engine": "faiss", "space_type": "innerproduct"},
            },
            "text": {"type": "text", "analyzer": "kuromoji"},  # 日本語 BM25
            "doc_id": {"type": "keyword"},
            "source_file": {"type": "keyword"},
            "domain": {"type": "keyword"},
            "page_no": {"type": "integer"},
            "page_title": {"type": "text", "analyzer": "kuromoji"},
            "section": {"type": "keyword"},
            "parent_id": {"type": "keyword"},
            "chunk_index": {"type": "integer"},
            "token_count": {"type": "integer"},
        }},
    }


def _ensure_index(client: Any) -> None:
    """index が無ければ作成（Map 並列実行の競合は already_exists を無視）。"""
    from opensearchpy.exceptions import RequestError

    if client.indices.exists(index=cfg.opensearch_index):
        return
    try:
        client.indices.create(index=cfg.opensearch_index, body=_index_body())
        logger.info("created index %s", cfg.opensearch_index)
    except RequestError as exc:
        if getattr(exc, "error", "") == "resource_already_exists_exception":
            return  # 別の並列実行が先に作成済み
        raise


def _actions(inp: models.IndexInput) -> Iterator[dict]:
    """embeddings JSON を読み、bulk アクション（chunk_id を _id に upsert）を生成。"""
    for page_no in range(1, inp.page_count + 1):
        chunks = common.get_json(inp.bucket, f"{inp.embeddings_prefix}/p{page_no}.json") or []
        for c in chunks:
            m = c["metadata"]
            yield {
                "_op_type": "index",
                "_index": cfg.opensearch_index,
                "_id": c["chunk_id"],
                "_source": {
                    "text": c["text"],
                    "embedding": c["embedding"],
                    "doc_id": m["doc_id"],
                    "source_file": m["source_file"],
                    "domain": m["domain"],
                    "page_no": m["page_no"],
                    "page_title": m.get("page_title"),
                    "section": m.get("section"),
                    "parent_id": m["parent_id"],
                    "chunk_index": m["chunk_index"],
                    "token_count": m["token_count"],
                },
            }


def _prune_orphans(client: Any, doc_id: str, keep_ids: list[str]) -> int:
    """同じ doc_id で今回投入しなかった残存 chunk（orphan）を削除し件数を返す。

    再 ingest で chunk 数が減ったとき、upsert だけでは古い _id が残るため。bulk の
    後に呼ぶことで「投入済みより少ない瞬間」を作らず、本物の orphan だけ消す。
    """
    resp = client.delete_by_query(
        index=cfg.opensearch_index,
        body={"query": {"bool": {
            "filter": [{"term": {"doc_id": doc_id}}],
            "must_not": [{"ids": {"values": keep_ids}}],
        }}},
        conflicts="proceed",  # 並行更新による version 競合は無視
        refresh=True,
    )
    return int(resp.get("deleted", 0))


def handler(event: dict, context: object) -> dict:
    from opensearchpy import helpers
    from opensearchpy.exceptions import OpenSearchException, TransportError
    from opensearchpy.helpers import BulkIndexError

    try:
        inp = models.IndexInput.model_validate(event)
    except ValidationError as exc:
        raise common.PermanentError(f"invalid IndexDocument input: {exc}") from exc

    actions = list(_actions(inp))
    if not actions:
        logger.warning("doc_id=%s: index skip（chunk なし）", inp.doc_id)
        return models.IndexOutput(
            doc_id=inp.doc_id, index=cfg.opensearch_index, indexed=0
        ).model_dump()

    logger.info("IndexDocument start: doc_id=%s index=%s docs=%d",
                inp.doc_id, cfg.opensearch_index, len(actions))
    client = _client()
    _ensure_index(client)
    try:
        indexed, _ = helpers.bulk(
            client, actions, chunk_size=cfg.bulk_batch_size,
            max_retries=3, initial_backoff=2, request_timeout=60,
        )
    except BulkIndexError as exc:  # ドキュメント単位の失敗（マッピング不整合等）→ 恒久
        raise common.PermanentError(f"opensearch bulk doc errors: {exc.errors[:3]}") from exc
    except TransportError as exc:  # 429/5xx は一時障害として Retry
        status = getattr(exc, "status_code", 0)
        if status == 429 or (isinstance(status, int) and status >= 500):
            raise common.TransientError(f"opensearch bulk transient {status}: {exc}") from exc
        raise common.PermanentError(f"opensearch bulk failed {status}: {exc}") from exc
    except OpenSearchException as exc:
        raise common.TransientError(f"opensearch error: {exc}") from exc

    # bulk の後に orphan を掃除（再 ingest で chunk が減っても古い _id を残さない）
    deleted = _prune_orphans(client, inp.doc_id, [a["_id"] for a in actions])
    logger.info("IndexDocument done: doc_id=%s indexed=%d orphan_deleted=%d",
                inp.doc_id, indexed, deleted)
    return models.IndexOutput(
        doc_id=inp.doc_id, index=cfg.opensearch_index, indexed=indexed
    ).model_dump()
