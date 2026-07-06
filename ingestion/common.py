"""S3 / Bedrock / ロギング / 例外の共有ヘルパー（全 Lambda が import）。

外部呼び出しの失敗は TransientError(Retry)/PermanentError(Catch) に正規化する。
認証は Lambda 実行ロール。boto3 クライアントは遅延生成・再利用。
"""

from __future__ import annotations

import json
import logging
import os
import re
from functools import lru_cache
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


class PipelineError(Exception):
    pass


class TransientError(PipelineError):
    """一時障害（スロットリング・5xx 等）→ SF で Retry。"""


class PermanentError(PipelineError):
    """恒久障害（入力不正・未対応モデル等）→ SF で Catch。"""


_LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(_LOG_LEVEL)
    return logger


_logger = get_logger(__name__)
_AWS_REGION = os.environ.get("AWS_REGION") or os.environ.get(
    "AWS_DEFAULT_REGION", "ap-northeast-1"
)
_TRANSIENT_CODES = {
    "ThrottlingException", "TooManyRequestsException", "ServiceUnavailableException",
    "ServiceUnavailable", "InternalServerException", "InternalServerError",
    "ModelTimeoutException", "ModelNotReadyException", "RequestTimeout", "SlowDown",
}


def _wrap_client_error(exc: ClientError, context: str) -> PipelineError:
    code = exc.response.get("Error", {}).get("Code", "")
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
    if code in _TRANSIENT_CODES or (isinstance(status, int) and status >= 500):
        return TransientError(f"{context}: transient {code}: {exc}")
    return PermanentError(f"{context}: permanent {code}: {exc}")


@lru_cache(maxsize=1)
def s3_client() -> Any:
    return boto3.client("s3", region_name=_AWS_REGION)


@lru_cache(maxsize=1)
def bedrock_runtime_client() -> Any:
    cfg = Config(
        region_name=_AWS_REGION,
        retries={"max_attempts": 4, "mode": "adaptive"},
        read_timeout=120,
        connect_timeout=10,
    )
    return boto3.client("bedrock-runtime", config=cfg)


# --- S3 ---
def download_bytes(bucket: str, key: str) -> bytes:
    _logger.info("S3 get: s3://%s/%s", bucket, key)
    try:
        return s3_client().get_object(Bucket=bucket, Key=key)["Body"].read()
    except ClientError as exc:
        raise _wrap_client_error(exc, f"download s3://{bucket}/{key}") from exc


def put_bytes(bucket: str, key: str, data: bytes, content_type: str) -> None:
    _logger.info("S3 put: s3://%s/%s (%d bytes)", bucket, key, len(data))
    try:
        s3_client().put_object(Bucket=bucket, Key=key, Body=data, ContentType=content_type)
    except ClientError as exc:
        raise _wrap_client_error(exc, f"put s3://{bucket}/{key}") from exc


def get_text(bucket: str, key: str) -> str:
    return download_bytes(bucket, key).decode("utf-8")


def put_text(bucket: str, key: str, text: str, content_type: str) -> None:
    put_bytes(bucket, key, text.encode("utf-8"), content_type)


def get_json(bucket: str, key: str) -> dict | None:
    """JSON を取得。存在しなければ None（キャッシュヒット判定用）。"""
    try:
        resp = s3_client().get_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code", "") in ("NoSuchKey", "404"):
            return None
        raise _wrap_client_error(exc, f"get_json s3://{bucket}/{key}") from exc
    return json.loads(resp["Body"].read().decode("utf-8"))


def put_json(bucket: str, key: str, obj: Any) -> None:
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    put_bytes(bucket, key, body, "application/json; charset=utf-8")


