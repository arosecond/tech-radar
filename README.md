# tech-radar

> 🇬🇧 English version: [README.en.md](README.en.md)

arXiv（Phase 2 では Hugging Face / 技術ブログも追加予定）をスキャンし、設定ファイルで定義した興味プロファイルでフィルタした上で、論文ごとの要約・キーポイント・タグを含む日次 Markdown ダイジェストを生成するマルチエージェント LLM キュレーションパイプライン。Notion データベースへのミラー出力と Slack 成功通知も実装済み。

このリポジトリの興味プロファイルは **3D 復元**（NeRF, 3D Gaussian Splatting, MVS, SfM, 深度推定, メッシュ復元, feed-forward 3D）になっているが、プロファイルは YAML 1ファイルなので任意のトピックに差し替え可能。

**運用コスト: $0/月。** デフォルトルーティングは全 stage がローカル Qwen 3.6-27B（llama.cpp 経由）で動く — スタックのどこにも有料 API 呼び出しはない。**マルチプロバイダ設計**：stage→model のルーティングは1つの YAML に集約され、`provider:` の1行を書き換えるだけで任意の stage をクラウドプロバイダ（Gemini / OpenAI 互換 / etc.）に切り替えられる。

## 出力されるもの

`output/digest-2026-04-30.md` のような日次 Markdown ファイルと、arXiv id をキーに Notion DB に upsert された各論文ページ。**実物のサンプルを [`docs/sample-digest.md`](docs/sample-digest.md) にコミット済み**。

論文ごとに、Summarizer は以下の Pydantic スキーマに従って出力する：

- **TL;DR** — 1〜2文の見出し
- **Key points** — 3〜5個の技術的箇条書き（abstract に忠実、数式・ベンチマーク名を保持）
- **Why it matters** — 興味プロファイルから見た意義の1文
- **Technical details** — 性能 / 処理速度 / 必要GPU（不明な項目は幻覚せず `N/A` にフォールバック）
- **Repository** — GitHub URL と SPDX ライセンス（enrich step で GitHub API から実取得）
- **Tags + paper type + datasets** — Tagger stage が制約語彙の中から付与

## なぜ作ったか

エンドツーエンドで LLM キュレーションシステムを構成する各ピースを示すことが目的：

- **マルチプロバイダ × 単一設定でのルーティング** — `config/models.yaml` で各 stage（Filter / Summarizer / Tagger / Ranker）を provider+model にマップ。同じクライアントコードが任意の OpenAI 互換バックエンドで動く。デフォルトはコスト都合で全ローカル、`provider:` を書き換えれば即クラウドへ。
- **記事を取りこぼさない構造化出力** — `response_format=json_object` + Pydantic 検証 + 自動リトライ + マークダウンフェンス救出。ローカルモデルからの JSON 破損は復旧されてロストしない。
- **stage 単位の thinking モード A/B** — `enable_thinking` を YAML で stage 別に指定。比較用に `models-thinking.yaml` を同梱しているので、昇格判断は実測ベース（_Engineering decisions_ 参照）。
- **狭く差し替え可能なソース層** — arXiv → HF → RSS、すべて同じ `Article` スキーマの裏に。
- **累積ダイジェスト** — 1回の run 失敗で過去出力が消えない設計。`tech-radar render` は LLM を呼ばず DuckDB から digest を再生成（コスト $0）。

## アーキテクチャ

```
Sources                  Agents                                        Storage           Output
─────────                ────────                                      ─────────         ─────────
arXiv API   ─┐
HF Hub MCP  ─┼── Article ─→ Filter (provider per models.yaml)
RSS feeds   ─┘                  │ keep?
                                ▼
                            Summarizer (provider per models.yaml)
                                │
                                ▼
                            Tagger (provider per models.yaml) ─→ DuckDB ─→ Markdown digest
                                                                      ├→ Notion DB
                                                                      └→ Slack notify
```

### デフォルトルーティング（all-local）

| Stage | Provider | 備考 |
|---|---|---|
| Filter | local Qwen 3.6-27B via llama.cpp | 大量の二値判定。$0、レート制限なし |
| Summarizer | local Qwen 3.6-27B via llama.cpp | `enable_thinking: true`（A/B で thinking-off に勝利。数式・ベンチマーク名を忠実に保持） |
| Tagger | local Qwen 3.6-27B via llama.cpp | 制約語彙 + JSON モード |
| Ranker (Phase 2) | TBD | 暫定 Summarizer 同等 |

