# ingest 各 Lambda（parse / chunk_embed / index）が共有する単一コンテナイメージ。
# Function ごとに ImageConfig.Command（CMD）を上書きする。
#   ProcessDocument : parse.handler.handler        (1 PDF を丸ごと → md)
#   EmbedDocument   : chunk_embed.handler.handler  (md → chunk → embed)
#   IndexDocument   : index.handler.handler        (embeddings JSON → OpenSearch)
# Python 3.13: numpy/onnxruntime/pymupdf 等の prebuilt wheel が揃う安定版。
# （3.14 は cp314 wheel 未提供でソースビルド→コンパイラ無しで失敗するため不可）。
FROM public.ecr.aws/lambda/python:3.13

# 依存のインストール（レイヤーキャッシュのため requirements を先に COPY）
# rapid-layout は GUI 版 opencv-python（libGL 必須）を依存に持つが Lambda に libGL は無い。
# インストール後に headless 版へ差し替える（同じ cv2 モジュールを提供）。
COPY requirements.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements.txt \
 && pip uninstall -y opencv-python opencv-contrib-python \
 && pip install --no-cache-dir opencv-python-headless

# RapidLayout のレイアウトモデル(pp_doc_layoutv3 ~124MB)をビルド時にイメージへ焼き込む
# （実行時にダウンロードしない＝コールドスタート短縮＋外部 mirror 非依存）。
RUN python -c "from rapid_layout import RapidLayout; from rapid_layout.utils.typings import RapidLayoutInput, ModelType; RapidLayout(cfg=RapidLayoutInput(model_type=ModelType.PP_DOC_LAYOUTV3))"

# 共有ヘルパー・設定・型 + プロンプト + 各ステップのハンドラ
COPY common.py config.py models.py ${LAMBDA_TASK_ROOT}/
COPY prompts/  ${LAMBDA_TASK_ROOT}/prompts/
COPY parse/        ${LAMBDA_TASK_ROOT}/parse/
COPY chunk_embed/  ${LAMBDA_TASK_ROOT}/chunk_embed/
COPY index/        ${LAMBDA_TASK_ROOT}/index/

# 既定 CMD（process）。各 Function 作成時に ImageConfig.Command で上書きする。
CMD ["parse.handler.handler"]