# --- Bedrock: VLM (Claude vision, Converse API) ---
def converse_image(
    *,
    model_id: str,
    image_png: bytes,
    prompt: str,
    max_tokens: int = 4096,
    temperature: float | None = 0.0,
) -> tuple[str, int, int]:
    """PNG + プロンプトを Converse へ送り (本文, 入力トークン, 出力トークン) を返す。

    画像を先・テキストを後（AWS media-then-text）。SDK が bytes を base64 化。
    temperature=None で省略（Opus 4.7/4.8 は temperature 非対応）。
    """
    inference: dict = {"maxTokens": max_tokens}
    if temperature is not None:
        inference["temperature"] = temperature
    try:
        resp = bedrock_runtime_client().converse(
            modelId=model_id,
            messages=[{
                "role": "user",
                "content": [
                    {"image": {"format": "png", "source": {"bytes": image_png}}},
                    {"text": prompt},
                ],
            }],
            inferenceConfig=inference,
        )
    except ClientError as exc:
        raise _wrap_client_error(exc, f"converse model={model_id}") from exc

    usage = resp.get("usage", {})
    _logger.info(
        "VLM usage model=%s in=%s out=%s", model_id,
        usage.get("inputTokens"), usage.get("outputTokens"),
    )
    contents = resp.get("output", {}).get("message", {}).get("content", [])
    parts = [c["text"] for c in contents if "text" in c]
    if not parts:
        raise PermanentError(f"converse returned no text (model={model_id})")
    return (
        "\n".join(parts).strip(),
        int(usage.get("inputTokens", 0)),
        int(usage.get("outputTokens", 0)),
    )


# --- Bedrock: 埋め込み (Amazon Titan / Cohere) ---
_COHERE_BATCH = 96


def embed_texts(
    texts: list[str],
    *,
    model_id: str,
    input_type: str = "search_document",
    output_dimension: int = 1024,
) -> tuple[list[list[float]], int]:
    """テキスト群 → 埋め込みベクトル。Returns (vectors, dim)。

    既定は Amazon Titan Text Embeddings V2（normalize=True で単位ベクトルを返すため、
    OpenSearch 側は innerproduct でコサイン相当になる）。評価用に Cohere にも切替可能
    （input_type は Cohere のみ有効）。
    """
    if not texts:
        return [], output_dimension
    if model_id.startswith("amazon.titan-embed"):
        return _embed_titan(texts, model_id, output_dimension)
    if model_id.startswith("cohere.embed"):
        return _embed_cohere(texts, model_id, input_type, output_dimension)
    raise PermanentError(f"unsupported embedding model: {model_id}")


def _invoke_json(model_id: str, body: dict) -> dict:
    try:
        resp = bedrock_runtime_client().invoke_model(
            modelId=model_id, body=json.dumps(body),
            accept="application/json", contentType="application/json",
        )
    except ClientError as exc:
        raise _wrap_client_error(exc, f"invoke_model model={model_id}") from exc
    return json.loads(resp["body"].read())


def _embed_cohere(
    texts: list[str], model_id: str, input_type: str, output_dimension: int
) -> tuple[list[list[float]], int]:
    vectors: list[list[float]] = []
    for start in range(0, len(texts), _COHERE_BATCH):
        payload = _invoke_json(model_id, {
            "input_type": input_type,
            "texts": texts[start : start + _COHERE_BATCH],
            "embedding_types": ["float"],
            "output_dimension": output_dimension,
            "truncate": "RIGHT",
        })
        embeddings = payload.get("embeddings")
        if isinstance(embeddings, dict):  # embedding_types 指定時は dict で返り得る
            embeddings = embeddings.get("float")
        if not embeddings:
            raise PermanentError(f"cohere embed returned no embeddings (model={model_id})")
        vectors.extend(embeddings)
    return vectors, len(vectors[0])


def _embed_titan(
    texts: list[str], model_id: str, output_dimension: int
) -> tuple[list[list[float]], int]:
    """Titan Text Embeddings V2。1リクエスト1テキスト。normalize=True で単位ベクトル。"""
    vectors: list[list[float]] = []
    for text in texts:
        payload = _invoke_json(model_id, {
            "inputText": text,
            "dimensions": output_dimension,
            "normalize": True,
        })
        emb = payload.get("embedding")
        if not emb:
            raise PermanentError(f"titan embed returned no embedding (model={model_id})")
        vectors.append(emb)
    return vectors, len(vectors[0])


# --- 日本語向けトークン数の近似（CJK≒1, それ以外≒4文字/token） ---
_CJK_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿ｦ-ﾟ]")


def estimate_tokens_ja(text: str) -> int:
    cjk = len(_CJK_RE.findall(text))
    return cjk + (len(text) - cjk + 3) // 4