当初は mori + Gemini のハイブリッドルーティングを試作したが、このアカウントの Gemini 無料枠が ~20 req/日（モデル非依存）と日次バッチには不足することが判明。有料移行ではなく all-local に切り替えた。マルチプロバイダ配管はそのまま生きているので、`config/models.yaml` の `provider:` をいじるだけでコードに触らず再ルーティング可能。

## 設計判断（Engineering decisions）

実プロダクトとしてレビュアーが知りたい設計トレードオフをいくつか言語化しておく：

- **Hybrid → all-local 移行。** 当初設計では Summarizer を Gemini Flash に投げて要約品質を上げ、Filter / Tagger をローカル 27B に残す構成だった。しかし Gemini AI Studio の無料枠が当アカウントで ~20 req/日（モデル非依存）と判明し、日次バッチには非実用。有料化ではなく all-local に切り替え。マルチプロバイダ配管と「stage ごとに役割分担する」思想はコードに残しているので、コスト判断が変わった瞬間に元のルーティングに戻せる。
- **Summarizer の thinking モード ON（A/B 検証済）。** 共通22論文で `enable_thinking: true` vs `false` を side-by-side 比較したところ、thinking-on の方が数式記号（`$x_\theta$`）を保持し、ベンチマーク名（例: _1-NFE_）を具体名で言及し、"hand-wavy" な言い換えを避けていた。thinking-on はトークン予算を食うので `max_tokens: 4500` とセットで本番化。同じ run で両構成とも JSON 破損は0件。Filter / Tagger は thinking-off のまま — 不要なレイテンシは入れない。
- **累積ダイジェスト。** 初期は途中 stage の失敗で digest が空になる事故があった。修正後は、tag が完走した記事だけを DuckDB に書き込み、digest renderer が DuckDB から直近 N 日分を引いて Markdown を1ファイル生成する設計。`tech-radar render` は LLM を呼ばず再生成のみを行う $0 コマンドで、プロンプト改善イテレーション時に重宝する。
- **部分失敗に強い dedup。** 記事が `seen` テーブルに登録されるのは Filter 段階ではなく Tagger 完走時。途中失敗した記事は次回 run で自動復帰する。
- **レート制限ハンドリングは agent ではなく client 側に集約。** OpenAI 互換クライアント側で Gemini を 6.5秒/req にスロットリングし、llama.cpp の 503 には10秒バックオフ + リトライ。各 agent はバックエンドの素性に無頓着でいられる。
- **enrich step を後段に。** タグ付け完了後、リポジトリ URL を GitHub API に投げて実 SPDX ライセンス文字列を取得。モデルが「ぽい」だけで `Apache 2.0` と幻覚することを防ぎ、フィールドは正しい値か `N/A` のどちらかになる。

## プロジェクト状況

- **Phase 1 (このリポジトリ):** arXiv source · Filter / Summarizer / Tagger · DuckDB dedup · Markdown digest · Notion DB output · Slack 通知 · Windows Task Scheduler 日次自動実行 · CLI · multi-provider client
- **Phase 2:** Hugging Face source · RSS source · Ranker agent · 著者所属機関ハイライト（Semantic Scholar）を digest / Notion / Slack に反映
- **Phase 3:** Raspberry Pi cron デプロイ · Langfuse オブザーバビリティ

## 運用（Operations）

無人での日次実行を前提に組まれている。開発機では：

- `run_pipeline.bat` を **Windows Task Scheduler**（`TechRadarPipeline`、毎日 06:05）に登録。中身：
    1. Docker サービスと Docker Desktop GUI を起動（GUI が立たないとデーモンが上がらない）
    2. `docker ps` でデーモン準備完了を最大5分ポーリング
    3. `python -m tech_radar.cli run` 実行
    4. 5分以内に Docker が上がらなかった場合は `tech-radar notify-docker-fail` でログパス付き Slack アラートを送出
- llama.cpp コンテナ `llamacpp-mori` は `restart: unless-stopped` で動いているので、Docker が来た瞬間に自動復帰。
- **Slack 通知**（Incoming Webhook 方式。URL は `.env` の `SLACK_WEBHOOK_URL`）：
    - **成功**: 新規論文件数 + タイトル先頭20件をチャンネルに投稿
    - **失敗**: 落ちた stage 名 + ログパス
    - **Docker not ready**: 上記 bat レベルアラート
    - `SLACK_WEBHOOK_URL` 未設定時は notify 全関数が静かな no-op になる
- ログは `data/logs/pipeline_YYYYMMDD.log`（gitignore 済）に出力。

## セットアップ

