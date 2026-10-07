# FileMaker × AI ワークショップ 配布キット

FileMaker の DDR（Database Design Report）を Claude に読ませて、設計を調べたり
引き継ぎ資料を作ったりするための一式です。

```
配布用/
├── README.md                     ← いまここ
├── DDR/                          サンプル DDR（86 MB / 匿名化済み）
│   ├── 概要.xml
│   └── 【IRCA SYSTEM ver01】_fmp12.xml
└── .claude/skills/fmddr/         Claude Code 用の skill（隠しフォルダ）
    ├── SKILL.md
    ├── scripts/fmddr.py
    └── reference/ddr-xml-map.md
```

## 1. skill を入れる

**インストール作業はありません。** この `配布用` の**中身を作業フォルダの直下に置いて**、
VSCode でそのフォルダを開き直すだけです。

```
ai-workshop/            ← VSCode で開くフォルダ
├── DDR/
└── .claude/skills/fmddr/
```

`.claude/skills/` に置かれた skill は、**そのフォルダを開いている間だけ**使えます
（プロジェクト skill）。`/` を押して `fmddr` が候補に出れば成功です。出てこないときは
**新しい会話を始める**か、VSCode を再読み込みしてください。

> `.claude` は先頭がドットの**隠しフォルダ**です。macOS の Finder では
> `Command + Shift + .` で表示できます。**共有ドライブや圧縮ファイルでやり取りするときに
> 抜け落ちていないか**を確認してください。

どのフォルダを開いても使えるようにしたい場合は、ホームに置きます。

```sh
mkdir -p ~/.claude/skills
cp -R .claude/skills/fmddr ~/.claude/skills/
```

Windows（PowerShell）:

```powershell
New-Item -ItemType Directory -Force "$HOME\.claude\skills" | Out-Null
Copy-Item -Recurse .claude\skills\fmddr "$HOME\.claude\skills\"
```

- **Python 3 が必要です**（追加ライブラリは不要）。macOS で入っていなければ
  `xcode-select --install`
- 会社の Claude Code marketplace を使っている場合は
  `claude plugin install fmddr@supportas` でも入ります

```sh
# 動くかどうかの確認
python3 .claude/skills/fmddr/scripts/fmddr.py --version
```

## 2. 試す

Claude Code を `配布用` フォルダで開いて、日本語で頼むだけです。

```
DDR/ のサンプル DDR を展開して、どんなシステムか教えて
```

自分でコマンドを打つ場合:

```sh
FMDDR=.claude/skills/fmddr/scripts/fmddr.py

# ① どんな DDR か見る
python3 $FMDDR probe -i DDR/概要.xml

# ② Markdown + JSON 索引に分解する（2〜3 秒）
python3 $FMDDR split -i DDR/概要.xml -o ./split --clean

# ③ フィールドの使用箇所を逆引きする
python3 $FMDDR xref -d ./split --field "請求書DB::請求先CD"

# ④ 使われていない要素を洗い出す
python3 $FMDDR audit -d ./split

# ⑤ 引き継ぎ資料の骨組みを作る
python3 $FMDDR handover -d ./split -o ./handover --clean
```

⑤ で作った `handover/` は、件数・一覧・依存関係・リスク箇所が埋まった状態で、
説明の部分だけが `要執筆` の枠として空いています。その枠を Claude に埋めてもらうと
引き継ぎ資料になります。

```
handover/ の要執筆の枠を、split/ の中身を読んで埋めて
```

## 3. サンプル DDR について

**中身はダミーに置き換えてあります。** 実在の担当者氏名・取引先名・パスワードは
すべて架空の値です（山田 太郎 / 株式会社サンプル電子 / DummyPass01 など）。
アカウントの説明欄は中身ごと「ダミー説明」に差し替えてあります。

ただし**業務の構造そのものは実物**なので、社外へ再配布はしないでください。

- 61 テーブル / 2,579 フィールド / 513 スクリプト / 214 レイアウト
- 366 テーブルオカレンス / 298 リレーション / 132 値一覧
- FileMaker 22.0.6 で出力した DDR

## 4. 自分の FileMaker ファイルで試す

FileMaker Pro の **[ツール] → [データベースデザインレポート]** で、形式に **XML** を
選んで出力します。出力先フォルダに `概要.xml` と `<ファイル名>_fmp12.xml` ができるので、
`-i` に `概要.xml` を渡してください。

**自分の業務ファイルの DDR は取り扱いに注意してください。**

- スクリプトのコメント・アカウント一覧・値一覧に、実在の顧客名・担当者氏名・
  社内アカウント名が平文で入っていることがあります
- **アカウントの説明欄やスクリプト内に、パスワードが平文で書かれている場合があります**
  （実際にそういう DDR が存在します）。fmddr は説明欄を既定で伏せますが、
  スクリプト内の埋め込みは伏せません
- DDR や展開結果を、外部サービスへの貼り付け・公開リポジトリへのコミット・
  社外への共有をする前に、中身を確認してください

`handover` の 8 章「改修時の注意点」が、認証情報が埋め込まれている箇所を
（値は出さずに）指してくれます。まずそこを見るのが早いです。

## 困ったら

| 症状 | 対処 |
| --- | --- |
| skill が呼ばれない | 新しい会話を始めるか VSCode を再読み込み。開いているフォルダの直下に `.claude/skills/fmddr/SKILL.md` があるか確認 |
| `python3: command not found` | macOS: `xcode-select --install` / Windows: Microsoft Store から Python |
| `cat` したら文字が飛び飛びに見える | DDR は UTF-16。破損ではないので、中身は fmddr 経由で見る |
| `extract` が何も返さない | パス式の誤りとは限らない。`nodes` で実際の構造を確認する（SKILL.md 5 章） |
