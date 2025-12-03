# 振り返りシート自動保存ツール

塾講師が授業直後に記録する「振り返りシート」を、Google ドキュメントと Google ドライブへ自動保存する Flask 製 Web アプリです。ブラウザから講師・生徒・保存先を選び、テンプレートに沿って入力するだけで、指定フォルダへ適切なフォーマットでドキュメントを生成します。入力中はドラフトが自動保存され、通信断が発生しても途中から再開できます。

## なぜ Web アプリか

- 既存の講師 PC はブラウザ常駐が前提で、追加ソフトの配布やアップデートコストを削減できる
- Google OAuth / Drive / Docs API との連携が容易で、将来のマルチデバイス展開にも対応しやすい
- サーバー側でテンプレート更新やログ蓄積を一元管理できる

## 主な機能

- 講師・既存生徒・新規生徒・保存先 Google ドライブの選択 UI
- 「前回コピー」チェックで、直近のドキュメントを自動インポート
- テンプレートベースの入力フォーム（必須/任意項目を明確化）
- 入力中ドラフトの自動保存（5 秒ごと or 入力停止 3 秒後）
- 生徒名と同じフォルダを Drive 上に自動生成し、その配下へ保存
- 完了画面での保存先リンク・記録サマリ表示
- 社員番号＋パスワードによる講師ログイン（ログイン講師が自動的に記録を実施）
- 紐づく保護者メールアドレスへ Google ドキュメントの URL を自動送信
- 完了画面に Drive フォルダ URL も同時表示
- 生徒・保護者マスタを SQLite DB で一元管理（JSON から自動同期）
- 紐づく保護者メールアドレスへ Google ドキュメントの URL を自動送信

## セットアップ

```bash
cd "/Users/shimizutomoya/210_プラグミング/practice/AI講座/4-2-2/Google ドキュメント API"
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 必要な環境変数

| 変数名                        | 説明                                                                                       |
| ----------------------------- | ------------------------------------------------------------------------------------------ |
| `FLASK_SECRET_KEY`            | セッション保護用シークレット                                                               |
| `GOOGLE_OAUTH_CLIENT_SECRETS` | Google Cloud で発行した OAuth クライアント (Web) の JSON への絶対パス                      |
| `DEFAULT_DRIVE_PARENT_ID`     | 生徒フォルダ作成時の親フォルダ ID（任意。未指定の場合はルートに作成）                      |
| `DEFAULT_TEMPLATE_NAME`       | テンプレートファイル（`data/templates/`）のファイル名。未指定なら `reflection_template.md` |
| `FIXED_DRIVE_PARENT_ID`       | 記録を保存する Google Drive フォルダ ID。未設定時は `1o8Zxmet43AdIaSrUbVevhdBWeqsKjY-l` を使用 |

### ログイン情報（初期値）

初回起動時に `data/teachers.json` から SQLite (`app.db`) へ講師マスタが同期されます。  
同ファイルに記載された `employeeCode` / `password` を用いてログインしてください（例: `T1001 / kawamura123`）。  
DB 生成後は JSON を編集しても自動反映されないため、既存レコードの更新は管理用スクリプトや DB ツールで行ってください。

### 実行

```bash
source .venv/bin/activate
flask --app app run --reload
# もしくは
python app.py
```

ブラウザで `http://127.0.0.1:5000/` を開き、「Google と接続」ボタンから OAuth 認証を完了させてください。

## Google API 設定

1. Google Cloud Console で Docs API / Drive API / Gmail API を有効化
2. **OAuth クライアント ID（Web アプリ）** を作成し、承認済みリダイレクト URI に `http://127.0.0.1:5000/oauth/callback` を追加
3. ダウンロードした `client_secret_*.json` を任意の場所に保存し、`GOOGLE_OAUTH_CLIENT_SECRETS` でパスを指定
4. アプリを起動後、画面右上の「Google と接続」ボタンから一度ログインすると `oauth_token.json` にトークンが保存されます（既存ユーザーは Gmail 送信スコープ追加のため再認証が必要です）

## データ定義

- `data/teachers.json`: 講師マスタ
- `data/students.json`: 生徒マスタ（`folderId` が未設定の場合は初回登録時に Drive 上へ自動作成）
- `data/guardians.json`: 保護者マスタ（氏名・続柄・メールアドレスを定義）
- `data/student_guardians.json`: 生徒と保護者の紐づけ（`{ "student-id": ["guardian-id", ...] }` 形式）
- `data/templates/reflection_template.md`: 入力フォームの初期値 / Docs 生成フォーマットに使用

## データベースについて

- アプリ起動時に `app.db`（SQLite）が自動生成され、上記 JSON ファイル内容が未登録分のみ同期されます。
- 講師ログインや生徒・保護者マスタはすべて DB から参照され、フォーム送信時には `students` テーブルが更新されます。
- JSON から新規データを追加で取り込みたい場合は、`app.db` を削除して再起動するか、別途移行スクリプトを実行してください。
- メール送信テスト用に `student-morita` の保護者へ `t.shimizu064@gmail.com` / `shimitomo.eup@gmail.com` を登録済みです。
- 生徒に複数の保護者が紐づいている場合でも、リスト先頭の 1 名（代表保護者）のみにメールが送信されます。
- ドキュメントは Google API で作成された時点では講師のみ閲覧可能です。送信処理で代表保護者に「コメント可」権限を自動付与してから URL を共有します（Drive フォルダ全体の権限は変更しません）。

## 今後の拡張

- Firestore / Datastore などへのドラフト保存（現状はローカル JSON）
- Google OAuth を使った講師本人認証
- Slack やメールへの保存通知
- 生徒フォルダの自動共有設定

---

質問や改善要望があればお知らせください。README の更新も行います。
