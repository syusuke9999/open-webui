# W&B WeaveでAPIとの会話を確認する

この連携を有効にすると、Open WebUIのバックエンドからモデルAPIに送信した本文と、APIから返った応答をW&B Weaveに記録できます。モデル設定・システムプロンプト・検索結果などを反映した**送信直前の内容**を記録します。Weaveへの登録は応答の終了後に行います。

## 設定

初期状態は無効です。有効にすると、会話本文、システムプロンプト、検索で挿入された情報、ツールの引数・結果も指定したW&Bプロジェクトへ送信されます。その内容を保存してよいプロジェクトを指定してください。APIキーはチャットに貼り付けず、サーバーの環境変数に設定します。

| 環境変数                  | 設定内容                                                                      |
| ------------------------- | ----------------------------------------------------------------------------- |
| `ENABLE_WEAVE`            | `true`で記録する。初期値は`false`。                                           |
| `WEAVE_PROJECT`           | W&Bの`entity/project`。例：`your-entity/open-webui`。                         |
| `WANDB_API_KEY`           | そのプロジェクトに書き込めるW&B APIキー。モデルAPIのキーとは別。              |
| `WEAVE_MAX_CAPTURE_BYTES` | 入力・出力それぞれの記録量の目安。初期値は`2097152`（2 MiB）。1 KiB～32 MiB。 |

キーやプロジェクトが未設定の場合、対話ログインは行わず記録を無効にし、バックエンドに理由を出力します。環境変数の変更後はOpen WebUIを再起動します。

### Docker Compose

リポジトリのルートにある`.env`に、次を設定します。既存の`.env`がある場合は必要な項目だけ追加してください。

```dotenv
WEAVE_PROJECT=your-entity/open-webui
WANDB_API_KEY=your-wandb-api-key
```

追加したCompose設定を重ねてビルド・起動します。

```sh
docker compose -f docker-compose.yaml -f docker-compose.weave.yaml up -d --build
```

この追加設定は`USE_WEAVE=true`でSDKを組み込み、実行時に記録を有効にします。標準・Slimのどちらのイメージにも対応する構成です。既存の公式イメージを起動するだけでは、今回のソース変更は反映されません。

Dockerを直接ビルドする構成では`--build-arg USE_WEAVE=true`を追加し、実行時に上記の環境変数を渡します。通常のビルドではSDKを追加しません。

### Python環境から起動する場合

Open WebUIを実行しているPython 3.11/3.12の環境で、通常の依存関係に加えて以下をインストールします。

```sh
python -m pip install -r backend/requirements.txt -r backend/requirements-weave.txt
```

Slim構成では`requirements.txt`を`requirements-slim.txt`に置き換えます。既存のOpenTelemetry等のバージョン指定を維持するため、両方の依存ファイルを同じコマンドに渡してください。

PowerShellでの設定例です。その後、同じ環境で既存の手順に従ってOpen WebUIを起動・再起動してください。

```powershell
$env:ENABLE_WEAVE = 'true'
$env:WEAVE_PROJECT = 'your-entity/open-webui'
$env:WANDB_API_KEY = 'your-wandb-api-key'
```

## ログの見方

1. バックエンドのログで`Weave provider tracing enabled`を確認します。これは初期化成功の表示です。
2. Open WebUIで短いテスト会話を送信し、応答が終わるまで待ちます。
3. [W&B](https://wandb.ai/)で指定したプロジェクトを開き、**Weave → Traces**を表示します。
4. `openai.chat.completions`、`openai.responses`、`ollama.chat`、`anthropic.messages`などの行を開きます。
5. **Call**の`inputs`と`output`で送信・受信内容を確認します。Chat表示に対応する内容は**Chat**からも確認できます。

| 項目                        | 内容                                                                                         |
| --------------------------- | -------------------------------------------------------------------------------------------- |
| `inputs`                    | 実際に送信したJSON本文。`messages`、`input`、`instructions`、`tools`、生成パラメーターなど。 |
| `output`                    | 通常応答はAPIのJSONまたはテキスト。ストリーミングは結合した内容と`raw_events`。              |
| `output.raw_events`         | ストリームから読み取ったJSONイベント。記録上限内で保存する。                                 |
| `attributes`                | プロバイダー、接続先、呼び出し元が渡したチャット・メッセージ等の識別情報。                   |
| `summary.status_code`       | APIのHTTPステータス。接続前の失敗では未設定。                                                |
| `summary.capture_truncated` | `true`なら記録の省略・上限到達があり、全文ではない。                                         |
| `summary.usage`             | APIから取得できた場合のトークン使用量。                                                      |
| エラー・所要時間            | HTTPエラー、ストリーム内エラー、途中停止などと、実際の通信開始・終了時刻。                   |

ツール実行後の追加呼び出しや、タイトル・検索語などの内部生成も、対象の通信経路を使えば別の行になります。チャットID等の属性で関連する通信を絞り込めます。履歴の一括取り込みは行いません。

## 対象と記録の扱い

- OpenAI互換Chat Completions（Azureを含む）、Responses API、Ollamaの会話・生成、Anthropic Messagesの直接転送を対象にします。通常応答とストリーミングに対応します。
- OpenAIのResponsesをChat Completions形式へ変換する場合も、変換前のAPI応答を記録します。
- 通信ヘッダー・Cookieは記録処理へ渡しません。構造化された認証情報の項目は伏せ、URLのユーザー情報・クエリ・フラグメント、埋め込み画像・音声などを省きます。会話の自由文に書かれた秘密や個人情報をすべて自動検出する機能ではありません。
- 非常に長い本文は全体または末尾を省略します。ストリームも上限以降は記録を省きますが、ユーザーへの応答は省略しません。結合結果とイベントを同時に保存するため、上限近くではイベントの一部が省略される場合があります。
- Weaveの停止や送信失敗でチャットを失敗させない設計です。送信待ち件数にも上限を設けているため、障害・混雑・強制終了時の完全な記録を保証する監査ログではありません。
- ブラウザーから直接APIへ接続するDirect Connections、独自Pipe/Function内だけで行う通信、モデル一覧・埋め込み・画像生成・音声APIなどは対象外です。
- この変更はWeaveに送信する連携です。Open WebUI内にログ閲覧画面は追加していません。

## 検証

W&BのアカウントやモデルAPIへ通信しない回帰テストを実行できます。

```sh
python -m unittest discover -s backend/tests -p "test_weave*.py" -v
```

テストでは通常応答、各ストリーム形式、日本語の分割受信、ツール呼び出し、エラー、途中停止、マスク、上限、通信処理への組み込みを確認します。実際のプロジェクトへの到着は、上記のテスト会話で別途確認します。

実装はWeave SDKの[手動Call記録](https://docs.wandb.ai/weave/guides/tracking/create-call)と[WeaveClient API](https://docs.wandb.ai/weave/reference/python-sdk/trace/weave_client)を使用しています。
