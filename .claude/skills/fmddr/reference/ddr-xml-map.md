# DDR（FMPReport）XML 構造マップ

fmddr が整形していない情報を `nodes` / `extract` で直接取るときの地図。
実測は FileMaker Pro 22.0.6 の DDR。

## ルートと種別

| ルート要素 | `type` 属性 | 内容 |
|---|---|---|
| `FMPReport` | `Summary` | 概要ファイル。`File/@link` で本体 XML を指す。各カタログの件数だけ持つ |
| `FMPReport` | `Report` | 本体（`*_fmp12.xml`）。実体はすべてここ |
| `FMSaveAsXML` | — | SaveAsXML。DDR とは別形式（fmddr の `split` は非対応） |

エンコーディングは UTF-16（BOM 付き）。宣言に `encoding` が無いが、expat が BOM で
判定するため `ET.iterparse(path)` でそのまま読める。

## `/FMPReport/File/` 直下のカタログ

| パス | 内容 |
|---|---|
| `BaseTableCatalog/BaseTable` | テーブル。配下 `FieldCatalog/Field` がフィールド定義 |
| `RelationshipGraph/TableList/Table` | テーブルオカレンス（TO）。`@baseTable` で実テーブル、子 `FileReference` で外部ファイル |
| `RelationshipGraph/RelationshipList/Relationship` | リレーション。`LeftTable` / `RightTable` / `JoinPredicateList` |
| `LayoutCatalog/(Group/)*Layout` | レイアウト（グループで入れ子） |
| `ValueListCatalog/ValueList` | 値一覧 |
| `ScriptCatalog/(Group/)*Script` | スクリプト（グループで入れ子）。配下 `StepList/Step` |
| `CustomFunctionCatalog/CustomFunction` | カスタム関数 |
| `CustomMenuCatalog/CustomMenu` | カスタムメニュー。配下 `MenuItemList/MenuItem` |
| `CustomMenuSetCatalog/CustomMenuSet` | メニューセット |
| `AccountCatalog/Account` | アカウント（`@privilegeSet` `@status`、子 `Description`） |
| `PrivilegesCatalog/PrivilegeSet` | 権限セット。配下に**テーブルごとの** `BaseTable` と `Create/Edit/Delete/View/FieldAccess` |
| `ExtendedPrivilegeCatalog/ExtendedPrivilege` | 拡張アクセス権 |
| `ExternalDataSourcesCatalog/FileReference` | 外部データソース |
| `ThemeCatalog/Theme` | テーマ（配下は CSS 相当で巨大） |
| `Options` | ファイルオプション（`OnOpen` / `WindowTriggers` / `DefaultTheme` …） |
| `AuthFileCatalog/Outbound/OutboundAuthorization` | ファイルアクセスの承認 |
| `BaseDirectoryCatalog/BaseDirectoryList/BaseDirectory` | 基準ディレクトリ |

**同じタグ名が「定義」と「参照」の両方で使われる。** 例えば `Layout` 配下の
`<Script name="…"/>` はトリガの参照、`ScriptCatalog` 配下の `<Script>` が定義。
タグ名ではなく**絶対パスで判定する**こと。

## 参照の共通形（相互参照はこれで拾える）

DDR は他オブジェクトへの参照を、種別によらず同じ形で持つ。

| タグ | 意味 | 主な属性 |
|---|---|---|
| `Field` | フィールド参照 | `table`（TO 名）, `id`, `name`, `repetition` |
| `Script` | スクリプト参照 | `id`, `name` |
| `Layout` | レイアウト参照 | `id`, `name` |
| `Table` | TO 参照 | `id`, `name` |
| `ValueList` | 値一覧参照 | `id`, `name` |
| `FileReference` | 外部ファイル参照 | `id`, `name` |
| `CustomMenu` / `CustomMenuSet` / `Theme` | 各参照 | `id`, `name` |

`Field` に `table` 属性が無い場合はインポート/エクスポートのマッピング先で、
TO が特定できない（fmddr は別枠に分けている）。

## スクリプト（`Script/StepList/Step`）

- `Step/@id` ステップ種別（**言語非依存**）、`@name` は表示名（ローカライズ済み）
- `Step/@enable="False"` 無効化ステップ。**`StepText` の先頭に `//  ` が既に入る**
- `Step/StepText` スクリプトワークスペース表示そのままのテキスト。**人間向けはこれを読む**
- 個別の子要素（`Field` / `Script` / `Layout` / `Calculation` …）は機械処理向け

制御構造のステップ ID（字下げの復元に使う）:

| id | ステップ | id | ステップ |
|---|---|---|---|
| 68 | If | 71 | Loop |
| 125 | Else If | 72 | Exit Loop If |
| 69 | Else | 73 | End Loop |
| 70 | End If | 89 | # コメント |