```bash
# 依存関係インストール（uv が venv を管理）
uv sync

# プロバイダ設定
cp .env.example .env
# MORI_BASE_URL のデフォルトは http://localhost:8080/v1 — 別の場所で llama.cpp を立てている場合のみ変更
# GEMINI_API_KEY は任意 — config/models.yaml で provider: gemini に切り替えた stage がある場合のみ必要
# NOTION_* と SLACK_WEBHOOK_URL も任意 — 未設定でもパイプラインは動く

# 設定ロード確認
uv run tech-radar show-profile
uv run tech-radar show-models

# 設定済みプロバイダに小さな構造化呼び出しを送って疎通確認
uv run tech-radar ping
```

### ローカル Qwen の前提

デフォルトルーティングは全 stage が `MORI_BASE_URL` 上で動く llama.cpp `server`（Qwen 3.6-27B 互換モデル）に向く。`unsloth/Qwen3.6-27B-GGUF`（24GB GPU で UD-Q4_K_XL）の動作確認済み `docker-compose` は別ドキュメントで管理。ローカル GPU が無い場合は `config/models.yaml` を `provider: gemini` に書き換え、`.env` に `GEMINI_API_KEY` を設定する。

## 実行

```bash
# 日次ダイジェスト、2日 lookback
uv run tech-radar run

# 7日 lookback、verbose、dry-run（seen に登録しない）
uv run tech-radar run --lookback-days 7 --dry-run --verbose

# Notion push を1回スキップ
uv run tech-radar run --no-notion

# DuckDB から digest を LLM 呼ばず再生成（$0）
uv run tech-radar render
```

ダイジェストは `output/digest-YYYY-MM-DD.md` に出力される。Notion 設定済みなら各タグ済み論文が指定 DB にページとして upsert される。Slack 設定済みなら成功サマリが投稿される。

### Notion 出力

各 run はタグ済み論文を arXiv id をキーに Notion DB に upsert する：

- **`NOTION_PARENT_PAGE_ID` 設定済での初回 run:** そのページ配下に新規 DB を bootstrap（Title / Topics / Method type / Phase 2 Ranker 用 Score 等のスキーマ込）。生成された DB id がログに出るので、それを `NOTION_DATABASE_ID` に移すと2回目以降は bootstrap をスキップ。
- **`NOTION_DATABASE_ID` 設定済の run:** 新規論文はページとして作成、既存論文はプロパティを更新。

タグ済みなのに Notion 未連携の論文をバックフィルしたい時、あるいはスキーマ変更後に全件再同期したい時：

```bash
# DuckDB にあるが notion_page_id を持たない論文だけ push
uv run tech-radar notion-sync

# 全ページのプロパティを強制再 push
uv run tech-radar notion-sync --all
```

push は best-effort 設計 — Notion エラーでパイプラインを止めない。Markdown ダイジェストが source of truth。

## 設定ファイル

- `config/interest_profile.yaml` — トピックと重み、および Ranker 用の reader context
- `config/sources.yaml` — どの arXiv カテゴリ / HF query / RSS フィードを取りに行くか
- `config/models.yaml` — どの provider+model がどの stage を担当するか

3つすべて hot-swappable。プロファイル YAML を差し替えれば別トピックのキュレータに転用できるし、`models.yaml` を差し替えればコードに触らず stage を別プロバイダにルーティングできる。

## ファイル構成

```
src/tech_radar/
├── schemas.py            # Pydantic models — パイプラインの背骨
├── sources/
│   └── arxiv_source.py   # arXiv API → Article
├── agents/
│   ├── _client.py        # OpenAI 互換 マルチプロバイダクライアント + Pydantic 構造化出力
│   ├── filter_agent.py   # stage 非依存。models.yaml でルーティング
│   ├── summarizer.py     # stage 非依存。models.yaml でルーティング
│   └── tagger.py         # stage 非依存。models.yaml でルーティング
├── outputs/
│   ├── markdown.py       # 日次ダイジェスト renderer
│   └── notion.py         # Notion DB upsert（data sources API、スロットリング付き）
├── enrich.py             # GitHub API → リポごとの SPDX ライセンス
├── notifier.py           # Slack Incoming Webhook（success / error / docker-fail）
├── storage.py            # DuckDB: dedup + タグ済み論文アーカイブ
├── pipeline.py           # オーケストレーション + stage→model 解決
└── cli.py                # Typer エントリーポイント (run / render / notion-sync / notify-docker-fail / show-profile / show-models / ping)
```

## ライセンス

MIT — [`LICENSE`](LICENSE) を参照。