`StepText` が空のコメントステップは「空行のコメント」。
対応する `If` / `Loop` が無い孤立した `Else` / `End If` が実在する（無効化された
ブロックの残骸）ので、字下げ計算はマイナスに落ちないようガードする。

## フィールド（`BaseTable/FieldCatalog/Field`）

`@id` `@name` `@dataType`（Text/Number/Date/Time/TimeStamp/Container）
`@fieldType`（Normal/Calculated/Summary）

| 子要素 | 内容 |
|---|---|
| `Comment` | フィールドコメント |
| `Calculation@table` | 計算フィールドの式（`@table` は計算コンテキストの TO） |
| `DisplayCalculation/Chunk` | 式を種別付きに分解（`type="FieldRef"` の子 `Field` が依存フィールド） |
| `AutoEnter` | 自動入力。`@calculation` `@lookup` `@constant` `@furigana` `@value`（作成/修正情報）`@alwaysEvaluate` |
| `AutoEnter/Lookup` | ルックアップ（`Table` = 起点 TO, `Field` = 参照元, `NoMatchCopyOption`） |
| `AutoEnter/Serial` | シリアル番号（`@nextValue` `@increment` `@generate`） |
| `Validation` | 入力値の制限（`NotEmpty` / `Unique` / `Existing` / `StrictDataType` / `ValueList` / `@maxLength` / `Calculation` / `ErrorMessage`） |
| `Storage` | `@global` `@index` `@autoIndex` `@indexLanguage` `@maxRepetition` `@storeCalculationResults` |
| `SummaryInfo` | 集計（`@operation`, `SummaryField/Field`） |

## レイアウト（`Layout`）

- 直下: `Table`（TO）, `Theme`, `CustomMenuSet`, `Options`, `ScriptTriggers`, `Object`
- `Object/@type` = Field / Text / Portal / Button / GroupButton / PopoverButton /
  TabControl / TabPanel / SlideControl / SlidePanel / WebViewer / Chart / Group /
  Rectangle / Image …
- `Object/Bounds@top,left,bottom,right` 位置
- 型ごとの中身は `<型>Obj`:

| 型 | 中身の在り処 |
|---|---|
| Field | `FieldObj/Name`（`TO::フィールド`）、`FieldObj/DDRInfo/Field` |
| Text | `TextObj/CharacterStyleVector/Style/Data` を連結したものが本文。`TextObj/FieldList/Field` はマージフィールド |
| Portal | `PortalObj/TableAliasKey`（関連 TO）、`@numOfRows`、配下に `Object` |
| Button 系 | `ButtonObj/Step/StepText`（単一ステップ）、`Image@name` |
| TabControl / SlideControl | 配下 `Object`（TabPanel / SlidePanel）→ さらに `Object` |
| Chart | `ExternalObj`（`Title/Text/Calculation`, `ChartSeries`） |

- `Object/HideCondition/Calculation` 非表示条件
- `Object/ConditionalFormatting/Item/Condition/Calculation` 条件付き書式
- スクリプトトリガは **`ScriptTriggers/Trigger` の下**（`Layout` 直下・`Object` 直下の
  両方に付く）。`Trigger@event` + 子 `Script@name` + `TriggerText`。
  `Trigger` を直下に探すと 1 件も取れないので注意
- `Styles/LocalCSS` `ExtendedAttributes` は見た目の情報（量が多い）

## リレーション（`RelationshipList/Relationship`）

- `LeftTable` / `RightTable`: `@name`（TO）, `@cascadeCreate`, `@cascadeDelete`,
  子 `SortList`
- `JoinPredicateList/JoinPredicate@type`: `Equal` / `NotEqual` / `LessThan` /
  `LessThanOrEqual` / `GreaterThan` / `GreaterThanOrEqual` / `CartesianProduct`
- 述語のフィールドは `LeftField/Field` と `RightField/Field`

## 値一覧（`ValueList`）

- `Source@value`: `Custom` → `CustomValues/Text`（改行区切り）
- `Source@value`: `Field` → `PrimaryField` / `SecondaryField`（`@show` `@sort`）と
  `ShowRelated`（`@value` と起点 `Table`）

## カスタムメニュー（`CustomMenu`）

- `Options@browseMode/@findMode/@previewMode/@macPlatform/@winPlatform`
- `BaseMenu@name` 元にした標準メニュー
- `Conditions/Install/Calculation` インストール条件
- `MenuItemList/MenuItem`: 名称は `Name/Calculation`（計算式なので `"…"` 付き）、
  `Command@name` は標準コマンド、`Step/StepText` はスクリプト等の動作、
  `@isSeparatorItem`、`Shortcut@key/@modifier`
