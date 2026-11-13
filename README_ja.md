# CADL: Contract Architecture Description Language

[English](README.md) | 日本語

CADL（Contract Architecture Description Language）は、System of Systems（SoS）における制度設計を形式的に記述・検証・配備するためのドメイン固有言語（DSL）です。エンジニアから市民まで、幅広いステークホルダがガバナンスルール・契約・プロトコル・インセンティブ構造を、計算機が処理可能かつ検証可能な形式で定義できます。

名古屋大学 大学院情報学研究科 ERTL にて開発しています。

**ドキュメント**

- [仕様書・言語リファレンス](https://www.ertl.jp/cadl-spec/ja/)（[English](https://www.ertl.jp/cadl-spec/)） — 言語は[第5章](https://www.ertl.jp/cadl-spec/ja/docs/spec/language-spec)、構文の全体は[付録A](https://www.ertl.jp/cadl-spec/ja/docs/spec/appendix-a-syntax)から読むのがおすすめです
- [ハンズオン講座](https://www.ertl.jp/cadl-spec/ja/docs/handson/) — ツールチェーンを順に体験する教材
- このREADME — インストール、コマンド、同梱サンプル
- [CHANGELOG](CHANGELOG.md) · [コントリビュート](CONTRIBUTING.md) · [セキュリティポリシー](SECURITY.md) · [Issues](https://github.com/ertlnagoya/cadl/issues)

## 動機

SoS環境では、独立に運用される複数のシステムが共通のルールのもとで協調する必要があります。現状、これらのルールは自然言語の契約書や暗黙の合意に依存しており、曖昧性・矛盾・設計意図と実装の乖離が避けられません。

CADLは以下の3つの目的を実現します：

1. **制度を計算可能にする** — 権限構造・情報共有方針・インセンティブ機構などの現実世界のルールを、コピー・合成・バージョン管理・検索が可能なデータオブジェクトとしてモデル化する。
2. **矛盾と違反を検出する** — SMTソルバやモデル検査器で無矛盾性を自動検証し、制度制約を実行時に監視する。
3. **実行可能な成果物を生成する** — 検証済みの制度設計から、制御ロジック・監視コード・スマートコントラクトをターゲットプラットフォーム向けに自動生成する。

## 言語の概要

CADLファイル（`.cadl`）はYAMLライクな宣言的構文を使用します。各ファイルは1つのSoS定義を記述し、以下のセクションで構成されます：

| セクション | 役割 |
|---|---|
| `actors` | 構成システム（アクター）の役割・自律性レベル・能力の定義 |
| `contracts` | 権限(beta)・情報共有(alpha)・インセンティブ(lambda)パラメータを含むassume-guarantee契約 |
| `protocols` | メッセージ送受信・タイミング制約・フォールバックを含む協調手順 |
| `algorithms` | 中央/局所アルゴリズムの参照 |
| `transitions` | 安全不変条件を伴う、運用モード（regime）間の遷移 |
| `metrics` | 計算式と目標値を持つ評価指標 |
| `verification` | 検査する性質と、使用する手法（実装済みは `smt` のみ） |

契約には `lifecycle:` と `monitors:` のブロック（SoS-DSL拡張）も書けます。契約インスタンスがたどる状態と、実行中に監視する条件を記述するものです。キーの一覧は、仕様書の[付録A](https://www.ertl.jp/cadl-spec/ja/docs/spec/appendix-a-syntax)と[付録E](https://www.ertl.jp/cadl-spec/ja/docs/spec/appendix-e-sos-dsl)にあります。

### 制度パラメータ

CADLは制度の特性を[0, 1]の連続パラメータで定量化します：

- **alpha (情報共有度)** — 0: 各アクターはローカル情報のみ、1: 全情報を完全共有
- **beta (意思決定集中度)** — 0: 完全分散（各アクターが自律的に決定）、1: 完全集中（単一アクターが全決定）
- **lambda (インセンティブ強度)** — 0: 指令ベース、1: 市場メカニズム

### 記述例

```yaml
sos:
  name: "RobotDeliverySystem"
  type: Acknowledged
  version: "1.0.0"

  actors:
    - id: DISPATCHER
      role: "global_planner"
      autonomy: low
    - id: "ROBOT[1..N]"
      role: "delivery_vehicle"
      autonomy: high

  contracts:
    - id: DELIVERY_SLA
      parties:
        - DISPATCHER
        - "ROBOT[*]"
      assume:
        - "DISPATCHER.is_operational == true"
        - "network_latency <= 200ms"
      guarantee:
        - "all_routes_conflict_free()"
        - "delivery_time <= promised_time * 1.2"
      authority:
        decision_holder: DISPATCHER
        beta: 0.8
      information:
        alpha: 0.8
      incentives:
        type: reputation
        lambda: 0.5
        rules:
          - "reward(ROBOT[i], 10) when on_time_delivery"
          - "penalty(ROBOT[i], -5) when route_deviation"
      duration: indefinite

  protocols:
    - id: FAILURE_REPLAN
      trigger: "road_failure_detected_by(ROBOT[i])"
      steps:
        - "ROBOT[i] -> DISPATCHER : failure_report"
        - "DISPATCHER : recompute_routes"
        - "DISPATCHER -> ROBOT[*] : new_route"
      timing:
        max_total: 500ms
      postcondition: "all_routes_conflict_free()"

  transitions:
    - from: IDLE
      to: ACTIVE
      condition: "delivery_request_pending == true"
      safety_invariant: "all_routes_conflict_free()"
    - from: ACTIVE
      to: IDLE
      condition: "all_deliveries_complete == true"
      safety_invariant: "all_robots_at_base == true"
```

## インストール

Python 3.9以上が必要です。

```bash
pip install cadl-lang

# AI機能を使う場合（Anthropic APIキーが必要）
pip install "cadl-lang[ai]"
```

PyPI上の配布名は `cadl-lang` です。importするパッケージ名とコマンド名はどちらも `cadl` です。

CADL自体を開発する場合は、ソースからインストールします。

```bash
git clone https://github.com/ertlnagoya/cadl
cd cadl
pip install -e ".[dev]"
pytest
```

## 使い方

```bash
# CADLファイルをパースして概要を表示
cadl parse examples/robot_delivery.cadl

# パース＋型検査を実行
cadl check examples/robot_delivery.cadl

# 全検証（型検査 + SMT検証 + デッドロック検出）
cadl verify examples/robot_delivery.cadl

# Pythonランタイムコードを生成
cadl codegen examples/robot_delivery.cadl -o /tmp/robot_delivery

# Solidityスマートコントラクトを生成
cadl codegen examples/robot_delivery.cadl --target solidity -o /tmp/solidity_out

# OPA/Regoポリシーを生成
cadl codegen examples/robot_delivery.cadl --target opa -o /tmp/rego_out

# SoS-DSL契約（lifecycle / monitors）からUnity C#を生成
cadl codegen examples/sos_dsl_robot_delivery.cadl --target unity-csharp -o /tmp/unity_out

# エージェントガバナンス拡張（agents 0.1）：境界で1通のメッセージを判定し、
# 記録済みのメッセージ列を再生し（保留、委任、監査記録）、監査ログのハッシュ連鎖を検証し、
# 境界ポリシーをRegoまたはCedarとして生成（MCPゲートウェイは後述のエージェントガバナンスの節）
cadl agents-decide examples/agents_tool_governance.cadl --input examples/agents_inputs/delete_file.json
cadl agents-run examples/agents_tool_governance.cadl --trace examples/traces/agents_tool_governance_a_approved.jsonl --audit /tmp/audit.jsonl
cadl audit-verify /tmp/audit.jsonl --source examples/agents_tool_governance.cadl
cadl codegen examples/agents_tool_governance.cadl --target agent-rego -o /tmp/agent_rego
cadl codegen examples/agents_tool_governance.cadl --target agent-cedar -o /tmp/agent_cedar

# 運用モードの遷移を分析
cadl regime-map examples/smart_city_traffic.cadl
cadl regime-map examples/smart_city_traffic.cadl --format dot -o regime.dot

# IEC 62853を参考にしたディペンダビリティ要約を生成
cadl iec62853 examples/robot_delivery.cadl
cadl iec62853 examples/smart_city_traffic.cadl --format json

# 記録済みトレースに対する規範拡張の判定
cadl norms-eval examples/norms_robot_delivery.cadl --trace examples/traces/norms_robot_delivery_a_fulfilled.jsonl

# 同じ判定を、生成したPythonパッケージのモニタで得る（cadlなしで動く）
cadl codegen examples/norms_robot_delivery.cadl -o /tmp/gen/robot_delivery_norms
(cd /tmp/gen && python -m robot_delivery_norms.norms_monitor "$OLDPWD/examples/traces/norms_robot_delivery_a_fulfilled.jsonl" --format text)

# ある版から次の版への改正が規範に及ぼす効果
cadl norms-diff examples/norms_amendment_v1.cadl examples/norms_amendment_v2.cadl

# 1つの契約インスタンスの規範を、すべての振る舞いについて設計時に検査（PATH上にPRISMが必要）：
# 違反されうる規範、衝突、行使されない権能を、証拠のトレースとともに報告
cadl norms-check examples/norms_check_conflict.cadl --bindings i=1 --export /tmp/norms-check

# 根拠付け拡張：契約ごとの情報共有の導出指標、権限集中の導出指標、インセンティブ被覆率
cadl norms-indices examples/grounding_robot_delivery.cadl
cadl norms-indices examples/grounding_robot_delivery.cadl --format json
# 確率モデル拡張：model: ブロックをPRISMへ出力（設計ごとにモデルファイルと性質ファイル）
cadl model-export examples/stochastic_robot_delivery.cadl -o prism_out/

# 内部の参照評価器で性質を評価（厳密な有理数演算）
cadl model-check examples/stochastic_robot_delivery.cadl
cadl model-check examples/stochastic_robot_delivery.cadl --regime-map best_for_delivery --grid 5

# 保証付きの運用モードマップ：パラメータ空間のどのセルでどの運用モードが最良か
cadl model-check examples/stochastic_robot_delivery.cadl --regime-map best_for_delivery --certify

# 戦略モデル拡張（games 0.1）：遵守プロファイルはナッシュ均衡か。パラメータ領域のどのセルで均衡か
cadl game-check examples/games_robot_delivery.cadl
cadl game-check examples/games_robot_delivery.cadl --certify --equilibrium comply_by_lambda

# 自然言語からCADLを生成（ANTHROPIC_API_KEYが必要）
cadl ai "5台の自律ドローンが農地を測量するシステム。地上局が統括する。"
cadl ai -f requirements.txt -o output.cadl
```

## アーキテクチャ

CADL処理系は、一般的なコンパイラと同じパイプライン構成です：

```
.cadlファイル
    |
    v
+-------------------+
|  パーサ            |  YAML構造解析 + Lark式文法
|  (parser.py)      |  -> 抽象構文木（AST）
+---------+---------+
          |
          v
+-------------------+
|  型検査器          |  アクター参照解決、パラメータ値域検証、
|  (type_checker)   |  契約当事者の整合性検査
+---------+---------+
          |
          v
    [型検査済みAST]
          |
    +-----+-----+-----------+-----------+
    v           v           v           v
  SMT検証    コード生成   モード      IEC 62853
  (Z3)      (codegen/)   マップ     要約
              |
        +-----+-----+--------+
        v     v     v        v
     Python Solidity OPA/Rego Unity C#
```

### コード生成

`cadl codegen` はCADL定義から実行可能なコードを生成します。型検査は行いますが検証器は実行しないので、先に `cadl verify` を実行してください。4つのターゲットに対応：

**Python** (`--target python`, デフォルト):

| 生成モジュール | 内容 |
|---|---|
| `actors.py` | 役割・自律性・能力スタブを持つアクター基底クラス |
| `contracts.py` | assume/guarantee検査を行う契約監視クラス |
| `protocols.py` | タイムアウト処理付き非同期プロトコル状態機械 |
| `transitions.py` | 遷移条件を持つ運用モードコントローラ |
| `metrics.py` | 計算式による評価指標コレクタ |
| `runtime.py` | 全コンポーネントを結合するオーケストレータ |
| `norms_monitor.py` | `norms: 0.1` を宣言したファイルの場合のみ：契約の規範、counts-as規則、時相演算子を含む項目をオンラインで監視するモニタ（[後述](#生成したpythonコードの規範モニタ)） |

生成コードは `cadl.codegen.runtime_support` の基底クラスを継承します。`norms_monitor.py` はcadlをインポートしません。

**Solidity** (`--target solidity`):

Ethereumスマートコントラクト（`.sol`）を生成：
- 各CADL `ContractDef` に対応するコントラクト（`checkAssumptions()`, `checkGuarantees()`, `runMonitorCycle()`）
- `RegimeController.sol` — enum型で運用モードを表す状態機械
- メインオーケストレータコントラクト
- 制度パラメータ（alpha, beta, lambda）をスケーリングされた `uint256` 定数として定義
- `norms: 0.1` を宣言したファイルでは、契約ごとに規範のモニタ `<Contract>NormsMonitor.sol`（[後述](#生成したregoとsolidityの規範モニタ)）

**OPA/Rego** (`--target opa`):

Open Policy Agentポリシー（`.rego`）を生成：
- 各CADL `ContractDef` に対応するRegoパッケージ（`assumptions_hold`, `guarantees_hold`, `allow`, `violation` ルール）
- 全契約を集約するメインポリシーパッケージ
- 情報共有・権限ポリシールール
- `norms: 0.1` を宣言したファイルでは、契約ごとに規範のモニタのステップ関数 `norms_monitor_<contract>.rego`（[後述](#生成したregoとsolidityの規範モニタ)）

**Unity C#** (`--target unity-csharp`):

SoS-DSL拡張（`lifecycle:` / `monitors:`）を使う契約からC#を生成：
- 契約ごとの `Generated/<Contract>State.cs`、`<Contract>Contract.cs`、`<Contract>Monitors.cs`
- `Runtime/` のサポートクラス（契約ランタイム、イベントバス、述語評価器）

[テキストブックの第 7 章](https://www.ertl.jp/cadl-spec/ja/docs/textbook/codegen-runtime/)で、このターゲットを一通り実行できます。

**エージェント境界ポリシー** (`--target agent-rego`, `--target agent-cedar`):

`agents: 0.1` を宣言したファイルの執行対象の契約から、エージェントガバナンス拡張（後述）の境界ポリシーを生成します。宣言がないファイルでは `cadl codegen` がエラーを報告します。どちらも `opa` ターゲットとは独立です。
- `agent-rego` は `agent_policy.rego` を出力します。一つのRegoパッケージ（`cadl.agent_policy`）が、ポリシー入力を `input` として受け取り、結果オブジェクト全体を `result`（および `decision`）として返します。静的な規範もそうでない規範も扱い、静的でない規範の発効フラグと派生インスタンスは `input.instances` から読みます。ファイルの先頭に、静的でない規範の一覧を出力します
- `agent-cedar` は `agent_policy.cedar` と `agent_policy_table.json` を出力します。ポリシーは静的に判定できる部分を扱います。未宣言のツールと静的な禁止は `forbid`、静的な許可は `permit` になり、許可のないツール呼び出しはCedarの既定の拒否に落ちます。Cedarの結果は2値なので、エスカレーションは拒否として返り、ランタイムが表（禁止ごとの `escalate_to`・`timeout`・`fallback`・`notify`）から解決します。ファイル先頭のREADMEコメントに、扱わないもの（静的でない規範、派生した許可、契約インスタンス）と、Cedarの答えが方針の入力に対する決定と一致する場合（G.10）を明記します。どのポリシーも属性を読まないので、評価エラーでスキップされることはありません

### エージェントガバナンス（agents 0.1）

エージェントガバナンス拡張（仕様の付録G）は規範拡張の上に成り立ちます。ファイルは `norms: 0.1` と `agents: 0.1` の両方を宣言します。そのうえで、`agent:` がアクターに種別（`llm_agent`・`tool_server`・`human`・`service`）とプロトコル、ツールサーバの場合は `tools:` を与え、`enforcement:` が契約を執行対象にします。禁止に付ける `on_denied:` は承認者（`escalate_to:`）、`timeout:`、代替メッセージ（`fallback:`）、通知先（`notify:`）を指定し、`delegations:` は許可を委任可能にします。`examples/agents_tool_governance.cadl` は付録の例です。

`cadl agents-decide <file.cadl> --input <input.json> [--format text|json]` は判定関数（G.5）の参照実装です。1通のメッセージに対して `allow`・`deny`・`escalate` のいずれかを、理由、決定に関わった契約、規範、派生した許可の来歴チェーン、エスカレーションのデータとともに返します。

```bash
$ cadl agents-decide examples/agents_tool_governance.cadl --input examples/agents_inputs/delete_file.json
decision:    escalate
reason:      prohibition
contracts:   ASSISTED_REPORTING
norms:       ASSISTED_REPORTING.no_delete
provenance:  -
escalate_to: OPERATOR
timeout:     600000 ms
fallback:    PLANNER -> TICKETS : create_ticket
notify:      OPERATOR
```

入力はJSONオブジェクトで、`message`（`from`・`to`・`name`）と、省略可能な `now`、`instances`（動作中の契約インスタンスごとの `bindings`、静的でない規範の発効フラグ `in_force`、有効な派生インスタンス `derived` とそのチェーン）を持ちます。`examples/agents_inputs/` に付録のサンプル呼び出しがあります。終了ステータスは `allow` が0、`deny` が1、`escalate` が3、使い方の誤りが2です。執行対象の契約の当事者であるツールサーバ宛てのメッセージは、送信者が誰であっても統治されます。当事者でないアクターや未定義のアクターからのツール呼び出しは、ツールの既定によって拒否されます。照合は3値で、不明はフェイルクローズに解決します。不明な禁止は適用され、不明な許可は適用されません。

このコマンドは状態を持ちません。発効フラグと有効な派生インスタンスは入力として渡します。その状態を保持し、記録済みトレースの各メッセージに同じ判定関数を適用するのが、次の `cadl agents-run` です。

#### 記録済みトレースに対するランタイム

`cadl agents-run` は付録（G.5〜G.8）のランタイムの参照実装です。記録済みのメッセージ列を、ファイル中のすべての執行対象の契約にわたる一つの論理的な判定点に通して再生し、各メッセージの判定、保留とその終わり方、委任の状態、監査記録を計算します。決定的（トレースが実行IDを与えないときの監査ヘッダの乱数の実行IDを除く）でオフラインです。エージェントもツールも実行せず、時計はトレースの `t` だけです。

```bash
cadl agents-run examples/agents_tool_governance.cadl \
    --trace examples/traces/agents_tool_governance_a_approved.jsonl
cadl agents-run FILE --trace TRACE [--format text|json] [--audit OUT.jsonl [--audit-key FILE]]
```

ランタイムは、トレースの順に、メッセージごとに次のことを行います。(a) 状態を導きます。静的でない規範の発効フラグは `cadl norms-eval` の判定エンジンから、有効な派生インスタンスはそれまでに見た付与と取り消しから、動作中の契約インスタンスとその束縛はトレースから得ます。(b) その状態から組み立てたポリシー入力に `cadl agents-decide` の判定関数を適用します。(c) 判定を適用します。許可したメッセージは、それを統治する契約のインスタンスにとって規範エンジンの観測になり、拒否したメッセージは観測になりません。`escalate` は保留を作ります（作った順に `h-1`、`h-2`、…）。(d) 監査記録を追記します。保留が終わるのは、承認者のメッセージが `counts_as:` 規則を通じて `approval` か `rejection` を作ったとき（保留のidは最初の引数。`approve("h-1")`）、タイムアウトが過ぎたとき、根拠の契約インスタンスが終了したか、その契約が改正されたときです。承認されたメッセージは、承認の時刻にもう一度判定し、拒否するものがなければ次の観測として解放します。解放したメッセージは、保留の根拠になった禁止に違反しません。この2回目の判定で、承認が許可と根拠の代わりになるのは、根拠の契約インスタンスの中だけです。それ以外で適用される禁止、終了した根拠の契約インスタンス、メッセージをもう許可しなくなった別の統治契約（委任の取り消し、インスタンスの終了、改正）は、解放を拒否します。ファイルは `norms: 0.1` と `agents: 0.1` を宣言し、`cadl check` に通る必要があります。終了ステータスは、判定の内容にかかわらずトレースを再生できれば0、使い方の誤りは2です。

**トレース。** `cadl norms-eval` のJSON Lines形式（`t`、`bindings`、`request`、`values`、`messages`、`state`、`amend`、`end`。[トレースに対する規範の判定](#トレースに対する規範の判定)を参照）を、一つの契約インスタンスではなく判定点の全体について読み、キーを二つ追加します。

```json
{"t": 0, "quantities": [], "epoch": "2026-11-10T09:00:00Z", "messages": [{"from": "OPERATOR", "to": "PLANNER", "name": "grant_read"}]}
{"t": 180, "messages": [{"from": "PLANNER", "to": "FILES", "name": "delete_file"}]}
{"t": 220, "messages": [{"from": "OPERATOR", "to": "PLANNER", "name": "approve", "args": ["h-1"]}]}
```

| キー | 意味 |
|---|---|
| `epoch` | 先頭行のみ：`t = 0` の時刻（ISO 8601のタイムスタンプ）。指定すると、監査記録の時刻とポリシー入力の `now` はタイムスタンプになります。指定しなければ `t` の数値です |
| `quantities` | 先頭行のみ：実行の量の名前のリスト（`["limit", "target"]`）。指定すると、ちょうどこれらの裸の名前が最初から量になります。値を持つ前にそれを読む述語は未定義です。指定しなければ、裸の名前は、値を与える最初の行から量になり、それより前は記号的な値です。これにより、どの判定も後の行に依存しません。そのような名前が、メッセージを判定した後で最初の値を得たとき、警告を出します。このキーを指定する書き方を推奨します。同梱の例のように、述語に裸の名前がない契約では `[]` を指定します。トレースは、先頭行で `values` の初期値も与えるべきです。禁止の `trigger:` や許可の `until:` が読む量に値がない間、メッセージは拒否またはエスカレーションされます（「読み方」を参照） |
| `host_instances` | 先頭行のみ：ホストがインスタンスを作る契約のidのリスト。最初のインスタンスが後の行で作られる場合に使います（`["DELIVERY"]`）。契約がホストの作るインスタンスを持つかどうかは、先頭行で決まります |
| `instances` | ホストが作る契約インスタンス。各項目は `contract` と `instance` を持ち、`bindings`、`request`、`state`、`"end": true` を持てます。新しいインスタンスを名指す項目は、その行でインスタンスを作ります。後の項目は、そのライフサイクルの `state` を設定するか、インスタンスを終了させます。先頭行が、ここで `default` 以外のインスタンスを名指さず、`host_instances` にも挙げない契約は、先頭行から最後まで、先頭行の `bindings` と `request` を持つ唯一のインスタンス `default` を持ちます。そのような契約のインスタンスを作る後の項目は、トレースの誤りです。どのインスタンスも、その契約の当事者の添字変数を束縛する必要があります（当事者 `ROBOT[i]` に対して `"bindings": {"i": 2}`）。そうでなければトレースを拒否します |
| `t` | 絶対値が2**53以下の有限の数。エポック秒もエポックミリ秒も収まります。`NaN`、`Infinity`、それより大きい値はトレースの誤りです。`cadl norms-eval` でも同じです。ファイルの期間（`timeout:`、`deadline:`）は秒の単位で数えます。`epoch` を指定したときは、トレースのすべての時刻と保留のすべての期限が、ISO 8601のタイムスタンプで表せる時点でなければなりません。そうでなければ、行番号を示してトレースを拒否します |
| `messages` | 書いた順に一つずつ判定します。`from` と `to` は具体的なアクター（`PLANNER`、`ROBOT[2]`）、`name` は識別子で、余分な空白なしで書きます（`"PLAN NER"` はトレースの誤りです） |
| `state` | `lifecycle:` を持つ契約の動作中のすべてのインスタンスに適用します。`instances` の項目は一つのインスタンスに設定します。終端状態はインスタンスを終了させます |
| `amend` | ファイルの新しい版。定義が変わった執行対象の契約を `cadl norms-eval` と同じように改正し、その契約の保留を取り消し、派生インスタンスを新しい版に照らして調べます（G.7）。執行対象の契約の集合は変えられません |
| `end` | `true` は動作中のすべてのインスタンスを終了させます。最後の行も、`"end": false` と書かないかぎり同じです（その場合、保留中の保留は `held` のまま残ります）。その後の行のメッセージも判定し、記録します。終了したインスタンスは戻らないので、その契約のツール呼び出しは `no_instance` で拒否されます |
| `run_id` | 先頭行のみ。監査ログの実行ID（小文字の16進数32桁）。ないときは `--audit` が乱数で決めます |
| `approvals` | 先頭行のみ。ホストが承認をどう読んだか（`authenticated`、`insecure`、`none`）。監査ヘッダに写します |
| `withdraw` | この行でホストが保留中のメッセージを取り下げる保留のID（クライアントが呼び出しを取り消した）。まだ `held` の保留は `withdrawn` で閉じ（閉じる記録は `deny`、理由 `withdrawn`）、その後の承認は `not_held` になります |
| `host_events` | この行でのホストの事象の記録。行を処理する前に書きます。項目は `event`（`method_refused`、`unsolicited_response`、`unauthorised_approval`、`unauthorised_rejection`、`unauthorised_message`、`approval_evidence`、`approval_unauthenticated`、`forward_failed`、0.3.25からは `unauthenticated_request`、`task_refused`、`invalid_response`、`task_cancel`、`overloaded`、`read_timeout`、0.3.28からは `handshake_failed`、`push_config`、`push_refused`、`push_sent`、`push_failed`、`push_dropped`）と、省略可能な `reason`、`hold_id`、`message` です。判定は変えません。MCPゲートウェイとA2Aゲートウェイが書きます |

一つの行は次の順に処理します。期限が `t` より前の保留を期限切れにします。`withdraw` の保留を閉じます。`host_events` を記録します。インスタンスを作ります。行の時刻と `values`（および `amend`）を、メッセージを持たない一つの観測として処理します。これにより、過ぎた期限や期間を、メッセージを判定する前に確定させます。各メッセージを判定し、許可したメッセージを一つの観測にします。`state` の変更と `end` は、メッセージを持たない最後の観測になります。

**出力。** テキストの報告は、監査ログ、保留、派生インスタンス、各契約インスタンスの規範の最終状態を示します。`--format json` は一つのオブジェクトを出力します。

| キー | 内容 |
|---|---|
| `file`、`trace`、`agents_version`、`contracts` | 再生した対象と、執行対象の契約 |
| `summary` | `messages`、`allow`、`deny`、`escalate`（メッセージの判定の件数）、`holds`（最終状態と `held` ごとの件数）、`released`、`audit_records` |
| `audit[]` | 監査ログ（後述） |
| `steps[]` | トレースのメッセージごと：`time`、`message`（`args` を含む）、`input`（ランタイムが組み立てたポリシー入力。`agents-decide` の入力と同じ形式）、`result`、`hold_id` |
| `releases[]` | 承認された保留ごと：`time`、`hold_id`、`message`、`input`（承認の時点のポリシー入力）、`grounds`（`contract`、`norm`、`instance`）、`released` |
| `holds[]` | `hold_id`、`state`（`held`、`approved`、`rejected`、`expired`、`cancelled`）、`message`、`time`、`approver`、`deadline`、`contracts`、`norms`、`closed`、`released`（メッセージが効果を生じたか） |
| `delegations[]` | すべての派生インスタンス：`contract`、`instance`、`norm`、`holder`、`chain`、`state`（`active`、`discharged`）、`valid`、`granted`、`ended`、`end_reason` |
| `instances[]` | 契約インスタンスごと：`contract`、`instance`、`bindings`、`running`、`start`、`end`、`state` と、規範エンジンによる `norms[]`（`norms-eval` と同じ項目に `in_force` を追加）、`violations[]`、`institutional_events[]`、`unauthorised[]`、`ok` |
| `amendments[]`、`warnings[]` | 適用した改正（`time`、`file`、`contracts`）と警告 |

**監査ログ。** `--audit OUT.jsonl` は、判定点が書いた順に、記録をJSON Linesとして出力します。同じファイルとトレースからは、常に同じバイト列が得られます。*判定の記録*はG.8の監査記録です。判定の結果（`decision`、`reason`、`contracts`、`norms`、`provenance`、`escalate_to`、`timeout`、`fallback`、`notify`）に、`time`、`message`（`from`、`to`、`name`）、`instances`（統治する契約の動作中のインスタンス。閉じる記録では、動作中かどうかにかかわらず、保留の根拠の契約インスタンス）、`hold_id`、`outcome` を加えたものです。`escalate` は同じ `hold_id` を持つ二つの記録になります。判定の記録と、保留を閉じる記録です。閉じる記録の `outcome` は保留の最終状態で、理由は `approved`、`rejected`、`expired`（`time` は期限）、`cancelled` のいずれか、または2回目の判定が拒否したときの拒否の理由です。*事象の記録*は `decision` の代わりに `event` を持ち、フィールドは `time`、`event`、`message`、`instances`、`norm`、`chain`、`holders`、`hold_id`、`reason` です。

| `event` | 書くとき | `reason` |
|---|---|---|
| `grant` | 付与のメッセージを観測し、G.7の条件1〜4が成り立つとき。`chain` と `holders` は新しい派生インスタンスを示します | `null`。条件5により何もしない場合は `holder_exists` |
| `revoke` | 取り消しのメッセージを観測したとき。`holders` は解除された保持者です | `null`、または `no_effect` |
| `unauthorised_grant` | 付与のメッセージが条件1〜4のいずれかを満たさないとき | 最初に満たさなかった条件：`no_authority`、`not_in_to`、`in_chain`、`max_depth` |
| `delegation_end` | 派生インスタンスが、取り消しのメッセージ以外の原因で終わったとき。インスタンスごとに一つの記録で、その `chain` と保持者を示します | `until`、`original_discharged`、`instance_end`、`amendment` |
| `instance_start`、`instance_end` | 契約インスタンスが開始または終了したとき（`message` は `null`） | `null`。終了では `end` または `terminal_state` |
| `amendment` | 新しい版が発効したとき。`instances` は、改正された契約の動作中のインスタンスです | トレースに書かれた新しい版の名前 |
| `unauthorised_approval`、`unauthorised_rejection` | `approval` または `rejection` がどの保留も変えなかったとき | `unknown_hold`、`not_held`、`not_approver`、`other_instance`（根拠の契約インスタンスで作られていない） |
| `unauthorised_act` | 規範エンジンが権限のない行為（F.5の7）を記録したとき | `no_power` |

```json
{"time": "2026-11-10T09:03:00Z", "message": {"from": "PLANNER", "to": "FILES", "name": "delete_file"}, "instances": [{"contract": "ASSISTED_REPORTING", "instance": "default"}], "decision": "escalate", "reason": "prohibition", "contracts": ["ASSISTED_REPORTING"], "norms": ["ASSISTED_REPORTING.no_delete"], "provenance": [], "escalate_to": "OPERATOR", "timeout": 600000, "fallback": ["PLANNER -> TICKETS : create_ticket"], "notify": ["OPERATOR"], "hold_id": "h-1", "outcome": null}
{"time": "2026-11-10T09:03:40Z", "message": {"from": "PLANNER", "to": "FILES", "name": "delete_file"}, "instances": [{"contract": "ASSISTED_REPORTING", "instance": "default"}], "decision": "allow", "reason": "approved", "contracts": ["ASSISTED_REPORTING"], "norms": ["ASSISTED_REPORTING.no_delete"], "provenance": [], "escalate_to": "OPERATOR", "timeout": null, "fallback": [], "notify": [], "hold_id": "h-1", "outcome": "approved"}
```

監査ファイルは、トレースの全体を再生した後にだけ書きます。実行が失敗したときはファイルを書かず、エラーメッセージにその旨を示します。規範の発効フラグの変化には、それ自体の記録はありません。記録されたメッセージ、時刻、トレースの `values` から導けます。

**ログの完全性。** 監査ファイルはハッシュ連鎖です（G.8「ログの完全性」）。1行目はヘッダ記録 `{"audit_format": "cadl-audit/1", "hash": "sha256", "key_id": null, "run_id": ..., "approvals": ..., "source_sha256": ..., "epoch": ...}` です。実行ID（16進数32桁。トレースの先頭行の `run_id`、なければ乱数）、ホストが承認をどう読んだか（`authenticated`、`insecure`、`none`。トレースが示さなければ `null`）、CADLファイルのバイト列のSHA-256、トレースの `epoch` または `null` を持ちます。その後に上の記録が、フィールドと値を変えずに続き、完結したログは `{"audit_end": true, "records": N}` で終わります。各行は自身のフィールドの後に三つのフィールドを持ちます。`seq`（ヘッダが0）、`prev`（直前の行の `digest`。ヘッダでは0が64個）、`digest` です。`digest` は、`digest` を除いた行の正規形JSONのSHA-256です。正規形は次のとおりです。キーを符号位置の順に並べ、区切りを空白なしの `,` と `:` とします。文字列はUTF-8で書き、JSONが求めるエスケープ（`\"`、`\\`、`\b`、`\f`、`\n`、`\r`、`\t`、U+0020未満のその他の文字の `\u00xx`）だけを使います。整数値の数は整数（絶対値は2**53以下）とし、それ以外の数は、同じ倍精度の値に読み戻せる最短の10進数を指数なしで書きます（`0.0000001`）。NaN、単独のサロゲート、重複したキーは使いません。行は、フィールドをその順序のまま区切りを `, ` と `: ` として同じ規則で書き、検証ではこの形であることも求めるので、空白、エスケープ、数の書き方の変更も検出します。`--audit-key FILE` を与えると、ダイジェストはFILEの鍵によるHMAC-SHA256になります（16バイト以上。末尾の改行一つは無視。コマンド行では与えない）。ヘッダは `hmac-sha256` となり、`key_id` はHMAC-SHA256(鍵, `cadl-audit key id`) の先頭16桁の16進数です。

保護の範囲。鍵なしでは、1バイトの変更、行の削除・挿入・入れ替え、末尾の切り詰め（終了記録の欠落）を検出します。他の実行のログの行は合いません。ヘッダの実行IDが、それより後のすべてのダイジェストを変えるからです。鍵なしで検出しないのは、変更した行から後を終了記録も含めて書き直したログです。SHA-256は誰でも計算し直せるためです。最後のダイジェストを別の場所に保管して `audit-verify --anchor` に与えるか、鍵を使ってください。鍵ありでは、鍵の保持者だけがログを書き直せます。ヘッダの `hash` フィールドは認証されません。鍵つきのログを鍵なしのログとして書き直すと `--key` なしで検証を通るので、検証者はログが鍵つきかどうかをログの外から知り、鍵を与える必要があります。`cadl.audit_log.read_records(path)` は、ヘッダ、終了記録、完全性のフィールドを除いた記録を返します。`agents-run` は、どのトレースの `withdraw` と `host_events` も受け付けます。したがって、鍵なしのトレースから再生したログの中のホストの事象記録は、それだけでは何も証明しません。また、`run_id` のないトレースの再生は新しい実行IDを引くので、そのログはバイト単位では再現しません。再現するには、トレースの先頭行に `run_id` を与えてください。

```bash
cadl audit-verify AUDIT.jsonl [--key FILE] [--source FILE.cadl] [--run-id HEX] [--anchor DIGEST] [--open] [--format text|json]
```

`cadl audit-verify` はすべての行を検査し、検証できない最初の行を、期待した値と実際の値とともに報告します（`--format json` では `valid`、`line`・`what`・`expected`・`found` を持つ `error`、および `lines`、`records`、`hash`、`key_id`、`run_id`、`source_sha256`、`epoch`、`complete`、`last_digest`）。`--source` はそのCADLファイルについてのログであること、`--run-id` はその実行のログであること、`--anchor` は最後のダイジェストが別に保管した値であることも求めます。終了記録のないログは、`--open`（実行中のゲートウェイのログ）を与えない限り無効です。鍵つきのログを `--key` なしで検証するのは使い方の誤りです。鍵なしのログに `--key` を与えると無効になります。終了ステータスは、有効なら0、無効なら1、使い方の誤りは2です。

**オンラインでの利用。** 同じエンジンを1行ずつ動かせます。`rt = AgentsRuntime.open(FILE)` の後、トレースの各行を辞書として `rt.observe(line)` に渡し（その行の `steps`、`audit` の記録、変化した `holds` を返す）、最後に `rt.close(t)` を呼びます。これは最終行 `{"t": t}` にあたります。`agents-run` は同じ処理の繰り返しであり、トレースを `observe` に渡してから `close` を呼んだ結果は、そのトレースの後に `{"t": t}` を加えたものに対する `agents-run` の結果と一致します。誤りが起きた後は、オンラインのランタイムはそれ以降の行をすべて拒否します。

**読み方。** G.5〜G.8が選択の余地を残す箇所は、次のように読みます。

- メッセージは、それを統治する執行対象の契約のインスタンスにとってだけ観測になります（G.5）。どの執行対象の契約も統治しないメッセージは `ungoverned` として許可され、どの契約の観測にもなりません。
- 拒否または保留されたメッセージは観測になりません。規範エンジンはそのメッセージを見ないので、規範を発動させず、制度的事象を作らず、義務を履行させず、禁止に違反せず、付与・取り消し・承認・却下にもなりません。時間は進みます。どの行も、まずメッセージ抜きで観測します。
- 行の時刻は、その行のメッセージを判定する前に観測します。したがって、その時刻のメッセージを判定するとき、期間が閉じた禁止はもう適用されず、期限が過ぎた義務はすでに `violated` です。
- どの契約インスタンスも、その契約の当事者の添字変数を束縛します。インスタンス `default` は先頭行の `bindings` で、ホストが作るインスタンスはその項目の `bindings` で束縛します。束縛のないインスタンスと、当事者が名指す集合の要素の添字でない値を束縛するインスタンスは、トレースの誤りであり、契約、インスタンス、変数、値を示して拒否します。どの当事者も持たない変数の束縛は、警告を出して無視します。G-18とあわせて、`cadl agents-run` では、執行対象の契約の添字が束縛されないままになることはありません。
- どの判定も、その時点までの行だけに依存します。トレースの接頭部分に対する判定は、トレースの全体に対する最初の判定と同じです。述語の中の裸の名前（`AG.mode == locked`）の読み方については、ランタイムは `cadl norms-eval` のように先を見ません。`quantities` がその名前を挙げないかぎり、`locked` は、ある行が値を与えるまで記号的な値です。インスタンスの終了より後のものは、その判定を変えません。
- データの欠落は不明として扱い、固定はしません。未発効の禁止の述語 `trigger:` が、読む量に値がないために未定義である間、その禁止は適用されます。発効中の許可の述語 `until:` が未定義である間、その許可は適用されません。規範の状態は `cadl norms-eval` と同じです（未定義の述語は起こりません）。ポリシー入力の発効フラグを省くだけで、値が得られれば、フラグはただちに状態に従うものに戻ります。その `trigger:` や `until:` が、`counts_as:` ルールが述語の `when:` から作る制度的事象であり、そのルールの `when:` または `if:` が未定義である間も同じです。規範ごとに1回、その量を示して警告を出します。不明なフラグによる拒否も、理由はほかと同じ `prohibition` です。そのとき、`steps[].input` の `in_force` にそのフラグはありません。対象にならないものが二つあります。メッセージの `when:` と `if:` を持つルールは、そのメッセージの時点で事象を作るか、決して作らないかのどちらかです。`if:` に値がない間に届いたメッセージは何も作りません（そのような事象が禁止を発動させるか許可を終わらせる場合、`cadl check` が警告し（G-19）、実行時にも警告します）。また、述語の `trigger:` に値がない義務は発動されないので、その修復である禁止は生じません。許可の `until:` が未定義である間に送った付与は `unauthorised_grant` / `no_authority` になり、送り直す必要があります。
- 束縛は、契約の中でその変数が添字として用いられるすべての宣言済みの集合（当事者 `W[i]` のほかにイベントの `SUP[i]` など）の要素の添字でなければなりません。
- 限量述語と集約は、`cadl norms-eval` と同じく評価します。記号の境界を持つ集合の定義域が開いている（`N` に値がない）ために未定義であるものは、データの欠落です。禁止の `exists` のトリガはその禁止を適用させ、許可の `for all` の `until:` は、既知のメンバーがそれを偽にしている間だけ許可を適用させます。限量子の外のワイルドカード参照や範囲参照（`W[*].alarm == true`）は、`cadl norms-eval` と同じく書き換え（F.5の「ワイルドカード参照と範囲参照」）、`for all j in W[*]: W[j].alarm == true` として判定します。評価できないまま残るもの（2つの集合にわたる集約 `sum(W[*].x * R[*].y)`）を使う規範は判定されず、発効フラグは実行の全体で不明です。そのような禁止は常に適用され、そのような許可は決して適用されません。
- 後の行のトレースの誤りは、何かを表示したり書いたりする前に報告します。それより前の行は、メモリ上で判定しただけです。
- `cadl.agents_runtime.run` と `run_files` は、`cadl check` のエラーがあるファイル、またはそのようなファイルへの改正を拒否します。`run(..., check=False)` は、この検査と束縛の検査を止めます。これは安全でなく、次の項目のテストのためのものです。
- 多層防御（`check=False` でだけ到達します）：束縛されていない添字に依存する発生や述語は不明であり、不明な発生は、メッセージを通す効果を決して持たず（許可を発動も再開もさせず、禁止を終了・停止・解除させず、制度的事象を作らず、権能を行使させず、義務を履行させません）、止める効果を常に持ちます（禁止を発動させ、許可と派生インスタンスを終わらせます）。
- 保留の根拠は、契約インスタンスごとに保持します。`approval` または `rejection` が保留を閉じるのは、根拠の契約インスタンスで作られたときだけです。一つあれば足ります。同じメッセージが複数のインスタンスで事象を作っても、1回と数えます。
- 理由が `rejected`、`expired`、`cancelled` の閉じる記録は、保留の `contracts`、`norms`、`escalate_to`、`fallback`、`notify` を保ちます。`timeout` は、どの閉じる記録でも `null` です。承認されたメッセージを2回目の判定が拒否したとき、記録はその拒否の結果を持ち、`outcome` は `approved` です。
- 承認されたメッセージの2回目の判定が新しい保留を作ることはありません。エスカレーションになるはずの契約は拒否します。その記録は、拒否するすべての契約と、理由 `no_instance`、`prohibition`、`default` のうち最初のものを示します。
- 委任の `until:` は、それが起こった時点で `active` の派生インスタンスを終わらせます。その後の付与は新しいインスタンスを作ります。
- 違反した義務が `excused` になるのは、発動から期限（期限がなければ違反）までの間に、担い手による内容に一致するメッセージが、その義務の契約によって拒否されたか、その契約の根拠で保留されて解放されなかったときです。一致は境界での照合（送信者、受信者、名前）によります。
- 違反の後も、禁止の期間の時計は、停止中は止まります。違反の前と同じです。
- 改正は、二つの版で定義が異なる執行対象の契約に適用します。

`examples/traces/` には `examples/agents_tool_governance.cadl` 用のトレースが三つあります。G.12の三つの呼び出し、すなわち委任の連鎖を通じた呼び出し、承認されるエスカレーション、取り消しの後の拒否（a）、却下と代替メッセージ、終了時に取り消される保留（b）、未宣言のツール、タイムアウト、遅れた承認（c）です。`examples/agents_change_control.cadl` は、トリガから `until:` までの間だけ発効する禁止と、`max_depth: 1` と `until:` を持つ委任の例です。そのトレースは、凍結中の承認・却下・期限切れ（a）と、深さの制限と委任の終了（b）を示します。

#### MCPゲートウェイ

`cadl mcp-gateway` は、MCPクライアント（エージェント）とMCPサーバ（ファイル中のツールサーバ）の間に判定点を置きます。通信はMCPのstdioトランスポートで、JSON-RPC 2.0のメッセージを1行に一つ、内部に改行を含めずに送ります（[MCP仕様 2026-07-28、stdio](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/stdio)）。クライアントはゲートウェイをサーバとして起動し、ゲートウェイがサーバを起動します。

```bash
cadl mcp-gateway FILE --agent ACTOR --server-actor TOOLSERVER --audit OUT.jsonl \
    [--audit-key FILE] [--approvals DIR --approver-keys KEYS.json] [--trace-out TRACE.jsonl] \
    [--init FIRST.json] [--allow-method NAME ...] [--no-filter-tools] [--hold-key FILE] \
    [--poll SECONDS] -- SERVER_COMMAND [ARGS...]
cadl mcp-approve DIR HOLD_ID approve|reject --as ACTOR --key ACTOR.key [--hold-key FILE] [--require-signed-hold]
cadl mcp-message DIR 'FROM -> TO : name' [--arg VALUE ...] --key FROM.key
```

`TOOLSERVER` は、`agent: { kind: tool_server, protocol: mcp, tools: [...] }` を持つファイル中のアクタです。

**通すもの。** 両方向のすべての行を、何かをする前に厳密に構文解析します。末尾の一つのCRLF以外の復帰文字、不正なUTF-8、どの深さでも重複したキー、NaN、無限大、無限大にあふれる数（`1e999`）、100を超える配列やオブジェクトの入れ子、単独のサロゲートを含む行は拒否し（JSONでない行は -32700、それ以外は -32600）、転送しません。受け取ったままの形では転送しません。構文解析したオブジェクトを、非ASCII文字をすべてエスケープした詰めたJSONとして送るので、どの読み手も、行の終わりとみなす文字でそれを分割できません。メソッドは一覧によって両方向で通します。

| メッセージ | ゲートウェイの処理 |
|---|---|
| `tools/call` | 規範が判定する唯一のメソッドです。到着時刻のメッセージ `ACTOR -> TOOLSERVER : name` になります。`allow` なら転送し、サーバの応答を返します。`deny` なら、`isError: true` のツール結果で応答し、その本文に理由、規範、保留、`fallback`、`notify` を書きます（MCPはこれをツール実行エラーと呼び、クライアントはモデルに渡して自己修正させます）。`escalate` なら保留し、まだ応答しません。他の要求は処理を続けます。保留が閉じたとき、承認されて解放された呼び出しは転送し、却下・期限切れ・取り消しになった呼び出しと、解放が拒否された呼び出し（G.6）には拒否で応答します |
| `initialize`、`notifications/initialized`、`ping`、`server/discover`、`notifications/cancelled`、`notifications/progress`、`tools/list` | 判定せずに通します。MCP 2026-07-28（`server/discover`）とそれ以前のリビジョンのライフサイクルと発見のメソッドです。`tools/list` の結果は、ファイルが `TOOLSERVER` について宣言したツールだけに絞ります（`--no-filter-tools` ではすべて残す） |
| クライアントのその他のメソッド | `--allow-method NAME` が名指さない限り、-32601 で拒否し、事象 `method_refused` として記録します。許可したメソッドは判定せずに通します |
| サーバからの要求 | `--allow-method` が名指さない限り、サーバに -32601 を返して拒否し、`method_refused` として記録します（MCP 2026-07-28では、サーバはstdioで要求を送れません。それ以前のリビジョンでは、サンプリングと情報要求のために送れました）。サーバからの通知は、`notifications/progress`、`notifications/message`、`notifications/cancelled`、`notifications/tools/list_changed` なら通します |
| 応答 | 逆方向で処理中の要求に対するものだけを通します。それ以外の応答（保留中の呼び出し、拒否した呼び出し、すでに応答したID、未知のIDに対するもの）は捨て、`unsolicited_response` として記録します |
| 保留中の呼び出しへの `notifications/cancelled` | 呼び出しを取り下げます。トレースに `withdraw` を書き、保留は `withdrawn` で閉じ、呼び出しは送らず、その後の承認は `not_held` になります。すでに送った呼び出しについては、取消しをサーバに伝え、サーバがまだ応答できるので、そのIDは処理中のままにします |

判定は到着順に一つずつ行います。複数の呼び出しを同時に処理中にでき、保留中の呼び出しは他を止めません。失敗しうる処理は、呼び出しを判定する前にすべて行います。送る行と、`params` の正規形JSON（G.8）のSHA-256です。正規形を持たない引数（2**53を超える整数）は -32602 で拒否します。許可または解放の後にそれでも送信に失敗したときは、クライアントに -32603 を返し、ログに事象 `forward_failed` を書きます。ツールの引数は判定に使わず、トレースにもログにも書きません（G.8に引数はない）。ゲートウェイはフェイルクローズです。判定の処理が例外を出したとき、またはトレースやログに書けないときは、その呼び出しを拒否し、以後の呼び出しもすべて拒否します。保留中の呼び出しには拒否で応答し、ログには終了記録を書きません。サーバが終了したときは、処理中の要求と保留中の呼び出しにエラー -32603 で応答し、終了ステータス1で終わります。クライアントが入力を閉じたときは、サーバを止めて終了ステータス0で終わり、保留中の呼び出しには応答しません。

**承認とその他のメッセージ。** `--approvals DIR` を与えると、ゲートウェイは受け箱ディレクトリDIRのJSONファイルを名前の順に読みます。DIRは、ゲートウェイの利用者のディレクトリで、グループと他者が書き込めないものでなければなりません（ゲートウェイはモード0700で作り、そうでなければ起動しません）。読むのは、その利用者が所有する、リンクが一つの通常ファイルだけです。シンボリックリンク、ハードリンク、他の利用者のファイルは `DIR/invalid` に移します。承認は認証します。`--approver-keys KEYS.json` は、受け箱を通じて発言してよい各アクタをその鍵に対応づけます（`{"OPERATOR": "operator.key"}`。パスはKEYS.jsonからの相対パス、または `{"hex": "..."}`。16バイト以上。監査ログの鍵は使えない）。ゲートウェイは、保留中の呼び出しごとに、実行ID、保留中のメッセージ、保留の時刻、呼び出しの引数（`params`）とそのSHA-256を書いた `DIR/holds/h-N.json` を公開し、実行IDを書いた `DIR/run.json` も公開します。判定は引数に依存しません（G.4）。引数は承認者のためのものです。`cadl mcp-approve DIR h-1 approve --as OPERATOR --key operator.key` は、保留中の呼び出しとその引数を表示し（`--show` は表示だけを行う）、保留のID、決定、アクタ、実行ID、保留中のメッセージ、保留の時刻、引数のSHA-256、新しいノンスと、それ以外の部分の正規形JSON（G.8）に対するアクタの鍵でのHMAC-SHA256を持つファイルを書きます。ゲートウェイがそれを受け入れるのは、MACがそのアクタの鍵で検証でき、実行IDがその実行のもので、保留中のメッセージ、その時刻、引数のダイジェストが保留のものと一致し、ノンスが未使用のときだけです。そうでなければ、理由 `unauthenticated`、`wrong_run`、`wrong_message`、`replayed` のいずれかで `unauthorised_approval`（または `unauthorised_rejection`）を記録し、呼び出しは保留のままです。受け入れたファイルは、ファイルの `counts_as:` 規則が `approval` に対応づける承認者のメッセージ（この例では `OPERATOR -> PLANNER : approve("h-1")`）になり、その判定の前に事象の記録 `approval_evidence` を書きます。その `reason` は `file=sha256:…;params=sha256:…;key_id=…;nonce=…`（ファイル、引数、承認者の鍵のID、ノンス）です。保留が閉じるかどうかはG.6で決まります。`cadl mcp-message` は、エージェント間の付与（`OPERATOR -> PLANNER : grant_read`）などのその他のメッセージを、送信者の鍵で認証して書きます。この方法で送った承認や却下は拒否します（`unauthorised_message`、`approval_as_message`）。`--approver-keys` がなければ、ゲートウェイは `--approvals` を拒否します。`--insecure-approvals` は受け箱を認証なしで読むもので、テスト専用です。その方法で受け入れた承認は `approval_unauthenticated` として記録します。トレースの先頭行と監査ヘッダは、その実行の方式を示します（`approvals`：`authenticated`、`insecure`、`none`）。鍵が承認者どうしを区別するのは、互いの鍵ファイルを読めない場合だけです。ゲートウェイの利用者アカウントを共有する承認者は鍵の一覧の全体を読めます。したがって、各承認者は自分のアカウントを持ち（鍵はそこでとゲートウェイだけが読める）、エージェントはゲートウェイのアカウントで動かさないか、承認を別の方法で承認者を認証する通信路で届ける必要があります。受け箱が守るのは、鍵ファイルを持たない者に対してだけです。読めないファイルは、理由を書いた `.why` ファイルとともに `DIR/invalid` に移し、承認とはみなしません。

**時刻、トレース、再生。** 時刻 `t` は、ゲートウェイの開始からの単調時計の経過時間で、秒単位、ミリ秒に丸めた値です。トレースの1行目は、乱数の `run_id`、`t = 0` の実時刻としての `epoch`、`"quantities": []` を持ち、`--init FIRST.json` のキー（`bindings`、`request`、`values`、`quantities`、`instances`、`host_instances`、`state`）を加えます。各処理は判定の前にトレース（`--trace-out`。既定は監査ログのパスの拡張子を `.trace.jsonl` にしたもの）に書きます。判定した各メッセージ、保留の期限が過ぎたときの行 `{"t": t}`、`withdraw` と `host_events` の行、停止時の最終行 `{"t": t}` です。そのトレースに対する `cadl agents-run FILE --trace TRACE --audit A.jsonl` は、同じ判定をし、バイト単位で同じ監査ログを書きます（同じ `--audit-key` を使う場合）。監査ログまたはトレースがすでにあるとき、ゲートウェイは起動しません。保留の時間切れがクライアントの要求の時間切れより長いと、先にクライアント側で終わります。`timeout:` はそれに合わせて選んでください。

対象外：ゲートウェイは `values` を観測しません（量を読む規範は、その量を欠けたデータとして扱う。G.5）。また、1行目より後にホストが作る契約インスタンスを作成・終了しません。MCP 2026-07-28では、サーバが `input_required` で応答した `tools/call` をクライアントは新しい要求として再送し、それは改めて判定されます。承認者の道具は、ゲートウェイが公開する保留中のメッセージを信頼します。

#### A2Aゲートウェイ

`cadl a2a-gateway`（0.3.25から）は、一つのA2Aエージェント、つまりファイル中のアクター `ACTOR` の前に判定点を置きます。[A2A 1.0.0](https://a2a-protocol.org/v1.0.0/specification/) のJSON-RPCバインディングを受けるHTTPサーバです。要求は、パス `/` へのHTTP POSTで送るJSON-RPC 2.0で、`Content-Type: application/json` とヘッダ `A2A-Version: 1.0` を持ちます。ストリームのメソッドにはServer-Sent Eventsで応答します（0.3.26から）。0.3.28からは、同じポートでA2A 11.3の経路によるHTTP+JSONのバインディングも受けます。任意で入れる追加の依存 `pip install 'cadl-lang[grpc]'` を入れれば、`--grpc-listen` でgRPCのバインディングも受けます。クライアント証明書を求めること（相互TLS、`--client-ca`）と、許可リストのもとでプッシュ通知を中継すること（`--push-allow`）もできます。許可した要求は、`--upstream URL` のエージェントにJSON-RPCで送ります。`--upstream-binding rest` を与えたときは、HTTP+JSONのバインディングで送ります。

```bash
cadl a2a-gateway FILE --agent-actor ACTOR --upstream URL --client-keys CLIENTS.json \
    --audit OUT.jsonl [--audit-key FILE] [--approvals DIR --approver-keys KEYS.json] \
    [--trace-out TRACE.jsonl] [--init FIRST.json] [--listen HOST:PORT] [--port-file FILE] \
    [--tls-cert CERT.pem --tls-key KEY.pem [--client-ca CA.pem] [--max-handshakes N]] \
    [--agent-card CARD.json] [--upstream-binding jsonrpc|rest] \
    [--push-allow ENTRY ... [--push-url URL] [--push-timeout SECONDS] [--push-ca CA.pem]] \
    [--grpc-listen HOST:PORT [--grpc-port-file FILE] [--max-grpc-connections N] \
      [--max-grpc-connections-per-address N] [--max-grpc-connections-per-prefix N]] \
    [--upstream-token FILE] [--upstream-timeout SECONDS] [--read-timeout SECONDS] \
    [--max-readers N] [--max-readers-per-address N] [--max-readers-per-prefix N] \
    [--max-handlers N] [--max-handlers-per-actor N] [--max-holds-per-actor N] \
    [--hold-key FILE] [--poll SECONDS]
```

`ACTOR` は、`agent: { kind: llm_agent, protocol: a2a }` を持つファイル中のアクターで、すべてのメッセージの受信者です。既定の待ち受けアドレスは `127.0.0.1:8470` です。ループバック以外のアドレスには `--tls-cert` と `--tls-key` が必要です。

**送信者。** すべての要求は、本文をJSONとして読む前に認証します。`--client-keys CLIENTS.json` は、送信してよい各アクターをその鍵に対応づけます（形式は `--approver-keys` と同じで、各アクターはファイル中のアクターでなければならない）。要求はヘッダ `Authorization: CADL-HMAC actor="PLANNER", nonce="<16進32桁>", mac="<16進64桁>"` を持ちます。`mac` は、`{"scheme": "cadl-a2a-request/1", "actor", "run_id", "nonce", "method": "POST", "path": "/", "body_sha256"}` の正規形JSON（G.8）に対する、アクターの鍵でのHMAC-SHA256です。`cadl.a2a_gateway.authorization(key, actor, run_id, body)` がこのヘッダを計算します。すべての応答は、ゲートウェイの実行IDをヘッダ `CADL-Run-Id` に持ち、401の応答は `WWW-Authenticate: CADL-HMAC run="..."` にも書きます。ノンスは一つの実行の中で一度だけ受け付け、実行IDは要求を一つの実行に結びつけます。実行中のノンスはアクターごとにメモリに保持し、一つのアクターの認証された要求が1000000に達すると、起動し直すまでそのアクターの以後の要求を拒否します（`nonce_limit`）。他のアクターの要求には影響しません。ヘッダ `A2A-Version` と `Content-Type` はMACの対象ではありません。正しいヘッダのない要求にはHTTP 401で応答し、理由 `missing`、`malformed`、`unknown_actor`、`bad_mac`、`replayed` のいずれかで `unauthenticated_request` として記録します（記録は1秒に20件までで、その次の記録に記録しなかった要求の数を `;not_recorded_before=N` として書く）。認証された送信者がなければメッセージはなく、何も判定しません。クライアントの `Authorization` ヘッダはエージェントに渡しません。`--upstream-token FILE` は、エージェントに対するゲートウェイ自身のBearerトークンを与えます。ループバック以外のアドレスの `http://` のエージェントに対しては、このオプションを拒否します。

**メッセージ。** A2Aのメッセージは操作の名前を持ちません。CADLのメッセージの名前と引数は拡張から取ります。メッセージは `extensions` に `https://www.ertl.jp/cadl-spec/a2a/agents-message/v0.1` を挙げ、`metadata` のそのURIの下に `{"name": "summarize", "args": ["..."]}` を持ちます（名前は64文字以下、引数は256文字以下の文字列で16個まで）。このとき、アクター `X` の `SendMessage` は、到着時刻のメッセージ `X -> ACTOR : summarize` です。`--agent-card` で与えるエージェントカード（`/.well-known/agent-card.json` で返す）は、`capabilities.extensions` にこの拡張を宣言していなければなりません。宣言していなければ、ゲートウェイは起動しません。

| 要求 | ゲートウェイの処理 |
|---|---|
| `SendMessage` | `allow` なら、ゲートウェイのJSON-RPCのIDを付け、構文解析した結果を改めて直列化して送ります。エージェントの応答はクライアントのIDを付けて返します。`deny` なら、ゲートウェイのタスクを `TASK_STATE_REJECTED` の状態で返します。その状態メッセージには、理由、規範、保留、代替のメッセージ、`notify` を書きます（拡張のURIの下の `metadata` にも書く）。`escalate` なら保留します。応答を待つ（ブロッキングの）要求には、保留が閉じるまで応答しません。`configuration.returnImmediately` のある要求には、`TASK_STATE_SUBMITTED` の状態のゲートウェイのタスクをすぐに返し、その後の決着は `GetTask` で見えます。解放した要求はエージェントに送ります。却下、期限切れ、取り消しになった要求と、解放が拒否された要求には、拒否のタスクを返します。拡張のないメッセージと、`counts_as:` 規則の承認または却下と同じ名前のメッセージは、判定せずに拒否し、`unauthorised_message`（`no_name`、`approval_as_message`）として記録します |
| `SendStreamingMessage`（0.3.26から） | `SendMessage` と同じく、何も渡す前に一度だけ判定します。応答はServer-Sent Eventsのストリームで、各イベントは、ゲートウェイが直列化し直した一つのJSON-RPCの応答です。`allow` なら、エージェントのイベントを一つずつ中継します。各イベントはこの要求への応答であり、同じクライアントの一つのタスクまたはメッセージに属していなければならず、そうでなければ `InvalidAgentResponseError`（-32006、`invalid_response`）でストリームを終えます。エージェントのコメントと、単独の復帰文字を含む行は渡しません。`deny` なら、拒否のタスクという一つのイベントです。`escalate` なら、保留が閉じるまでSSEのコメントを1秒ごとに送り、その後、解放した要求のイベントか拒否のタスクを送ります。接続を閉じたクライアントは要求を取り下げます。`returnImmediately` があれば、ゲートウェイの提出済みのタスクという一つのイベントです。エージェントからイベントが来ない間も、ゲートウェイはクライアントの接続から読み、1秒ごとにコメントを送ります。接続を閉じたクライアントのストリームは終え、エージェントへの要求も終えます。エージェントの通常のJSONの応答は、16 MiBを上限とし、厳密なUTF-8として読みます |
| `SubscribeToTask`（0.3.26から） | クライアント自身のタスクについてだけ、判定も記録もせずに、そのタスクのエージェントのストリームを渡します。保留中のゲートウェイのタスクには、保留が閉じるまでコメントを送り、その後、エージェントのストリームをゲートウェイのタスクのIDで渡すか、最後のタスクを送ります |
| `GetTask`、`CancelTask` | クライアント自身のタスクについてだけ、判定せずにエージェントに渡します。`CancelTask` は渡す前に `task_cancel` として記録し、`GetTask` は記録しません。他のクライアントのタスクと以前の実行のタスクは `TaskNotFoundError`（-32001）とし、`task_refused` として記録します。保留中の要求に対する `CancelTask` は、その要求を取り下げ（G.6）、`TASK_STATE_CANCELED` を返します。保留の期限がすでに過ぎていたときは、拒否のタスクを返します。`returnImmediately` の要求を解放した後も、そのタスクはゲートウェイのIDのままです。ゲートウェイはエージェントにそのタスクを問い合わせ、ゲートウェイのIDで示します |
| `ListTasks`、`GetExtendedAgentCard` | `UnsupportedOperationError`（-32004）で拒否し、`method_refused` として記録します |
| プッシュ通知のメソッドと `configuration.taskPushNotificationConfig` | `--push-allow` がなければ、`PushNotificationNotSupportedError`（-32003）で拒否し、`method_refused` として記録します。ゲートウェイが、ゲートウェイを置いたネットワークの中から、クライアントの選んだURLに要求を送ることになるためです（サーバサイドリクエストフォージェリ）。`--push-allow` があるとき（0.3.28から）は、下の「プッシュ通知」のとおりです |
| その他のメソッド | -32601で拒否し、`method_refused` として記録します |

`SendMessage` が名指せるのは、この実行の中でエージェントまたはゲートウェイが同じアクターに与えたタスク（`taskId`、`referenceTaskIds`）とコンテキスト（`contextId`）だけです。それ以外は `TaskNotFoundError` とし、`task_refused` として記録します。`tenant` は拒否します（-32602）。クライアントのタスクをIDだけで区別しているためです。エージェントが別のアクターに与えたタスクやコンテキストは、そのアクターには示しません（`InvalidAgentResponseError`、-32006。`invalid_response` として記録する）。

**HTTPと本文の扱い。** HTTPでは、`Content-Length` が一つで1 MiB以下であること、`Transfer-Encoding` がないこと、一つの接続に要求が一つであることを求めます。コロンの前にスペースまたはタブのあるヘッダ行（標準ライブラリの構文解析器はこれをヘッダの終わりとみなします）、折り返したヘッダ行、単独の復帰文字には400で応答します（0.3.26から）。本文は、MCPゲートウェイの行と同じく厳密に構文解析し（重複したキー、NaN、`1e999`、深い入れ子、対になっていないサロゲート、不正なUTF-8は拒否する）、構文解析したオブジェクトを、ASCII以外の文字をすべてエスケープした詰めたJSONとして送ります。`A2A-Version` は、前後のスペースを除いて `1.0` または `1.0.N` でなければなりません。ないとき、または別の値のときは `VersionNotSupportedError`（-32009）です。A2A 1.0.0では、ヘッダがないことは0.3を意味します。失敗しうる処理は判定の前にすべて行います。許可または解放の後にそれでも要求を送れなかったときは、クライアントに -32603 を返し、ログに `forward_failed` を書きます。送った後に正しい応答が返らなかったときは、クライアントに -32006 を返し、ログに `invalid_response` を書きます。保留中の応答を待つ（ブロッキングの）要求について、クライアントが接続を閉じたとき、または送信側だけを閉じたときは、その要求を取り下げます。要求が保留されている間にクライアントが送るバイトは、読んで捨てます。`returnImmediately` の要求を解放した後に渡せなかったときは、そのタスクを `TASK_STATE_FAILED` にし、ログに `forward_failed` を書きます。要求行、ヘッダ、本文は、合わせて `--read-timeout` 秒（既定は10）以内に届かなければなりません。届かないときは接続を閉じ、`read_timeout` として記録します。認証の前に同時に読む接続は、一つのリモートアドレス（IPv4のアドレス、またはIPv6のアドレスの/64のプレフィックス。IPv4射影アドレスはそのIPv4アドレスとして数えます）について `--max-readers-per-address` 個（既定は4）まで、一つのIPv4の/24またはIPv6の/48について `--max-readers-per-prefix` 個（既定は16、0.3.26から）まで、全体で `--max-readers` 個（既定は64）までです。上限に達したアドレスまたはプレフィックスからの接続には、すぐにHTTP 503で応答します（`overloaded`、`address` または `prefix`）。全体の上限に達しているときは、接続は空きを最大1秒待ち、空かなければHTTP 503を受け取ります（`overloaded`、`readers`）。`--max-handlers`（既定は64）に数えるのは、認証された後の要求と保留中の要求だけで、それを超えるとHTTP 503で応答します（`overloaded`、`handlers`）。一つのアクターについては、同時に処理する要求を `--max-handlers-per-actor` 個（既定は16。保留中の応答を待つ要求と、遅いエージェントへの要求も数える）まで、保留を応答を待つかどうかにかかわらず `--max-holds-per-actor` 個（既定は64）までとし、それを超える要求には判定の前にHTTP 503で応答します（`overloaded`、`handlers_per_actor ACTOR` または `holds_per_actor ACTOR`）。保留の上限に達したアクターは、保留にならないメッセージも送れません。`unauthenticated_request`、`overloaded`、`read_timeout` の記録は、事象ごとに1秒に20件までです。すべての接続の読み込みの期限は、一つのスレッドで管理します。残る問題は次のとおりです。少なくとも四つの/24（または/48）のネットワークにあるホストが、ネットワークごとに16個ずつ接続し続ければ、64個の読み込みをすべて使い続けることができ、その間ゲートウェイは新しい接続をすべて拒否します。一つのプロセスは、認証の前にそのようなホストをクライアントと区別できません。これが問題になる場合は、接続の数を制限するプロキシやファイアウォールの後ろにゲートウェイを置いてください。プロキシの後ろでは、すべてのクライアントがプロキシのアドレスを持つので、`--max-readers-per-address` を大きくするか、要求を受け取り終えてから渡すプロキシを使ってください。SIGTERMまたはSIGINTを受けると、保留中の応答を待つ（ブロッキングの）要求に -32603 で応答し、トレースとログの終わりを書きます。判定点が失敗していたときは終了ステータス1で終わります。

**相互TLS（0.3.28から）。** `--client-ca CA.pem`（と `--tls-cert`、`--tls-key`）を与えると、クライアントはそのCAの証明書を示す必要があります。証明書のサブジェクトはコモンネームをちょうど一つ持ち、それは `--client-keys` のアクター（`CN=PLANNER`、`CN=WORKER[2]`）でなければなりません。TLSのハンドシェイクは、接続が読み込みの枠を取る前に、読み込みの期限の中で行い、同時に行うのは `--max-handshakes` 個（既定は256）までです（それを超える接続は空きを最大1秒待ち、空かなければ閉じます。`overloaded`、`handshakes`）。ハンドシェイクに失敗した接続と、そのようなアクターを名指さない証明書の接続は閉じ、`handshake_failed` として記録します（1秒に20件まで）。したがって、証明書を持たない相手が読み込みの枠を使うことはありません。証明書はヘッダ `CADL-HMAC` の代わりにはなりません。ヘッダは同じアクターを名指す必要があり（違えば `certificate_mismatch`）、そのMAC、ノンス、実行IDによって、再送の防止と、要求を本文と実行に結びつけることを保ちます。TLSのセッションだけではこれらはできません。このとき防げないのは、正しい証明書を使って、読み込みの期限まで時間をかけるハンドシェイクです。ハンドシェイクが終わるまで相手が誰かはわからないので、そのようなハンドシェイクは、アドレスごととプレフィックスごとの上限の範囲で、読み込みの期限までスレッドを一つとハンドシェイクの枠を一つ使います。少なくとも16個の/24（または/48）のネットワークにあるホストは、続けている間、256個の枠をすべて使えます。プッシュ通知を使うときは、エージェントのアクターも証明書を示せます。その証明書でできるのは通知を届けることだけです。

**HTTP+JSONのバインディング（0.3.28から）。** `POST /message:send`、`POST /message:stream`、`GET /tasks/{id}`（クエリ `historyLength`）、`POST /tasks/{id}:cancel`、`POST` または `GET /tasks/{id}:subscribe`、`--push-allow` があれば `/tasks/{id}/pushNotificationConfigs` の経路を、JSON-RPCと同じポートで受けます。要求はJSON-RPCの要求と同じく認証します。MACの `method` と `path` には、HTTPのメソッドと、送られたとおりの要求ターゲットの全体（パスとクエリ）を使います（`authorization(..., path=target, method=...)`）。その後、要求をJSON-RPCの要求に直し、判定、保留、記録、転送はJSON-RPCと同じに行います。したがってトレースとログもJSON-RPCの場合と同じです。本文は `application/json` または `application/a2a+json` で、`GET` と `DELETE` は本文を持ちません。未知の経路（`/{tenant}/...` も含む）、未知のクエリパラメータ、パスと食い違う本文は拒否します。エラーは、A2A 5.4のHTTPステータスと `ErrorInfo`（`reason` は `TASK_NOT_FOUND` など、ドメインは `a2a-protocol.org`）を持つ `google.rpc.Status` のオブジェクトです。ストリームは `StreamResponse` のオブジェクトを `data` に持ち、エラーは `error` のイベントで送ります。`GET /tasks` と `GET /extendedAgentCard` は拒否します（`UNSUPPORTED_OPERATION`）。`--upstream-binding rest` を与えると、ゲートウェイはこのバインディングでエージェントに送り（`--upstream` はその経路の基点）、応答をJSON-RPCの応答と同じく厳密に検査します。A2Aの `ErrorInfo` を持たないエラーの応答は `invalid_response` です。

**プッシュ通知（0.3.28から）。** `--push-allow ENTRY`（繰り返し可）を与えると、ゲートウェイは、クライアント自身のタスクについて、`SendMessage` またはA2A 3.1.7から3.1.10のメソッドによるプッシュ通知の設定を受け付けます。ENTRYは、URL（`/` で終わるパスは前方一致）、`HOST[:PORT]`（ポートがなければ、そのホストのすべてのポートとパス）、IPアドレスまたはネットワーク（一致したホストが解決したアドレスと、アドレスが一つのときはそれをホストとするURLを認め、ホスト名をそれだけでは認めない）のいずれかです。パスに `.` や `..` のセグメント、バックスラッシュ、パーセント符号化したドット、スラッシュ、バックスラッシュ、パーセント記号（`%252e` も含む）を持つURLは拒否します。`SendMessage` の応答がエラーであったとき、またはタスクを示さなかったときは、その設定を取り除きます（`push_dropped`、`no_task`）。エージェントはクライアントのURLを知りません。エージェントには、`--push-url`（既定は `--listen` のアドレスで、そのときはループバックでなければならない）に `/cadl-push/<id>` を加えたURLと、ゲートウェイの秘密のトークンと資格情報を持つ、ゲートウェイの設定を渡します。そこに届いた通知を検査し（秘密の値、設定のタスクの `StreamResponse` が一つであること、そのタスクの所有者が設定のクライアントであること）、クライアントのトークンと資格情報を付けてクライアントのURLに中継します。`SendMessage` で与えた設定は、エージェントの応答が示すタスク（結果、またはストリームの最初のイベント）に結び付けます。それより前の通知は捨て、通知によってタスクやコンテキストがクライアントのものになることはありません。クライアントのURLは、ENTRYのどれかに一致し、ホストがループバックでなければ `https` であり、解決したアドレスがすべてグローバルユニキャストか、IPのENTRYに含まれるものでなければなりません（クラウドのメタデータのアドレスは、そのアドレス自体をENTRYに挙げたときだけ）。これは設定を受け取るときと、通知のたびに検査します。要求は検査したアドレスに送り、リダイレクトには従わず、接続から応答まで `--push-timeout`（既定は10秒）以内に終え、応答は64 KiBまでしか読みません。`https` のURLのためのCAは `--push-ca` で与えます。記録は、`push_config`（エージェントに渡した設定、または削除した設定）、`push_refused`、`push_sent`、`push_failed`、`push_dropped`（1秒に20件まで）です。`SendMessage` の設定のトークンと資格情報は、公開する保留には表示しません。GetとListにはゲートウェイが応答します。Deleteは中継をすぐに止め、エージェントにも渡します。

**gRPC（0.3.28から、任意で入れる追加の依存）。** `pip install 'cadl-lang[grpc]'` は `a2a-sdk[grpc]` を入れます。その `a2a.types.a2a_pb2` は規範である `a2a.proto` から生成したものです。`--grpc-listen HOST:PORT` は、その `lf.a2a.v1.A2AService` を受けます（TLSと相互TLSは `--listen` と同じオプションで指定し、ループバック以外のアドレスにはTLSが必要です）。メタデータ `authorization: CADL-HMAC ...` のMACは、`POST`、RPCのパス（`/lf.a2a.v1.A2AService/SendMessage`）、届いたとおりの直列化された要求のSHA-256を対象とします。メタデータ `a2a-version` は `1.0` でなければなりません。`a2a.proto` が定義しないフィールドを持つメッセージは拒否します。エラーは、`grpc-status-details-bin` に `ErrorInfo` を持つgRPCのステータスで、ストリームはサーバストリーミングのRPCです。gRPCは呼び出しをそのメッセージが届く前にワーカに渡すので、ゲートウェイはメッセージを自ら受け取ります。正しい形のヘッダ（鍵を持つアクター、相互TLSでは証明書のアクター）のない呼び出しは、メッセージを待つ前に拒否します。メッセージが届くまで、呼び出しはまだ認証していない呼び出しの枠を一つ使い（HTTPと同じく、リモートアドレスごとに4個、/24または/48ごとに16個、全体で64個）、`--read-timeout` を過ぎれば取り消します。ワーカは、これらの分を、`--max-handlers` に16を加えた数の認証された呼び出しの分とは別に持ちます。HTTP/2では、一つの接続の同時のストリームを16個まで、メタデータを16 KiBまでとし、pingを制限し、何もしない接続は2分で閉じます。要求のメッセージは1 MiBまでです。`grpcio` には接続を数えたり拒否したりする箇所がないので、ゲートウェイは `--grpc-listen` の接続を前段の待ち受け（一つのスレッドで動き、標準ライブラリだけを使う）で自ら受け付け、各接続のバイト列をそのままgRPCのサーバに渡します。gRPCのサーバは、モード0700のディレクトリにあるUnixソケットで待ち受けます（Unixソケットのない環境では127.0.0.1で待ち受けます）。TLSと相互TLSはこれまでどおりgRPCで終端します。前段の待ち受けが同時に保つ接続は、一つのリモートアドレスについて `--max-grpc-connections-per-address` 個（既定は4）まで、一つの/24または/48について `--max-grpc-connections-per-prefix` 個（既定は16）まで、全体で `--max-grpc-connections` 個（既定は64）までです。これを超える接続はすぐにリセットします（`overloaded`、`connections`）。受け付けてから `--read-timeout` 秒以内に呼び出しが届かない接続（何も送らない接続、TLSを始めない接続、ハンドシェイクの遅い接続、ハーフオープンの接続）はリセットします（`read_timeout`、`grpc handshake`）。呼び出しの後は、呼び出しのない状態が2分続いた接続と、keepaliveのpingに20秒以内に応答しない接続を、gRPCが閉じます。前段の待ち受けは、クライアントへ送るバイトが256 KiBまでたまっている間はgRPCからの読み込みを続けるので、クライアントが何も読まない場合にもこの切断を知り、遅くとも5秒後にクライアント側の接続をリセットします。それより多くたまった場合、またはgRPCがクライアントからのバイトを受け取らない場合に、滞留が30秒以内に解消しなければ、両側をリセットします（`read_timeout`、`grpc stalled`）。接続は、そのソケットを閉じ終えるまで上限に数えます。`localhost` を指定したときは、gRPCと同じく127.0.0.1と::1の両方で待ち受けます。Unixソケットのない環境では、同じ計算機の他のプロセスは上限を受けずにgRPCのループバックのポートへ接続できますが、その呼び出しは拒否します。gRPCがゲートウェイに渡す前に拒否する呼び出し（未知のメソッド、ワーカの不足）は最初の呼び出しに数えず、チャネルを早く開いて期限を過ぎても呼び出さないクライアントの接続もリセットします。どちらのクライアントも接続し直します。gRPCのサーバへの各接続は接続ごとに異なるアドレスから張り、ゲートウェイはそのアドレスをクライアントに対応付けます。そのため、まだ認証していない呼び出しの上限は、クライアントの実際のアドレスで数えます。したがって、一つのアドレスの接続は4個まで、同時の呼び出しは64個までです。残る問題は次のとおりです。呼び出しが一つ届いた接続は期限を過ぎても残るので、相互TLSを使わない場合、4個のネットワークに16個のアドレスを持つホストは、呼び出しを続ける間とその後の2分5秒間、64個の接続を保てます（読まないクライアントに256 KiBを超えるバイトがたまっている場合は30秒間。ゆっくり読み続けるクライアントは、たまっているバイトを読み終えるまでの時間だけ延ばせます）。プロキシの後ろでは、アドレスごとの上限とプレフィックスごとの上限を大きくしてください。

**承認。** 承認とその他のメッセージは、MCPゲートウェイと同じく受け箱から届きます（`--approvals DIR --approver-keys KEYS.json`、`cadl mcp-approve`、`cadl mcp-message`）。保留中のメッセージの `params`（`SendMessage` の要求の全体）は `DIR/holds/h-N.json` に公開し、承認はそのSHA-256に結びつきます。承認はA2Aのメッセージとしては受け付けません。受け箱からの承認はランタイムに与えるだけなので、承認の規則の受信者が集合、範囲、または添字変数であるとき（`OPERATOR -> SUMMARIZER[*] : approve`）は、具体的なアクターとします。添字変数の値は、保留の根拠の契約インスタンスの束縛から取ります（0.3.26から）。それ以外では、参照が `ACTOR` に一致しうるなら `ACTOR`（MCPゲートウェイでは、ツールサーバ、次に `--agent`）、そうでなければ集合の最初に宣言された要素です。ゲートウェイのアクターの契約が保留を作るのに、`approval` を作る規則を持たないとき、ゲートウェイは起動しません（このファイルはすでにG-9によって誤りです）。時刻、トレース、再生はMCPゲートウェイと同じです。そのトレースに対する `cadl agents-run` は、バイト単位で同じ監査ログを書きます。

**署名した保留（0.3.26から）。** `--hold-key FILE` を与えると（どちらのゲートウェイも）、公開する保留はすべて `"kind": "hold"` と `hold_mac` を持ちます。`hold_mac` は、保留（保留中のメッセージ、時刻、引数とそのSHA-256）の正規形JSONに対する、その鍵でのHMAC-SHA256です。`run.json` は `"holds_signed": true` を持ちます。`cadl mcp-approve --hold-key FILE` は、保留を表示または署名する前にこれを検証し、署名のない保留と署名の誤った保留を拒否します。`--hold-key` がないときは、署名された保留を拒否します。署名のない保留には警告を出しますが、受け箱の `run.json` が `holds_signed` を示すとき、または `--require-signed-hold`（受け箱に依存しません）を与えたときは拒否します。どの場合も、表示する引数が保留の示すSHA-256を持たないときは拒否し、引数はそのままの形で表示されない文字をすべて `\uXXXX` として表示します（制御文字、双方向の上書き、幅のない文字などの書式文字、私用の文字、サロゲート、未割り当てのコードポイント、行と段落の区切り）。保留の鍵はゲートウェイの鍵で、監査ログの鍵とも、どの承認者の鍵とも、A2Aゲートウェイのどのクライアントの鍵とも異なっていなければなりません。承認者は検証のためにこの鍵を持ち、受け箱に書けても鍵を読めない者が保留を書き換えたとき、その改ざんを検出できます。承認の認証はこの鍵に依存しません。保留の鍵がなくても、ゲートウェイは保留中のものと異なる引数の承認を拒否します（`wrong_message`）。後方互換性のため、このオプションは任意です。承認者にとって保留が認証されるのは、`mcp-approve` に `--hold-key` を与えたときだけです。

**SDK。** `tests/test_sdk_interop.py` は、公式のSDKに対してゲートウェイを試験します。`mcp` 2.3.0のMCPサーバを `cadl mcp-gateway` の後ろに置き、SDKの `ClientSession`（リビジョン2025-11-25）と `Client`（2026-07-28、`server/discover`）から呼び出します。`a2a-sdk` 1.3.0のエージェントを `cadl a2a-gateway` の後ろに置き、`a2a-sdk` のクライアントから、ストリーミングの有無の両方で呼び出します。ヘッダ `CADL-HMAC` は、そのクライアントの `httpx` のイベントフックで加えます。0.3.28からは、相互TLS、HTTP+JSONのバインディング（エージェントへはJSON-RPCとHTTP+JSONの両方）、SDKのエージェントのプッシュ通知の送信、gRPC（直列化した要求に署名する `grpc.aio` のインターセプタを使う）でも試験します。SDKがなければこれらの試験は飛ばし、CIのジョブ `sdk-interop` がPython 3.12にSDKを入れて実行します。SDKはcadlの依存ではありません。

対象外：`ListTasks`、`GetExtendedAgentCard`、テナント、ヘッダ `A2A-Extensions` と `ExtensionSupportRequiredError`（-32008）、クエリパラメータとしての `A2A-Version`、部分 `raw` がbase64であることの検査、`CADL-HMAC` 以外のA2Aの認証方式（A2Aクライアントがこのヘッダを計算する必要があり、クライアント証明書はそれに加えて使う）、ゲートウェイの保留中のタスクに対する `CreateTaskPushNotificationConfig`（設定は `SendMessage` で与える）、失敗した通知の再送、未知のコンテキストIDを指定する要求。ゲートウェイは、タスクの所有者とプッシュ通知の設定を実行をまたいで保持しません。試験は偽のA2Aエージェントと偽のクライアント、および `a2a-sdk` 1.3.0で行っています。

### 運用モード遷移の分析

`cadl regime-map` は運用モード（regime）の遷移グラフを分析します：

- `transitions:` の定義から運用モードの有向グラフを構築
- **到達可能性分析** — 初期状態からのBFS探索で、到達できないモードを検出
- **行き止まり状態（dead state）の検出** — 出て行く遷移を持たないモードを特定
- **サイクル検出** — TarjanのSCCアルゴリズムでモード間の循環を検出
- **最短経路** — 任意の2つのモード間の最短経路をBFSで算出
- **出力形式**: テキスト要約、Graphviz DOT、JSON

### IEC 62853を参考にした要約

`cadl iec62853` は、CADL定義をIEC 62853（オープンシステムディペンダビリティ）の主題に沿って要約します。指標名はCADL独自のものであり、規格の用語ではありません。また、このレポートはIEC 62853への適合性を評価するものではありません。

| CADL概念 | CADL独自指標 |
|---|---|
| alpha（情報共有度） | 情報透明性レベル |
| beta（権限集中度） | ガバナンス集中度指標 |
| lambda（インセンティブ強度） | ステークホルダ整合性メトリクス |
| ContractDef | サービスレベル合意（SLA） |
| ViolationBlock | 障害応答仕様 |
| TransitionDef | 運用状態機械 |
| SoSタイプ（D/A/C/V） | システム統合レベル |

### シミュレータ設定生成

`cadl sim-*` コマンドはCADL定義を**3層の中間表現（IR）**に変換（lowering）し、シミュレータ固有の設定ファイルを生成します：

```
CADL YAML ─── パーサ ──► AST ─── lowering ──► 3層IR ─── ジェネレータ ──► シミュレータ設定
                                                    │
                                       ┌────────────┼────────────┐
                                       ▼            ▼            ▼
                                   Layer 1       Layer 2       Layer 3
                                  制度/          プロトコル/    アルゴリズム/
                                  ガバナンス      インタラクション  オペレーション
```

| レイヤ | 研究上の関心 | IR型 | 例 |
|---|---|---|---|
| Layer 1: 制度 | 誰が決定？ 誰が知る？ 誰が利益を得る？ | `ActorSpec`, `ContractSpec`, `GovernanceParams` | beta=0.8 → DISPATCHERが権限を持つ |
| Layer 2: プロトコル | どう相互作用？ タイムアウト時は？ | `ProtocolSpec`, `StepSpec` | メッセージ: ROBOT→DISPATCHER : failure_report |
| Layer 3: アルゴリズム | 中央 vs 局所で何が動く？ | `AlgorithmSpec` | central=ECBS, local=tracking_only |

3種のシミュレータターゲット：

| ターゲット | 形式 | キー規約 | 用途 |
|---|---|---|---|
| `python` | YAML | snake_case、プランナ紐付け | Python系MAPF/MASシミュレータ |
| `unity` | JSON | camelCase、Prefabヒント | Unity3D可視化 |
| `go` | JSON | snake_case、インタフェース仕様 | Go並行シミュレータ |

```bash
# IRを検証
cadl sim-validate examples/a_sos_robot_delivery.cadl

# IRを出力（YAML/JSON）
cadl sim-ir examples/a_sos_robot_delivery.cadl
cadl sim-ir examples/c_sos_taxi_fleet.cadl --format json

# シミュレータ設定を生成
cadl sim-gen examples/a_sos_robot_delivery.cadl --target python -o sim_config.yaml
cadl sim-gen examples/a_sos_robot_delivery.cadl --target unity -o sim_config.json
cadl sim-gen examples/c_sos_taxi_fleet.cadl --target go -o sim_config.json
```

#### A-SoS vs C-SoS の比較

| 特性 | A-SoS（ロボット配送） | C-SoS（タクシー群） |
|---|---|---|
| SoSタイプ | 認知型（Acknowledged） | 協調型（Collaborative） |
| 意思決定権限 | DISPATCHER（中央集権） | TAXI[*]（各タクシーが自律決定） |
| beta（契約ごと） | 0.8, 0.9（集中型） | 0.1, 0.05（分散型） |
| 中央プランナ | ECBS | aggregation_only |
| 局所プランナ | tracking_only | LRA* + 局所衝突回避 |
| 共有方式 | アップリンク + ブロードキャスト | ピアツーピア ブロードキャスト |

#### Raspimouse群ロボットシミュレータ — D-SoS / C-SoS / MCP-SoS の比較

同一の5台ロボット・11ノードグラフネットワーク上で、異なるSoSパラダイムを記述する3つのCADL定義です。`cadl sim-gen --target unity` により、raspimouse-swarm-simulator（研究用リポジトリで非公開。ハンズオンに必要な部分は [cadl-raspimouse-simulator](https://github.com/ertlnagoya/cadl-raspimouse-simulator) として公開）のUnity設定JSONを自動生成できます。

| 特性 | D-SoS（指示型） | C-SoS（協調型） | MCP-SoS（認知型） |
|---|---|---|---|
| 意思決定主体 | ARBITRATOR | ROBOT[*]（提案）+ ARBITRATOR（検証） | LLM_AGENT |
| beta | 0.9 | 0.3 | 0.6 |
| alpha | 0.2 | 0.7 | 0.9 |
| 中央プランナ | NaiveDijkstra | DirectionDijkstra | LLM_Dijkstra |
| 局所プランナ | なし | DirectionDijkstra | NaiveDijkstra + OccupancyAware |
| 通信方式 | NATS req/res | NATS req/res + リソースクエリ | MCPツール |
| 運用モード | NORMAL ↔ CONGESTED | NORMAL ↔ CONGESTED | NORMAL ↔ COLLISION_RESOLUTION ↔ DEADLOCK |

beta と alpha の行は、各定義の主となる契約の値です。C-SoS と MCP-SoS のファイルには、別の値を持つ2つ目の契約があります。

```bash
# 3モードのUnity設定を生成
cadl sim-gen examples/raspimouse_d_sos.cadl --target unity -o output/raspimouse_d_sos_unity.json
cadl sim-gen examples/raspimouse_c_sos.cadl --target unity -o output/raspimouse_c_sos_unity.json
cadl sim-gen examples/raspimouse_mcp_sos.cadl --target unity -o output/raspimouse_mcp_sos_unity.json
```

### トレースに対する規範の判定

`cadl norms-eval` は、規範拡張（仕様書の付録F、F.4とF.5）の判定を、記録済みのトレース上の一つの契約インスタンスについて計算します。オフラインのモニタであり、システムもライフサイクルの状態機械も実行しません。

```bash
cadl norms-eval examples/norms_robot_delivery.cadl \
    --trace examples/traces/norms_robot_delivery_b_reparation.jsonl
cadl norms-eval FILE --trace TRACE [--contract ID] [--format text|json]
```

ファイルは `norms: 0.1` を宣言し、`cadl check` に通る必要があります。`--contract` は、`norms:` か `counts_as:` を持つ契約が複数あるときにだけ必要です。終了ステータスは、判定が `violated` の規範も保証もなければ0（`excused` と `pending` は失敗にしません）、あれば1、使い方の誤り（`norms: 0.1` がない、型検査エラー、トレースの形式誤り）は2です。

**トレース。** JSON Lines形式で、1行が一つの観測です。時刻は非減少とします。一つのトレースが一つの契約インスタンスです。

```json
{"t": 0.0, "bindings": {"i": 2}, "values": {"DISPATCHER.is_operational": true, "ROBOT[2].status": "Idle"}}
{"t": 1.0, "messages": [{"from": "DISPATCHER", "to": "ROBOT[2]", "name": "route_assignment"}], "state": "Assigned"}
{"t": 2.5, "messages": [{"from": "ROBOT[2]", "to": "DISPATCHER", "name": "ack", "args": ["accepted"]}]}
{"t": 120.0, "values": {"ROBOT[2].status": "Delivered"}, "state": "Completed"}
```

| キー | 意味 |
|---|---|
| `t` | 絶対値が2**53以下の有限の数で、秒単位の時刻（必須）。述語の `now` |

数は正確に扱います（F.5）。トレースの数は書かれたとおりの十進数として読みます（`1700000000.123` はちょうどその値です）。時刻は許容誤差なしで比べ、`+`、`-`、`*`、`/`、`avg` は有理数で計算するので、`0.1 + 0.2 == 0.3` が成り立ちます。有効桁が400を超える数、小数点以下が400桁を超える数、絶対値が1.7976931348623157e308（有限の倍精度浮動小数点数の最大値）を超える数は、行番号を示すトレースの誤りとします（生成したモニタと `cadl agents-run` も同じです）。そのようなリテラルを含む述語は評価しません。数で書いた `deadline:` も正確に扱います（`0.0005` は0.5ミリ秒です）。JSONの結果では時刻と数をfloatで書きます。0.3.25までは数はfloatで、時刻は1e-9秒の許容誤差で比べていました。
| `bindings` | 先頭行のみ。インスタンスが束縛する添字（`ROBOT[i]` を `ROBOT[2]` と読む） |
| `request` | 先頭行のみ。`request` のフィールド |
| `values` | スナップショットの更新。キーは述語に書く名前（`ROBOT[2].battery`、`delivery_time`）。値は変更されるまで保たれる |
| `messages` | この観測で送られたメッセージ。`from`、`to`、`name`、省略可能な `args`。メッセージイベントは送信者・受信者・名前で照合し、CADL側に引数があれば（`ack(accepted)`）`args` と一致する必要がある。`from` と `to` は具体的なアクター（`DISPATCHER`、`ROBOT[2]`）、`name` は識別子で、余分な空白なしで書きます。アクター参照はF.3のとおりに照合します。`ROBOT[1..2]` は `ROBOT[1]` と `ROBOT[2]` だけに、`ROBOT[*]` と束縛のない `ROBOT[i]` は宣言された集合の要素だけに一致し、宣言されていないアクターには決して一致しません |
| `state` | この観測の後の、インスタンスのライフサイクル状態。変更されるまで保たれ、初期値は `lifecycle.initial`。ある観測の述語は、その観測の前の状態を読む（F.5の手順6が最後であるため） |
| `amend` | CADLファイルの新しい版のパス（トレースファイルからの相対）。この観測で契約を改正する（後述） |
| `end` | `true` はこの観測でインスタンスを終了する。最初の終端状態でも終了し、どちらもなければ最終行で終了する。最終行が `"end": false` のときは、トレースを途中までの記録とみなし、清算しない |

**出力。** テキストの報告は、タイムラインと、規範、違反、制度的事象、権限のない行為、前提、保証の表からなります。`--format json` は同じ内容を一つのオブジェクトで出力します。

| キー | 内容 |
|---|---|
| `file`、`trace`、`contract`、`norms_version` | 評価の対象 |
| `instance` | `bindings`、`start`、`end`（途中までの記録ではnull）、`ended`、`end_reason`、`state`（最後のライフサイクル状態）、`observations` |
| `norms[]` | `id`、`kind`、`bearer`、`state`（F.5の六つの状態）、`verdict`、`activated_at`、最後の変化の `time` と `reason`、`evaluable`、`history[]`（`t`、`from`、`state`、`reason`） |
| `violations[]` | `t`、`norm`、`severity`、`verdict`（`violated` か `excused`）、`sanction`、`reparation`、`reparation_activated`、`transition`（要求されたライフサイクル状態。報告するだけで適用しない） |
| `institutional_events[]` | `t`、`name`、`performer`（メッセージの送信者。述語から作られたものはnull）、`source` |
| `unauthorised[]` | `t`、`act`、`performer`、`reason`。行為者の該当する権能が有効でないときに行われた、権能の内容 |
| `amendments[]` | `t`、`file`、`effects[]`（`norm`、`effect`、`from_state`、`to_state`、`reason`）。`effect` は `carry_over`、`discharge`、`transform`、`activated`、`new_definition`、`removed`、`added` のいずれか |
| `assumptions[]` | `index`、`text`、`temporal`、`status`（`holds`、`failed`、`pending`）、`time`、`reason` |
| `guarantees[]` | `index`、`text`、`temporal`、`verdict`、`time`、`reason` |
| `timeline[]` | `t`、`kind`（`start`、`norm`、`violation`、`event`、`power`、`unauthorised`、`ignored`、`amend`、`assume`、`guarantee`、`end`）、`text` |
| `warnings[]`、`violated[]`、`ok` | 警告、判定が `violated` の規範と保証、それが一つもないかどうか |

**読み方。** F.4とF.5が選択の余地を残す箇所は、次のように読みます。

- すべての変化には、それが決まった観測の時刻を付けます。期限切れは、期限の後の最初の観測で検出し、その時点で `violated` か `excused` かを決めます。理由には期限が過ぎた時刻を示します。
- 時相演算子を含む `assume:` / `guarantee:` の項目は、インスタンスの開始時点で一度だけ評価します。含まない項目は状態述語であり、観測ごとに判定します。最初に偽になった観測で失敗とし、偽にならないままインスタンスが終了すれば `holds` / `fulfilled` とします。
- 時相演算子が偽（`within`、`until`）または真（`throughout`）に決まるのは、区間の後の観測があるときだけです。その前にインスタンスが終了すれば項目は `pending` のままで、時相的な内容が決まっていない規範も同様です。
- トレースに値のない名前を使う述語は未定義です。成り立たないものとして扱い、前提や保証を失敗にはせず、その名前を一度だけ警告します。値のない裸の名前は、`==`・`!=`・`IN` で文字列の値と比較されるときにだけ列挙値のリテラルとして読みます（`status == Delivered`）。それ以外（`ok == true`、単独の `flag`）では未定義です。時相演算子と内包表記の集約以外の関数呼出しは評価できないので、その規範や項目は理由を付けて `pending` のままにします。
- 限量子（`exists j in ROBOT: ...`、`for all r in ROBOT[*]: ...`）と、内包表記の集約 `sum`・`count`・`min`・`max`・`avg` は、F.5（「限量子と集約」）のとおりに評価します。定義域は宣言した集合（`A`、`A[*]`、`A[lo..hi]`。内包表記では `lo..hi` も）で、変数はメンバーの添字です（`ROBOT[j]`、`r.load`、`j != k`）。`ROBOT[j+1]` のように添字がメンバーでない参照は未定義です。`count` は、真または0以外の数である要素を数えます。境界が整数なら定義域は閉じており、限量子はクリーネの論理に従います。記号の境界（`ROBOT[1..N]`）は、トレースが `N` を束縛する（`bindings`）か、`N` がその観測で整数の値を持つ量であるときに値を持ちます。`N` という名前の限量子の変数はそれを隠しません（`cadl check` が警告します、N-14）。定義域が閉じるのは、境界が値を持ち、それを超えるメンバー（`N` = 3 のときの `ROBOT[5]`。F.3によりメンバーです）がまだ名指されておらず（名指されれば警告します）、メンバーが10 000個以下のときだけです。それより大きい定義域は警告を出して開いた定義域として評価するので、トレースの大きな `N` が評価を遅くすることはありません。そうでなければ定義域は開いており、既知のメンバーはトレースがそれまでに名指したもの（`ROBOT[3].delivery` のような値のキー、送信者と受信者、契約が `ROBOT[i]` と書く `i` の束縛）です。`exists` は既知の証拠によってだけ真に、`for all` は既知の反例によってだけ偽になり、それ以外は、すべての集約も含めて未定義です（警告を出します）。
- 比較の中の単調な集約（F.5）：内包表記の `count`・`max`・`min` を、限量子も内包表記も含まない被演算子と比べる比較（`<`・`<=`・`>`・`>=`・`==`・`!=`、どちら側でも）は、どの完成形でも値が同じになるとき、既知のメンバーから決めます。`count` は要素が真であるメンバーの数から、それに未定義のものを加えた数までの間にあり（開いた定義域では上端なし）、`max` は定義された値の最大値以上、`min` は最小値以下です。したがって `count(ROBOT[*].delivery == request.id) >= 2` は、`N` に値がなくても、既知のロボット2台がその配送を持った時点で真になり、`count(...) <= 1` はそのとき偽になります。`count(...) == 2` は開いた定義域の上では真になりません。要素がすべて定義された閉じた定義域では、これまでどおり厳密な値です。値の種類が誤っている要素（`max` では文字列、`count` では文字列や記号値）は、引き続き集約を未定義にします。`sum`・`avg`、算術の中の集約、2つの集約の比較は対象外です。
- 定義域の外のワイルドカード参照や範囲参照は略記です（F.5の「ワイルドカード参照と範囲参照」）。契約を読むときに書き換え、それが表す限量子または内包表記として評価します。`sum`・`count`・`min`・`max`・`avg` の引数の中ではメンバーを動きます。`count(ROBOT[*].status == Collision)` は `count(ROBOT[j].status == Collision for j in ROBOT[*])` です。それ以外の場所では、それを含む最小の比較（どの比較にも含まれないときは、`AND`・`OR`・`NOT` の被演算子、限量子の本体、時相演算子の引数のうち最小のもの）がすべてのメンバーについて成り立つ必要があります。`ROBOT[*].battery > 10` は `for all j in ROBOT[*]: ROBOT[j].battery > 10` です。`ROBOT[*].x > ROBOT[*].y` は各メンバーを自分自身と比べ、`ROBOT[*].x == BASE[*].y` はすべての組について成り立つ必要があります。`NOT ROBOT[*].stopped` は `NOT (for all ...)` であり、停止していないロボットがあれば真です。`ROBOT[2..N]` はその範囲を動き、上の開いた定義域の規則に従います。読み方が意外かもしれない場合、`cadl check` が、意図したと思われる限量子の形を示して警告します（N-15）。`NOT` の下、1つの比較の中の2つの集合、`OR` の両方の被演算子の同じ集合、1つの比較の両辺の同じ集合（`ROBOT[*].pos != ROBOT[*].pos` は各ロボットを自分自身と比べます。集約の中も同じです）、`within`・`throughout`・`until` の中の集合（すべてのメンバーを1つの観測で見ます）、評価しない2つの集合にわたる集約です。そのような参照のないファイルの結果は0.3.23と同じです。`cadl.norms_eval.desugar_wildcards(expr)` は書き換えた述語を返します。
- トリガのない規範は、最初の観測の手順1の前に有効になります。手順4の中の順序は、期限、内容、`until:` です。停止中の規範に対する内容は数えません。インスタンスの終了時に見つかった違反は報告しますが、修復規範の有効化も遷移の要求もしません。インスタンスが終了する観測で有効になった規範は `discharged` とします。

**改正。** `"amend": "new_version.cadl"` を持つ行は、その観測のほかの手順の前に、契約をそのファイルの同じidの契約に置き換え、規範インスタンスをF.6のとおりに扱います。有効または停止中のインスタンスは、古いバージョンの `on_amendment:` に従います。`carry_over` は状態と経過時間を保ったまま新しい定義に引き継ぎます（idがなくなるか種別が変われば `discharged`。経過時間が新しい期限をすでに超えていれば、停止中のものも含めて、義務はただちに `violated`、禁止はただちに `fulfilled` になります。ただし、時間的な内容の判定待ちの候補が残っていれば、新しいバージョンを最初から用いた場合と同じく、手順4でその候補が決めます）。`discharge` は解除します。`transform: X` は解除したうえで、未発効の `X` を経過時間を引き継いで有効にします。未発効のインスタンスは新しい定義に置き換わるか削除され、トリガのない新しい規範はただちに有効になり、確定済みのインスタンスは影響を受けません。新しいバージョンの `counts_as:` 規則と `lifecycle:` はその時点から適用します。新しいファイルは `norms: 0.1` を宣言し、`cadl check` に通り、同じidの契約と現在のライフサイクル状態を持つ必要があります。そうでなければ終了ステータスは2です。F.6が定めていない点は次のように読みます。文面が変わらない `assume:` / `guarantee:` の項目は継続し、新しい項目は改正の時点から評価し、未決のまま消えた項目は `pending` のままとします。

`cadl norms-diff OLD NEW [--contract ID] [--format text|json]` は、トレースなしで、この改正が何を引き起こすかを報告します。削除・追加・種別変更された規範、`on_amendment:` が `discharge` または `transform:` である規範（対象とその種別を含む）、`content` か `deadline:` が変わって引き継がれる規範（F.6が求める報告。期限は厳密に比較し、期限が短くなる場合は、経過時間がそれを超える義務と禁止がどうなるかを述べます）、新しいバージョンにない `transform:` の対象、変更された `counts_as:` 規則です。各所見にはレベル（`error`、`warning`、`info`）があります。JSONは `old`、`new`、`contracts[]`（`id`、`findings[]`：`level`、`kind`、`text` と詳細）、`errors[]`、`ok` からなります。終了ステータスは、改正を妨げるものがなければ0、エラー（`transform:` の対象がない、新しいバージョンが不正）があれば1、使い方の誤りは2です。

`examples/traces/` には `examples/norms_robot_delivery.cadl` 用のトレースが五つあります。すべての規範が履行される場合（a）、応答の不履行を障害報告で修復する場合（b）、両方とも不履行の場合（c）、配車係の停止により遅配が免責される場合（d）、再割当てにより義務が解除される場合（e）です。`norms_amendment_v1_to_v2.jsonl` は、`examples/norms_amendment_v1.cadl` のトレースで、障害報告の期限内に `examples/norms_amendment_v2.cadl` へ改正されます。

`examples/norms_no_double_assignment.cadl` は、この契約に、ロボットについて限量した禁止を加えたものです。同じ配送を同時に2台のロボットに割り当ててはなりません（`exists j in ROBOT: exists k in ROBOT: j != k AND ROBOT[j].delivery == request.id AND ROBOT[k].delivery == request.id`）。トレースは、二重割当てがない場合（a、履行）、t=3に `ROBOT[3]` にも同じ配送が割り当てられる場合（b、t=3に違反）、同じロボットへの再割当て（c、履行）です。これらのトレースでは `N` に値がないので、定義域は開いています。名指された2台のロボットが配送を持てば内容は真になり、そうでない間は未定義で、違反にはなりません。
### 規範の設計時の検査（モデル検査）

`cadl norms-check FILE [--contract ID] [--bindings i=2] [--members ROBOT=2] [--step 1s] [--horizon N] [--export DIR] [--domain Q=lo..hi] [--instance i=n ... | --instances N] [--starts any|together] [--version FILE ...] [--timeout SECONDS] [--require-checker] [--format text|json]` は、1つの契約インスタンスの規範をPRISM言語のマルコフ決定過程に変換し（付録F、F.12）、トレースがまだない段階で、インスタンスのすべての振る舞いについてPRISMに問います。環境は、各観測で、どのメッセージ事象が起こるか、各量の値のクラス（述語のリテラルが定義域を分けるクラスごとに1つの値と「値なし」）、インスタンスがそこで終わるかどうかを選びます。観測は `--step` ごと（既定は期限と区間の最大公約数）、最大で `--horizon` + 1個（既定は期限と区間から決めます）です。ステップごとに観測のあるトレースについて、モデルは厳密です。値のクラスは、トレースが与えうるすべてのJSONの値（値なし、真理値、数、テキスト、リストとオブジェクト）を覆います。述語はどれも代表値について `cadl norms-eval` の評価器で評価するので、型の異なる値の比較（テキストと数、真理値と数）も、モデルでトレースと同じ値になります。

```bash
cadl norms-check examples/norms_check_conflict.cadl --bindings i=1
#  report_status / radio_silence: after 1 observation(s) keeping one violates the other
#  apologise can be activated in a state from which every continuation violates it
#  the power audit is never exercised
```

変換した規範ごとに、発効しうるか、違反されうるか、履行されうるか（インスタンスはどの観測でも終わりえます）、上限まで続くどのインスタンスでも違反されるか、発効するたびに守れるかを報告します。義務と禁止の組ごとに衝突するか（それぞれ単独ではまだ守れるのに、どの続きもどちらかに違反する状態に到達できるか）、権能ごとに行使されうるか、修復が発効しうるかも報告します。「可能」という答えには必ず証拠が付きます。証拠は `cadl norms-eval` の形式のトレースで、このコマンド自身が `cadl norms-eval` のエンジンで評価し、確認済みと示します（衝突には、それぞれの規範を守る2つの証拠があります）。`--export DIR` は、モデル（`<contract>.prism`）、性質（`-const early_end=true` で使う `<contract>_any.props` と、`early_end=false` で使う `<contract>_full.props`）、証拠（`witness_<norm>_<question>.jsonl`）を書き出します。JSONは `instance`、`model`、`norms[]`（問いごとに `answer` と、`trace`、`choices`、`confirmed` を持つ `witness`）、`conflicts[]`、`findings[]`、`unconfirmed[]`、`assumptions[]`、`rules[]`、`notes[]` を持ちます。

変換するもの：メッセージ事象、counts-as規則、量をリテラルまたは `count` と比べる述語（`==`、`!=`、`<`、`<=`、`>`、`>=`、`IN`、単独の量）と `AND`、`OR`、`NOT`、限量子 `exists` と `for all`、集約 `count`、`min`、`max`、2つの量の比較（`ROBOT[1].load == ROBOT[2].load`。クラスは、まとまり全体のリテラル、すべての順序を取れるだけのリテラルの間の値、比べられるリストと比べられないリストのすべての組合せを実現するリストを持ちます）、定義域のない線形の算術、`sum`、`avg`（どの積もリテラルの因数を持ち、どの除数もリテラルであるもの。`sum(r.load for r in ROBOT) + 2 * DEPOT.reserve > DEPOT.capacity` など。数には、比較の線形の配置の各面から有限小数の点を1つずつ取ります。これはどのトレースについても厳密です。`3 * x == 1` のように有限小数を含まない面は除きます。JSONはこれらの量を `linear` に示します）、それ以外の算術、`sum`、`avg`（`--domain Q=lo..hi` で与えた整数の定義域の上で。たとえば `--domain "ROBOT[*].load=0..3"`。答えは、それらの量の数が定義域の整数であるトレースについて成り立ち、報告はそのことを示します。`--domain` がなければ、述語のリテラルのうち最小のものより1小さい整数から最大のものより1大きい整数までを定義域と仮定し、報告はそれを仮定した定義域として示します。JSONでは `assumed_domains` です。答えがそれに依存する規範は、JSONで `assumed_domains` を持ち、テキストでは `*` が付き、その規範についての指摘には「only under the assumed domain」が付きます）、ワイルドカード参照と範囲参照（`ROBOT[*].battery > 5`）。これらはインスタンスの要素（`--members`）の上で展開します。`N` のような記号の上限は最後の要素に束縛するので、定義域は閉じています。さらに、1つの `within` / `throughout` / `until` からなる内容、期限、`until:`、権能とその効果、修復、これらの形の `assume:` の項目も変換します。変換しないもの（理由を報告します）：`request`、`now`、`state`、変換しない規範に依存するすべての規範。1,000を超える観測、16を超えるメッセージ事象、観測あたり8,192を超える選択、述語の1つのまとまりについて50,000を超える値のクラスの組合せ、互いに大小を比べる（`<`、`<=`、`>`、`>=`）4個以上の量は拒否します。トレースがそれらにリストを与えることがあり、リストはいつも比べられるわけではありません（`[1, "a"] < [1, 2]` は未定義です）。さらに、リストを除いても、このようなまとまりのどの量も少なくとも15個のクラス（値なし、真、偽、8個のテキスト、4個のオブジェクト）を持つので、組合せは少なくとも15<sup>4</sup> = 50,625個になり、制限を超えます。拒否のメッセージはこれらの数を示します。`examples/norms_check_linear.cadl`（`--members ROBOT=2`）は、線形の算術、どの有限小数も満たさない比較、定義域を仮定する2つの量の積を含みます。ある観測の前の状態で、ある選択が規範を発効させ、発効させる選択がどれも、どの続きも規範に違反する状態に至るとき、その発効は守れません（期限0秒の修復がこれに当たります。メッセージを発動条件とする期限0秒の義務は、同じ観測で内容が起こりうるので守れます）。その証拠は、その観測でのすべての選択と、発効させる各選択にメッセージも値もない観測を続けたものを `cadl norms-eval` で確かめ、残りがモデル検査器の答えによることを報告に示します。時間は、モデルのコマンドの数（選択ごとに4つ）と到達可能な状態の数とともに増えます。メッセージ事象が12個（選択が4,096）の場合は10分を超えました。`--timeout SECONDS` は、モデルを作る時間とPRISMのすべての実行の時間の合計を制限し（既定は600）、超えたときは終了ステータス2で終わります。PRISMは4g、または物理メモリの半分がそれより小さければその大きさのJavaヒープで動きます（`CADL_PRISM_JAVAMAXMEM=8g` で変えられます）。`--export` はPRISMを実行する前にモデルを書き出します。終了ステータスは、指摘がなければ0（PATH上にPRISMがない場合も0です。報告は何も検査しなかったことを示し、`--export` はモデルを書き出します。`--require-checker` を指定すると、PRISMがないときは2です）、指摘（衝突、一度も発効しない規範やどのインスタンスでも違反される規範、守れない発効、行使されない権能）があれば1、使い方の誤り、拒否、時間切れは2、証拠が `cadl norms-eval` で確認されなければ3（norms-checkの誤り）、内部の誤りは4です。

**複数のインスタンス。** `--instance i=1 --instance i=2`（または、開いた1つの添字を集合の最初の要素に束縛する `--instances 2`）は、契約のインスタンスを同じ観測の上でまとめて検査します。インスタンスはメッセージ事象と量を共有し、それぞれの規範、仮定、開始、終了を持ちます。各インスタンスが始まる観測は環境が選び、順序も問いません（`--starts any`、既定）。インスタンスは開始の前と終了の後には変わらず、そのトレースは、共有する観測の時刻で、開始から終了までの観測からなります。`--starts together` を指定すると、これまでどおり、すべてのインスタンスが最初の観測で始まり、コマンドが少なくなります（選択ごとに開始と終了の組合せが2<sup>n</sup>通りで、`any` では4<sup>n</sup>通りです）。0.3.28から `--starts any` が既定なので、0.3.27で受け付けた `--instances` の実行が、コマンドが多すぎるために拒否されることがあります。その場合は `--starts together` で、これまでどおり検査できます。上限は共有するので、観測sで始まるインスタンスの観測は高々H − s + 1個です（報告はそのことを示します。より大きい `--horizon` で長いインスタンスを扱えます）。規範は `id@k` と呼び、衝突はインスタンスをまたぐすべての組について問います（あるインスタンスで担い手が義務づけられることを別のインスタンスが禁じる場合。`examples/norms_check_instances.cadl` を `--members ROBOT=2 --instances 2` で検査します）。証拠は各インスタンスのトレースを持ちます（`--export` では `witness_..._instance<k>.jsonl`）。異なる観測で始まるインスタンスでなければ起こらない衝突もあります。`examples/norms_check_starts.cadl`（`--members ROBOT=2 --instances 2`）では、あるインスタンスの修復がゲートにアクセスの許可を禁じている間に、後から始まった別のインスタンスがゲートに許可を義務づけます。`--starts together` ではこの衝突はありません。インスタンスは4個までです。

**改正。** `--version FILE`（一連の改正では繰り返します）を指定すると、環境は、どの観測でも、契約を次のバージョンに改正できます。改正は、F.6と `cadl norms-eval` の適用のとおり、その観測のほかの手順の前に効力を持ちます（`on_amendment:` の `carry_over`（経過時間と、内容が変わらない場合は判定を待つ候補を保つ）、`discharge`、`transform:`、取り除かれる規範と加わる規範、続く仮定、新たに始まる仮定、閉じる仮定）。問いは、どのバージョンのすべての規範idについても改正をまたいで問います（JSONは各規範のバージョンを示します）。改正を必要とする証拠は `amend` の行でバージョンのファイルを名指すので、`cadl norms-eval` はそれを改正されたインスタンスのトレースとして読みます。アクターやライフサイクルが異なるバージョン、種類が変わる規範、いずれかのバージョンで変換しない規範やcounts-as規則は拒否します。複数のインスタンス（`--instances N --version FILE`）では、1回の改正は、その観測で動くすべてのインスタンスに同じ観測で効力を持ちます。これは、`cadl norms-eval` が各トレースの `amend` の行を適用するのと同じです。終わったインスタンスは改正しません。改正の後に始まるインスタンスは新しいバージョンのインスタンスです（その証拠のトレースはそのファイルで評価し、JSONは `files` でそのファイルを示します）。`cadl norms-check examples/norms_check_amend_v1.cadl --members ROBOT=2 --instances 2 --version examples/norms_check_amend_v2.cadl` は、改正の時点で判定を待つあるロボットの要求が、バージョン2が加える別のロボットのインスタンスの外出禁止と衝突することを示します。`cadl norms-check examples/norms_amendment_v1.cadl --bindings i=2 --version examples/norms_amendment_v2.cadl` は、2番目のバージョンの規範 `request_help` が改正の後に発効し、違反されうることを示します（コマンドの数がバージョンの数だけ増えるので、数分かかります）。

変換は `cadl norms-eval` と照合して試験しています。テストは、生成したPRISMのテキストを、それが使う部分を読む解釈器で、環境の選択のランダムな経路と、1つの契約について観測が2個以下のすべての経路について実行し、各観測の後の各規範の状態、判定、権能の行使、修復の発効を比べます（`CADL_NORMS_CHECK_PATHS` でランダムな経路の数を増やせます）。解釈器はPRISMのシミュレータが生成した経路と比べ、PRISMの答えは到達可能な状態についての明示的な不動点計算と比べています。

### 生成したPythonコードの規範モニタ

`norms: 0.1` を宣言したファイルに対して、`cadl codegen -t python` は `norms_monitor.py` も出力します。一つの契約インスタンスをオンラインで監視するモニタです（付録F、F.5）。動作中のシステムが観測を一つずつ渡すと、規範の状態と四値の判定の変化を返します。`contracts.py` から外される、時相演算子を含む `assume:` / `guarantee:` の項目も判定します。

```python
from robot_delivery_norms.norms_monitor import NormsMonitor   # 生成したパッケージ

m = NormsMonitor("DELIVERY_SLA", bindings={"i": 2})
m.observe({"t": 0.0, "values": {"DISPATCHER.is_operational": True, "ROBOT[2].status": "Idle"}})
m.observe({"t": 1.0, "state": "Assigned",
           "messages": [{"from": "DISPATCHER", "to": "ROBOT[2]", "name": "route_assignment"}]})
# -> [{"t": 1.0, "kind": "norm", "id": "acknowledge", "state": "active", "verdict": "pending",
#      "previous_state": "inactive", "previous_verdict": "pending", "reason": "trigger occurred: ..."}]
m.next_deadline()        # 6.0：acknowledge の期限は有効化の5秒後
m.next_check()           # 6.000000002：それを検出できる最初の時刻印
m.observe({"t": 6.5})    # -> acknowledge が violated、report_failure が active
m.verdicts()             # すべての規範と項目の現在の状態と判定
m.end()                  # インスタンスの終了：規範を確定する
m.result()               # すべて。キーは `cadl norms-eval --format json` と同じ
```

| 呼出し | 意味 |
|---|---|
| `NormsMonitor(contract=None, bindings=None, request=None, quantities=None, versions=None)` | 一つの契約インスタンスのモニタ。`norms:` か `counts_as:` を持つ契約が一つなら `contract` は省略できます |
| `observe(observation)` | 観測を一つ処理します。観測は `cadl norms-eval` のトレース1行と同じキーの辞書です（`t` は必須。`values`、`messages`、`state`、`end`。最初の観測には `bindings` と `request` も書けます）。この手順での変化を返します |
| `end()` | 最後に受け取った観測でインスタンスを終了し、規範を確定します（F.5の項目6）。`"end": True` か終端の `state` を持つ観測でも終了します |
| `verdicts()` | `norms`（idごとの `state` と `verdict`）、`assumptions`（`status`）、`guarantees`（`verdict`）、`ok` |
| `result()` | それまでの観測に対する `cadl norms-eval --format json` の結果（`file` を除く）。終了前は接頭辞に対する結果です |
| `next_deadline()` | 有効な義務の期限、または有効な禁止の期間が終わる最も早い時刻。なければ `None`。ちょうどこの時刻の観測はまだ期限内です |
| `next_check()` | 時間の経過だけを待っているものが、観測によって確実に決まる最も早い時刻印。上の期限のほか、区間が終わった時相演算子（入れ子のもの、規範の内容の中のものも含む）が対象です。待っているものがなければ `None` |
| `started`、`ended`、`contract` | モニタの状態 |

変化は辞書です。規範では `t`、`kind: "norm"`、`id`、`state`、`verdict`、`previous_state`、`previous_verdict`、`reason`（規範と手順ごとに一つで、その手順の後の状態を示します）。項目では `t`、`kind: "guarantee"` または `"assumption"`、`index`、`text`、`verdict`（前提では `status`：`holds`、`failed`）、`previous`、`reason`。

**実装は一つ。** `norms_monitor.py` はcadlをインポートしません。Python 3.9以降の標準ライブラリだけで動き、このファイルだけをコピーして使えます。中身は、ファイルで宣言されたアクターと契約をデータにしたものと、`cadl norms-eval` のエンジン（`cadl/norms_eval.py` の `monitor-runtime` の印の間）とそれが使うASTクラスや補助関数をそのまま写したものです。エンジンがcadlの構文解析器を呼ぶ箇所では、生成時に構文解析器が返した結果を表から引きます。生成器は、モジュールを書き出す前にコンパイルし、使っているグローバルな名前がすべてモジュールの中で定義されていることを検査します。写したコードが写していないものを必要とする場合、`cadl codegen` はその旨のメッセージを出して失敗し、何も書き出しません。生成したパッケージはcadlのない環境でもインポートできます。ほかのモジュールは従来どおり `cadl.codegen.runtime_support` を必要とし、その名前を使った時点でインポートエラーになります。記録済みのトレースは次のように再生します。

```bash
python -m robot_delivery_norms.norms_monitor TRACE.jsonl [--contract ID] [--format json|text]
```

同じファイルとトレースに対して `cadl norms-eval` が出力するもの（JSONではキー `file` を除く）を、同じ終了ステータスで出力します。テストでは、cadlをインポートできないプロセスでモニタを動かし、サンプルのトレース、`tests/test_norms_eval.py` のすべてのケース、1,500本のランダムなトレース（アクターの範囲、範囲外のメンバー、宣言されていないアクターを含む）について両者を比較しています。

**オンラインのモニタに分かること。** モニタは、それまでに受け取った観測だけで各観測を判定します。したがって、ある観測の後の `result()` は、量の名前が同じであれば（三つ目の点）、そこまでのトレースを接頭辞（`"end": false`）として `cadl norms-eval` に与えた結果と一致します。トレース全体を読む場合と違う点は三つです。

- 期限切れと、区間が終わったことで偽（`within`、`until`）または真（`throughout`）に決まる時相演算子は、その時刻の後の最初の観測で検出します。違反が `violated` か `excused` かもその時点で決まります。報告することがないホストは、その時刻になったら、時刻だけの観測 `{"t": m.next_check()}` を送ってください。`next_check()` は、十進数の値が期限や区間の終わりより後になる最小のfloatです。時刻は正確に比べ、期限ちょうどの内容は期限内とするからです。期限がなく、内容に時相演算子を含む規範は観測のたびに評価するので、観測のたびに新しい確認時刻が生じます。
- インスタンスは「最後の行」では終了しません。`end()` を呼ぶか、`"end": True` または終端の `state` を持つ観測を送ってください。終了後の観測は何の効果も持ちません。形式だけを検査し、その数を `result()` の警告に示し、内容は保持しません。
- アクターを付けずに書いた名前（`ROBOT[1].load` ではなく `load`）は、まだ値がない間、`Delivered` のような記号値と区別できません。`cadl norms-eval` は、量の名前を、トレースの中のインスタンスの観測から取ります。モニタは `quantities=[...]` から取り、そこにない名前は最初の値が届いた後に限って量として扱います。判定が違いうるのはこの点だけで、そのような名前を最初の値の前に文字列の値と比較する場合に限ります（`mode == target` で、`target` の値が後から届く場合）。`quantities=` で名前を与えてください。値のない名前についての警告は、最初に出会った時点で出します。`lifecycle:` のない契約に対する `state` は、届いた時点で警告します。後の観測が契約を改正する場合も同じです（`cadl norms-eval` はその場合、警告しません）。

拒否した観測はモニタをまったく変化させず、モニタはその後も使えます。`observe` は、形式の誤った観測（型の誤り、時刻の逆行、有限でない時刻や絶対値が2**53を超える時刻、`NAME` や `NAME[2]` の形でないアクター、ライフサイクルにない状態）には `TraceError` を、適用できない改正には `ObservationError`（`MonitorError` の一種）を送出します。モニタは渡されたものをコピーして保持します。モニタは自分のインスタンスの観測を保持します（時相演算子と「真になる」の判定に必要です）。契約インスタンスごとにモニタを一つ作り、終了後に捨ててください。スレッドセーフではありません。`cadl norms-eval` と同じく、ライフサイクルの状態は観測から読み、違反が要求する `transition:` は報告するだけで適用しません。

**改正。** `"amend": "NAME"` を持つ観測には新しいバージョンのモデルが必要です。新しいファイルからもモニタを生成し、その `MODEL` を `NormsMonitor(..., versions={"NAME": new_package.norms_monitor.MODEL})` として渡します。コマンドラインでは `--model-version NAME=MODEL.json` で、このファイルは `python -m NEW_PACKAGE.norms_monitor --dump-model` で書き出します。`versions` の値がそのようなモデルでなければ、コンストラクタが拒否します。モニタが持たない版、その契約を含まない版、現在の状態がライフサイクルにない版を指す観測は拒否します（`ObservationError`）。モニタは構文解析器を持たないので、実行時に `.cadl` ファイルを読むことはできません。`amend` を持つ観測では、新しいバージョンで新しく加わった規範を `previous_state` が `None` の変化として、削除された規範を `state` が `None` の変化として報告します。項目の `index` は現在の版での位置なので、改正の前後で項目を対応づけるには `text` を使ってください。

**ファイル中の文字列と数値。** モニタが `.cadl` ファイルから受け取るものはすべて `MODEL` にあります。`MODEL` は一つのJSONテキスト（ASCIIのみで、NaNやInfinityを含まない）で、Pythonの文字列リテラル一つに収め、`json.loads` で読みます。コードに文字列として埋め込むものはありません。有限でない数を含むモデルは生成しません（`cadl codegen` がその場所を報告し、何も書き出しません）。`1e999` のように桁あふれする数を含む述語は、`cadl` ではそもそも述語として扱わず、テキストのまま保持します。`cadl codegen` の入力検査は従来どおりファイル全体に適用するので、引用符、バックスラッシュ、制御文字を含む文字列は、何も生成しないうちに拒否します。

### 生成したRegoとSolidityの規範モニタ

`norms: 0.1` を宣言したファイルに対して、`cadl codegen -t opa` は契約ごとに `norms_monitor_<contract>.rego` を、`cadl codegen -t solidity` は `<Contract>NormsMonitor.sol` を出力します（仕様書の付録DとF.5）。規範、counts-as規則、`assume:` / `guarantee:` の項目を監視し、契約のポリシー（`<contract>.rego`、`<Contract>.sol`）が外す `within`・`throughout`・`until` を含む項目も判定します。各ステップは `cadl norms-eval` が実装するF.5の順序に従います。テストは、観測ごとに両者を `cadl norms-eval` のエンジンと比較します。

**扱える範囲。** メッセージイベント（`[*]`・範囲・束縛した添字を持つアクターの指定、引数）、制度的イベント、F.5の三値の読み方による述語（アクターを書かない名前、`A[i].x`、`request.x`、`now`、`state`、比較、`+ - * /`、`IN`、`AND`、`OR`、`NOT`）、トリガ、`until:`、期限、回復義務、権能（`activate`、`suspend`、`resume`、`discharge`）、`if:` を持つcounts-as規則、入れ子を含む時相演算子を扱います。扱わないのは、述語の量化子、集約、ワイルドカードと範囲の参照、式の添字（`A[i+1]`）、改正（F.6。`amend` を持つ観測は拒否します）です。規範、counts-as規則、`assume:` の項目でこれらを使う契約にはモニタを生成せず、`cadl codegen` がその理由を警告で示します（ほかのファイルは生成します。そのような契約はpythonターゲットが監視します）。これらを使う `guarantee:` の項目は、ほかに影響しないので、警告を出したうえでモニタから外します。Solidityのモニタはさらに、`*` と `/`、小数点以下が6桁を超える数、時相演算子の中の時相演算子（区間を観測が届くたびに一度だけ走査するため）、規範の内容に含まれる三つ以上の時相演算子、生成時のコードがEIP-3860の49152バイトを超えるモニタ（生成したコンストラクタから見積もります）を拒否します。`guarantee:` の項目でこれらを使う場合は、その項目だけを外します。

**Rego：ステップ関数。** OPAは状態を持たないので、ポリシーは直前の状態と一つの観測からモニタの次の状態を計算します。

```bash
echo '{"state": null, "observation": {"t": 0, "bindings": {"i": 2}}}' > input.json
opa eval -d norms_monitor_delivery_sla.rego -i input.json 'data.cadl.norms_monitor.delivery_sla.result'
```

入力は `{"state": S, "observation": O}`（最初の観測では `S` はnullか省略。`O` は `cadl norms-eval` のトレースの1行）か、`{"state": S, "finish": true}`（最後の観測でインスタンスを終える。Pythonモニタの `end()`）です。最初の観測と並べて `bindings`、`request`、`quantities` を渡せます。`result` は `state`（ホストが保存し、次の観測とともに渡します）、`changes`（Pythonモニタの `observe()` と同じ形。`reason` はありません）、`violations`、`institutional_events`、`powers_exercised`、`unauthorised`、`verdicts`、`ended`、`next_deadline` を持ちます。拒否した観測では `error` と変わらない `state` を返します。状態は普通のJSONです（キーは付録Dにあります）。束縛、request、既知の名前、現在の値、ライフサイクルの状態、規範インスタンス（活性化、活性の時間、判定待ちの候補）、項目、時相演算子と判定待ちの候補がまだ読む観測のバッファ（それより古い観測は捨てます）を持ちます。契約はポリシーの中に一つのJSONテキスト（`model`、`json.unmarshal` で読みます）として入り、述語はリテラルをJSON文字列で書いたRegoの関数になるので、`.cadl` ファイルのテキストがRegoのコードになることはありません。

**Solidity：状態を持つコントラクト。** コンストラクタは報告者のアドレスを受け取ります（`address(0)` ならデプロイした者）。`begin(bindings, request, quantities)`（省略可。最初の観測の前）、`observe(t, values, messages, hasState, state, end)`、`addQuantities(names)`、`finish()` でインスタンスを記録でき、これらを呼べるのは報告者だけです。`verdicts()`（`normIds()` の順）、`ok()`、`nextDeadline()`、`contractId()`、`observations()`、`lastT()`、`ended()`、`ignored()` とイベント `NormChanged`、`ItemChanged`、`Violation`、`InstitutionalEvent`、`PowerExercised`、`Unauthorised` で結果を返します。形の正しくない観測はrevertするので、コントラクトは元のままです。時刻 `t` を含む数は小数点以下6桁の固定小数点数です（`int256` に10**6を掛けた値）。`t` は引数なので記録したトレースを再生でき、運用では `block.timestamp * 10**6` を渡すこともできます。値は `(name, kind, n, s)` で、kindは0（null）、2（真偽値）、3（数）、4（文字列）です。ファイルには、基底コントラクト（コンストラクタがデータとして格納する契約を解釈するもの。テキストは16進リテラル）と、別にデプロイしてリンクする六つのライブラリ `CadlNormsValues`、`CadlNormsEval`、`CadlNormsWindows`、`CadlNormsMulti`、`CadlNormsItems`、`CadlNormsActs`（この順にデプロイします）が入ります。モデルと状態はストレージの一つの構造体にまとめ、ライブラリがそれを読み書きします。こうしてそれぞれの実行時コードをEIP-170の上限24 KB未満に収めています（`cadl codegen` は大きさを大きめに見積もり、上限を超える契約は拒否します。本体のコントラクトの見積もりは16 840バイトです）。観測一つにかかるガスは、それより前の観測の数とともには増えません。時相演算子の区間は一度だけ走査し、値は二分探索で探し、後のステップが読まない値は新しい値一つにつき数個ずつ削除します。テストでは、例の観測一つに、1秒に一つの観測を320個与えたとき、仮定の300秒の区間が開いている間もその後も約6.4 × 10^5のガスを使います。最も大きいテストの契約では、観測一つに3 × 10^4から3.3 × 10^6（中央値1.3 × 10^6）のガスを使います。内容に時相演算子を含む規範は、候補を列に並べ、観測が被演算子について示すこと（`within` の被演算子が最初に真になった時刻、`throughout` の被演算子が最初に偽または未定義になった時刻、`until` の `q` が最初に真に、`p` が最初に真でなくなった観測）からまとめて判定します。二分探索と観測ごとに数個の削除だけで済むので、ガスは区間の中の観測の数とともには増えません。1秒に一つの観測で内容が `throughout(1000s, …)` のとき、320個目の観測で4.4 × 10^5、1000個目で4.5 × 10^5です（0.3.25では320個目で1.3 × 10^7）。そのような内容のほかの部分は候補を作るときに決まるので、候補はすぐに成り立つか、作られないか、演算子の判定を待ちます（`q` が真なら `within(3s, p) OR q` はすぐに成り立ちます）。候補を作るときに、演算子を未決、真、偽としたときの内容を読むので、演算子が偽のときに限り成り立つ候補や、真でも偽でも成り立つ候補もあります。そのような候補は、否定した被演算子に対する双対の演算子の列に入れます（`NOT within(d, p)` は `throughout(d, NOT p)`、`NOT until(p, q, d)` は ¬p と ¬q のreleaseです。仕様の付録DのD.5を参照）。これで `NOT` の下、比較の中（`within(3s, p) == flag`）、`IN` の中の演算子を扱います。時相演算子を二つ含む内容（`within(…) == throughout(…)` を含む）は `CadlNormsMulti` で判定します。候補はストレージのセグメント木の葉であり、節点は二つの演算子の状態とこれから取りうる値の組ごとに、成り立つ候補と未決の候補があるかどうかを記録します。観測一つは演算子を候補の範囲ごとに決め、その計算量は候補の数の対数に比例します。木の節点は削除しないので、そのような規範が発効するたびに候補一つにつき3つ以上のスロットがストレージに増えて残ります。どのモニタも観測ごとに配列へ2つの要素（時刻とライフサイクルの状態）を加えます。観測一つのガスはこれらによって増えません。ここでのガスの値は、1回の観測に値が一つの場合の測定です（値が二つなら約2割多くなります）。1秒に一つの観測で1000秒の区間のとき、`NOT until(…)` は320個目と1000個目の観測で5.2 × 10^5、`throughout(1000s, …) == true` は5.1 × 10^5、`within(1000s, …) OR throughout(1000s, …)` はどちらでも5.9 × 10^5のガスを使います。三つ以上の時相演算子を含む内容は拒否します。観測のガスがブロックのガス上限を超えるモニタは、その観測を記録できず、元に戻りません。報告者の鍵を失った場合も同じです。時相演算子の中で読むアクターを付けない名前が量であるとき（`throughout(10s, A.s == flag)` の `flag`）は、`begin` で与えるか最初の観測で観測してください。走査は読んだ値を保持するので、後からその名前を既知にする `observe` と `addQuantities` はrevertします。コンパイルにはsolc 0.8.24から0.8.37（確かめた範囲。pragmaは `>=0.8.24 <=0.8.37`）と `--optimize --optimize-runs 1` を使います（ファイルの先頭にも書いてあります。オプティマイザを使わないと実行時コードがEIP-170の上限を超えます）。

**`cadl norms-eval` との違い。** Pythonモニタと同じく、期限切れは期限の後の最初の観測で検出し、インスタンスは `finish` かインスタンスを終える観測で終わり、アクターを書かない量の名前は `quantities` とそれまでの観測から得ます。数と時刻は、`cadl norms-eval` と同じく三つとも正確に扱います。Regoのモニタはすべての数を書かれたとおりの十進数として読み（状態では `{"$q": [n, k]}`、つまり n / 10^k として持ちます）、`/`（商は十進数になるとは限りません）、小数点以下が4095桁を超えうる積、`cadl norms-eval` の範囲（上記）を超える数を拒否します。Solidityのモニタは小数点以下6桁を持ち、`*`、`/`、それより多い桁を拒否します。これらの制限を満たす入力では `cadl norms-eval` と一致します。束縛は小数部を持たない整数（`1.0` は拒否します）か文字列、`values` の値はnull・真偽値・数・文字列でなければなりません（リストは `request` でも拒否します。空白だけが違う `values` の二つのキーも拒否します）。Solidityのモニタは、requestを葉の形（`"a.b"`）で、値の名前をASCIIで受け取ります。Regoのモニタは与えられた状態を信頼します（形式と契約だけを検査します）。ホストは保存した状態を改変されないように保ってください。`cadl norms-eval` の理由、警告、タイムラインは返しません。

テスト（`tests/test_norms_monitor_targets.py`）は、Regoを `opa eval` で、SolidityをEVM（eth-testerとpy-evm。`CADL_EVM_PYTHON` で指定する別のPython。solcは `CADL_SOLC`、PATH、`~/.solcx` から）で実行し、入っていないものは飛ばします。サンプルのトレース、`tests/test_norms_eval.py` のトレース（OPA）、ランダムなトレースについて、観測ごとに、各規範の状態と判定、項目、違反、制度的イベント、権限のない行為を比較します。ランダムなトレースの数は `CADL_NORMS_TARGET_TRACES` で、EVMで実行する `tests/test_norms_eval.py` のトレースの数は `CADL_NORMS_EVM_CASES` で変えられます（既定はすべて）。CIのPython 3.12のジョブは、EVMのvenv（eth-testerとpy-evm）を作り、solc 0.8.24をpy-solc-xで取得するので、そこではEVMのテストがそのトレースのうち40本で動きます。EVMのvenvのバージョンは `tests/evm-constraints.txt` で固定し、`CADL_REQUIRE_EVM=1`（CIで設定）ではEVMがないときに飛ばさず失敗とします。cadl自体はこれらに依存しません。文字列リテラルやコメントを終わらせる文字を含むファイルの文字列がデータのままであることも確かめます。`scripts/norms_monitor_mutations.py` は、Solidityのモニタの時相的な部分を変えた変異体を時相的な内容のトレースで実行し、`cadl norms-eval` と同じ判定になる変異体を報告します（14個の変異体のうち、該当するものはありません）。

### 制度パラメータの根拠付け（grounding 0.1）

根拠付け拡張（仕様書の付録J）は、契約に宣言した `alpha`・`beta`・`lambda` を、その契約の規範と関係づけます。ファイルは `grounding: 0.1` を、それが必要とする `norms: 0.1` とともに宣言し、次の四つのキーを使えます。

```yaml
contracts:
  - id: DELIVERY_SLA
    parties: [DISPATCHER, "ROBOT[*]", "CUSTOMER[*]"]
    authority:   { decision_holder: DISPATCHER, beta: 0.9 }
    information:
      alpha: 0.6
      sharing: ["DISPATCHER -> ROBOT[*] : route"]
    incentives:  { lambda: 0.5 }
    grounding:
      items: { route: DISPATCHER, battery: "ROBOT[*]" }   # 情報項目: その出所
      tolerance: 0.25                                      # 既定値 0.25
    norms:
      - id: may_return_to_charge
        kind: permission
        bearer: "ROBOT[i]"
        content: "ROBOT[i] -> DISPATCHER : charging_request"
        shares: [battery]                                  # このメッセージが伝える項目
      - id: no_low_battery_driving
        kind: prohibition
        bearer: "ROBOT[i]"
        content: "ROBOT[i].battery < 10 AND ROBOT[i].status == InTransit"
        on_violation: { sanction: "removed from the pool", magnitude: 4 }
        on_fulfilment: { reward: "bonus", magnitude: 1 }
```

`cadl norms-indices FILE [--contract ID] [--format text|json]` は、契約ごとに三つの量を計算します。どれも二つの個数の正確な比で、分母が0のときは未定義です（J.4）。

| 量 | 比 | あわせて報告するもの |
|---|---|---|
| **alpha^**（情報共有） | 共有エントリ・義務・許可のいずれかが当事者全体に与える対（項目、出所以外の当事者）の数 / そのような対の総数 | 三つの与え方ごとの対の数、与えられていない対、宛先が特定のメンバー（`ROBOT[3]`）であるために対を与えないエントリと規範 |
| **beta^**（権限の集中） | 一つの単一アクターが担う権限の最大数 / 権限の数 | 担い手ごとの権限の数、各権限が `effects:` に挙げる規範の数、`decision_holder` の占める割合 |
| **chi**（インセンティブ被覆率） | 制裁の文言、報酬の文言、0より大きい大きさのいずれかを持つ義務と禁止の数 / 義務と禁止の数 | 修復だけを持つ規範、`transition:` を持つ規範 |

```console
$ cadl norms-indices examples/grounding_robot_delivery.cadl
Contract DELIVERY_SLA (tolerance 0.25)
  alpha^ (information sharing) = 5/8 = 0.625  given pairs / pairs
    declared alpha: 0.6, declared - index = -0.025
    pairs given by a sharing entry: 3, by an obligation: 1, by a permission: 1
    ...
  beta^ (concentration of powers) = 1/2 = 0.5  largest number of powers of one single actor / powers
    declared beta: 0.9, declared - index = 0.4
    ...
  chi (incentive coverage) = 1/3 = 0.333333  norms that carry an incentive / obligations and prohibitions
    declared lambda: 0.5 (a scale; not compared with chi)
    ...
  warning  contract 'DELIVERY_SLA' declares beta: 0.9, but the index of concentration of powers is 1/2 = 0.5 ...; they differ by 0.4, more than the tolerance 0.25 (J-7)
```

JSONには `file`、`grounding`、`contracts[]`（`id`、`items`、`tolerance` と、`alpha`・`beta`・`coverage`。それぞれ `defined`、`numerator`、`denominator`、`exact`、`value`、または理由つきの `undefined` と、上の個数を持ちます。ほかに `diagnostics[]`）、`designs[]`（J-9の診断）があります。

**SoS。** 2つ以上の契約を持つファイルでは（`--contract` を付けないとき）、最後の節 `SoS: all N contracts` とJSONのキー `sos` が、同じ3つの量をすべての契約の上で示します（J.4の「SoSの量」）。**alpha^_SoS** は、すべての契約の与えられた組 / その組です。項目は契約で修飾します（2つの契約で宣言した項目はそれぞれで組を与え、ある契約のエントリはほかの契約の組を与えません）。**beta^_SoS** は、1つの単一のアクターがすべての契約で合わせて負担する権能の最大数 / すべての権能です。**chi_SoS** は、インセンティブを伴う義務と禁止 / そのすべてです。`sos` は `contracts` と、上と同じ形で個数を合計した `alpha`・`beta`・`coverage` を持ちます（`bearers` と `single_actors` は負担者ごとの権能の合計です。`largest_bearers` は最大の数を持つ単一のアクターをすべて辞書順に示します。この節は契約の順序によりません）。宣言値とは比べません（v0.3はα・β・λを契約ごとにしか宣言しません）。重み付けもしません。alpha^_SoS と chi_SoS は各契約の値の間にあります。beta^_SoS は、各契約のbeta^を権能の数で重み付けした平均以下であり、そのどれよりも小さくなることがあります（ただ1つの権能をそれぞれXとYが負担する2つの契約では、1と1、SoSは1/2）。IRには `institution.grounding` として入ります（J.10）。各契約の出力は変わりません。終了ステータスは、指標を報告できれば警告の有無にかかわらず0、使い方の誤り（ファイルがない、型検査のエラーがある、`grounding: 0.1` と `norms: 0.1` を宣言していない）では2です。

導出指標は規範についての証拠であり、パラメータの意味そのものではありません。`cadl check` は宣言された `alpha` と `beta` を指標と比較し、食い違いを警告するだけです（「型検査の内容」の15）。`lambda` は尺度であり、数値とは比較しません。`norms: 0.1` を宣言していて新しいキーを使っていないファイルは、`grounding: 0.1` を宣言しても不正になりません。

**注釈にすぎない。** `shares:`、`on_fulfilment:`、大きさは判定を変えません。`cadl norms-eval`、`cadl agents-run`、生成した `norms_monitor.py` は、これらがあってもなくても同じ結果を返し、生成したモニタはこれらを持ちません。コード生成器もこれらを書き出さないので、報酬の文言に引用符があっても構いません。IR（`cadl sim-ir`）は、拡張を宣言したファイルについてこれらを持ちます。各契約に `grounding`（`items`、`tolerance`、`alpha`・`beta`・`coverage` の個数を持つ `indices`。未定義の量は `null`）が、各規範に `shares`、`on_violation_magnitude`、`on_fulfilment_reward`、`on_fulfilment_magnitude` が加わります（J.10）。契約が2つ以上あるときは、制度の層にSoSの量を持つ `grounding` が加わります。拡張を宣言していないファイルのIRは変わりません。

**二つの版。** `cadl norms-diff OLD NEW` は、`on_violation.magnitude`、`on_fulfilment:`、`shares:`、`grounding.items`、`grounding.tolerance` の変更と、変化した指標、新しい版で追加された規範の大きさ・`on_fulfilment:`・`shares:` も報告します。片方の版だけが拡張を宣言している場合は、指標を比較せず、その旨を報告します。宣言された `alpha` または `beta` と、その指標が二つの版の間で逆向きに動くときは警告します（J-13）。たとえば、改正でロボットに権限を与えながら `beta` を上げる場合です。

**戦略モデル向け。** 戦略モデル（付録I。後述の「戦略モデル」を参照）では、利得の項に重みを書く代わりに規範を名指せます：`{ reward: shortcuts, sanction_of: "DELIVERY.no_shortcut" }`。その項の重みは −m · ℓ（`reward_of:` では +m′ · ℓ）です。m は規範の `on_violation.magnitude`（m′ は `on_fulfilment.magnitude`）、ℓ は `from: "C.lambda"` で束縛したパラメータです（J.7）。`cadl.grounding.norm_payoff_link(sos, "C.n", "sanction_of" | "reward_of")` は参照を解決し、大きさ（正確な値）、担い手、出所の名前 `C.lambda`、または参照を使えない理由の説明を返します。例は `examples/games_grounding_robot_delivery.cadl` です。

### 確率モデル（PRISMへの出力）

`extensions:` で `stochastic: 0.1` を宣言したファイルには、`model:` ブロック（仕様書の付録H）を書けます。これは、有限の状態変数の上のガード付き確率コマンドで書くパラメトリック離散時間マルコフ連鎖で、パラメータを契約の `alpha`・`beta`・`lambda` に束縛できます。`transitions:` の各運用モードが、固有のパラメータ値とコマンドを持つ一つの「設計」になります。例は `examples/stochastic_robot_delivery.cadl` にあります。

```bash
# 設計ごとにPRISMのモデルファイルと性質ファイルを一つずつ出力：
#   <SoS名>_<設計名>.prism と <SoS名>_<設計名>.props
cadl model-export examples/stochastic_robot_delivery.cadl --output prism_out/
cadl model-export examples/stochastic_robot_delivery.cadl --output prism_out/ --regime CENTRALIZED

# 各設計について、固定されたパラメータ値で model.properties を評価
cadl model-check examples/stochastic_robot_delivery.cadl
cadl model-check examples/stochastic_robot_delivery.cadl --regime CENTRALIZED --set p_fail=0.2 --set p_loss=1/6 --format json

# 運用モードマップの問い合わせを、一様な格子の格子点で評価
cadl model-check examples/stochastic_robot_delivery.cadl --regime-map best_for_delivery --grid 5

# 問い合わせの保証付きの運用モードマップを計算（セルをパラメータリフティングで判定）
cadl model-check examples/stochastic_robot_delivery.cadl --regime-map best_for_delivery --certify
cadl model-check examples/stochastic_robot_delivery.cadl --regime-map best_for_delivery --certify --max-cells 500 --format json
```

- `cadl model-export` は付録H.11の出力規則に従います。運用モードマップの `over:` に挙げたパラメータは未定義の定数（`const double p;`）として出力するので、出力したファイルをPRISMやStormに渡して、モデル検査・領域検証・パラメータ合成に使えます。PRISMのキーワードや組込み関数名（`floor`・`ceil`・`round`・`pow`・`mod`・`log`）と同じ名前には末尾に下線を一つ付けます。その結果の名前の衝突はエラーです。
- `cadl` は外部のモデル検査器を実行しません。`--certify` を付けない `cadl model-check` は、出力の確認とテストのための小さな**内部の参照評価器**です。すべてのパラメータを固定した一つの設計のマルコフ連鎖を列挙によって作り、`P` と `F`・`F<=k`・`U`・`U<=k` の組合せ、および `R{"r"}` と `F` の組合せを、厳密な有理数演算で評価します。値を報告し、しきい値のある性質には `satisfied` か `violated` を付けます。終了ステータスは、評価したしきい値付きの性質がすべて満たされれば0（問い合わせは数えません）、一つでも満たされなければ1、評価できなかった場合（使い方の誤りや、解釈されるモデルのないファイル）は2です。
- `--regime-map ID --grid N` は、標本点ごとに、比較する各運用モードの目的関数の値と、その点で最良の運用モードを表示します。これは**標本点での推定であり、保証付きの運用モードマップではありません**。セルの判定は一つも行わず、終了ステータスは0です。
- `--regime-map ID --certify` は、付録H.9の**保証付きの運用モードマップ**を計算します。結果は、領域を覆うセル（開いたパラメータの箱）の集合です。各セルは、`decided`（`best` に挙げた運用モードがセル内のすべての値で最適）、`infeasible`（セル内のどの値でも、制約を満たす運用モードがない）、`undecided` のいずれかです。手法は、厳密な有理数演算による**パラメータリフティング**で、外部のツールを使わず `cadl` の中で計算します。セルごとに、各設計をマルコフ決定過程に緩和します。この過程では、各状態が、そこから出る確率に現れるパラメータについて、セルの頂点の一つを選びます。この過程の最小値と最大値は、セル全体での設計の値の下界と上界です。ある運用モードがセル全体で制約を満たし、ほかのどの運用モードも、セル全体で制約に違反するか、どの値でもそれより良くないことを、上下界が示すとき、セルは `decided` です。そうでなければセルを二分し、未判定のセルを大きい順に分割して、未判定の体積の割合が `resolution:` 以下になるまで続けます。`--max-depth N`（既定値24）は一つのセルを二分する回数の上限、`--max-cells N`（既定値20000）は分割のセル数の上限です。上限のために細分が先に止まった場合も、マップを出力し、分解能に達していないことを示します。出力は、手法、セルの数、未判定の割合（厳密な値と丸めた値）、および各セルの箱・状態・`best` です。同じ判定の二つの半分は一つにまとめます。終了ステータスは、分解能に達すれば0、達しなければ1、エラーは2です。`--certify` は `--grid` や `--regime` と同時には使えません。`--set` で、マップで開いていないパラメータを固定できます。この例のマップは95セル（`decided` が60、`undecided` が35、未判定の割合は51/1024）で、計算は約0.1秒です。
- 保証付きのマップの限界：設計どうしの値の差ではなく、設計ごとの上下界を比べるので、境界に近いセルは、境界が通っていなくても `undecided` のまま残ります。セル全体で同点の運用モードを並べて挙げるのは、上下界がそれを示す場合（等しい定数の上下界、またはモデルが同じ設計）だけです。開いたパラメータについて多重アフィンでない確率（`p * p`）は区間演算で囲みます。これは粗く、セルで設計が良定義であることは、区間が `[0, 1]` に収まる場合にだけ判定できます。グラフを保存しないセルも、判定を見送らずに解析します。そこでは、期待報酬の有限の上界は、頂点のどの選び方でも確率1で目標に達する場合にだけ得られます。計算量は、開いたパラメータ・セル・状態の数とともに増えます（連立一次方程式を厳密に解き、未知数は2 000までです）。
- この実装の上限：規則S-7とS-9は状態空間全体の列挙で判定し、状態数が200 000を超えるモデルは受け付けません。評価器は、未知数が2 000を超える連立一次方程式と、点の数が10 000を超える格子を受け付けません。
- `cadl model-export` は、その設計のどのコマンドにもないアクションラベルをもつ遷移報酬を出力しません（PRISMもStormも、そのような項目を受け付けません）。項目がなくなる報酬構造には `true : (0);` を出力します。

### 戦略モデル（均衡検査と均衡マップ）

`stochastic: 0.1` と `games: 0.1` を宣言したファイルでは、`model:` ブロックに `kind: smg` を書けます（仕様書の付録I）。これは、付録Hのモデルの上のターン制確率ゲームです。`players:` でプレイヤーを宣言し、コマンドの `owner:` でそれを誰が選ぶかを述べ、`profiles:` で記憶なしで決定的な戦略のプロファイルに、順序のある規則 `{guard, choose}` の形で名前を付け、`compliance:` で契約が求めるプロファイルを指し、`payoffs:` で各プレイヤーに、目標に達するまでに累積する期待報酬の符号付きの重み付き和を与え、`equilibria:` で、プロファイルが単独の逸脱に対してナッシュ均衡（またはε均衡）であるかを問います。例は `examples/games_robot_delivery.cadl`（取扱手順を省くことのできるロボット。省くとλに比例する制裁を受ける）と `examples/games_two_players.cadl`（ディスパッチャも監視するかどうかを選ぶ）です。

```bash
# 固定されたパラメータ値での均衡検査（model.equilibria のうち over: のない項目）
cadl game-check examples/games_robot_delivery.cadl
cadl game-check examples/games_robot_delivery.cadl --equilibrium comply_nash --regime CENTRALIZED --set lambda=0.2 --format json

# 均衡マップ（over: のある項目）：equilibrium / not_equilibrium と保証したセル、または undecided のセル
cadl game-check examples/games_robot_delivery.cadl --certify
cadl game-check examples/games_robot_delivery.cadl --certify --equilibrium comply_by_lambda --max-cells 500 --format json

# 一様な分割から始め、解析したままのセルを報告（I.12の16個のセル）
cadl game-check examples/games_two_players.cadl --certify --equilibrium watched_map --partition 4 --no-merge

# 戦略モデルの性質と運用モードマップは、プロファイルのもとで評価（既定は遵守プロファイル）
cadl model-check examples/games_robot_delivery.cadl
# ゲームをPRISM-gamesのモデル言語で出力（モデルファイルのみ）
cadl model-export examples/games_robot_delivery.cadl --output prism_out/
```

- **均衡検査**は、設計とプレイヤーごとに、プロファイルのもとでの利得、最適応答値、逸脱利得、標準の最適応答（プロファイルのコマンドが最適な状態ではそれを選び、そうでない状態では宣言順で最初の最適なコマンドを選ぶ戦略）を、厳密な有理数で報告します。続いて、`status`（`equilibrium`、または `deviators` を伴う `not_equilibrium`）と、`from_every_state`（どのプレイヤーも、到達できるどの状態からも得をしないか。`equilibrium` かつ ε = 0 のときだけ）を報告します。最適応答は、ほかのプレイヤーの戦略を固定して得られる1人のプレイヤーのマルコフ決定過程の上で、連立一次方程式を厳密に解く方策反復によって計算します。プロファイルは**許容**でなければならない（どの1人のプレイヤーも、ゲームを自分の利得の目標から遠ざけ続けられない）ので、記憶なしで決定的な最適応答は、記憶や確率的な選択を使う戦略を含むすべての戦略の中で最適です（I.7）。
- **均衡マップ**は、`over:` の領域をセルで覆います。`equilibrium` と `not_equilibrium` は、セルの**すべての**付値についての主張であり、保証付きです。セルの上では、各プレイヤーのプロファイルに対するゲームを緩和します（各状態が、許されたコマンドごとに、セルの頂点での分布を一つ選ぶ。多重アフィンでない確率は、厳密な区間で囲む）。そして、緩和した過程の厳密な最適値から、I.9の十分条件N・E1・E2によってセルを判定します。さらに、逸脱するコマンドの1ステップの優位 Q_p(s, c) − V_p(s) を通して逸脱利得を抑える条件PD+・PD−（性能差の恒等式、I.9）でも判定します。それらの界が広すぎ、確率が多重アフィンであるときは、2つの道を各ステップでセルの同じ頂点を取るように一緒に進めて優位を抑えます（結合した界、I.9）。セルを判定するのは、どのプレイヤーの緩和した過程でも、すべての方策が確率1で目標に達し、かつ設計がそのセルで適切に定義されることを示せたときだけです。それ以外のセルは `undecided` とし、`reason`（`bounds`・`not_admissible`・`not_well_defined`）を付けます。未決定の割合が `resolution:` 以下になるか、上限（`--max-depth`、既定は24。`--max-cells`、既定は20 000）に達するまで、最大の未決定のセルを二分します。その後、同じ主張をもつセルを一つにまとめます（`--no-merge` を付けると、解析したままのセルを報告します）。浮動小数点数は使いません。
- `cadl game-check` の終了ステータス（I.13）：求められた結果をすべて計算できたときは、答えが何であっても **0**。マップが分解能に達しなかったとき、または問い合わせを評価しなかったときは **1**。何も主張できないとき（静的な誤り、使い方の誤り、使った値で設計が適切に定義されない、またはプロファイルが許容でない）は **2**。`--set` を使うと、これらの条件をその値で検査し直します。評価するマップが開くパラメータは `--set` で指定できません。`--certify` を付けないとマップは計算せず、そのことを出力に書きます。
- 戦略モデルに対する `cadl model-check` は、各性質と各運用モードマップを、その `profile:`（既定は遵守プロファイル）のマルコフ連鎖で評価します。`cadl model-export` は、設計ごとにモデルファイルを一つ出力します。先頭は `smg` で、プレイヤーごとに `player … endplayer` を一つ書きます（その設計のコマンドを所有しないプレイヤーは、アクションなしで書きます）。性質ファイルは出力しません（I.11）。出力したファイルはPRISM-games 3.2.4で実行しました。PRISM-gamesがインストールされていれば（`CADL_PRISM_GAMES` にPRISM-gamesの配布物の `prism` スクリプトを指定）、`tests/test_games_prism_games.py` が、その利得、最適応答値、判定を `cadl game-check` と比べます。 `cadl sim-ir` は、ゲームを `stochastic_model` に入れて出力します（I.13）。
- **規範との結び付け（付録J、J.7）。** `norms: 0.1` と `grounding: 0.1` も宣言したファイルでは、利得の項 `sanction_of: "C.n"` の重みは −m · ℓ、`reward_of: "C.n"` の重みは m′ · ℓ です。m（m′）は規範の `on_violation.magnitude`（`on_fulfilment.magnitude`）、ℓ は `from: "C.lambda"` で束縛したパラメータです。`cadl sim-ir` は、定義された重み（`"-2 * lambda"`）を `sanction_of`・`reward_of` の欄とともに出力します。このような項を使って計算した結果には、テキストでもJSON（`assumptions`）でも、J.7の仮定（「報酬構造 r は C.n の違反の回数を数えるものとする」）を付けます。`cadl check` は、各 `sanction_of:`・`reward_of:` の項について、固定値での遵守プロファイルのもとでの期待回数を設計ごとに報告します（J-12）。`sanction_of:` の項では、0は遵守しても制裁を受けないこと、すなわちJ.7の条件 (ii) を意味します。`reward_of:` の項は、回数にかかわらず、重みの符号によって条件 (ii) を満たしません。`grounding: 0.1` の宣言がなければ、このような項は誤りです（I-7）。重みが定義されない項を既定の重みで読むことはなく、その問い合わせは `not_evaluated` と報告します（終了ステータス1）。
- **端点による保証（I.9）。** 均衡マップの開いたパラメータが一つ（ℓ）であり、ℓ がどの確率にもどの報酬の値にも現れず、どの重みにも文面上で数えて（積では因子の次数を足し、打ち消しは考えない）1次以下でしか現れないとき（条件A）、条件N、E1、E2で決まらなかったセル [a, b] は、a と b の両方でプロファイルがε均衡であれば `equilibrium` とします（厳密な評価。どの逸脱利得も ℓ について凸）。プレイヤー p について条件Bが成り立ち、対応する端での逸脱利得がεを超えるとき、p をそのセルの逸脱者とします。条件B−は、ほかのパラメータに使う値において ℓ の係数 c が0でない p の項がすべて c < 0 を満たし、その報酬構造のプロファイルのもとでの期待値が0であることです（逸脱利得は非増加で、b での値で判定）。条件B+は c > 0 として同じことです（非減少で、a での値で判定）。どの戦略モデルでも使え、付録JのJ-14はこの方法を参照します。このようなセルには `method: "end points (I.9)"` を付け、マップには設計ごとに、条件Aが成り立つか、逸脱利得が非増加または非減少であるプレイヤーは誰か（`endpoints`）、使わない場合はその理由を書きます。端点は上下界で決まらなかったセルだけを決め（`not_equilibrium` のセルに逸脱者を加えることはある）、上下界による主張はすべてそのまま残ります。端点からはこれ以外の主張は導きません。
- **到達可能なすべての状態からの均衡。** `equilibria:` の項目に `concept: every_reachable_state` を書くと、初期状態からだけでなく（既定の `concept: nash`）、Reach_p（ほかのプレイヤーがプロファイルに従うとき、p が自分の逸脱で到達できる状態）のどの状態から単独で逸脱しても得をしないかを問います。検査では、プレイヤーごとに、Reach_p の状態からの最大の逸脱利得（`state_gain`）、それを与える最初の状態（`state`）、そこからの標準の最適応答も報告します。マップでは、E2、緩和した過程のすべての状態でのE1、または端点によってセルを `equilibrium` とします。逸脱者は、NまたはN_sで確定します。N_sは、セルのすべての付値で Reach_p に属する状態 s（セル全体で確率が正である遷移の道があること。多重アフィンなら全頂点で、そうでなければ厳密な区間演算で確かめる）と、s からの利得がすべての付値でプロファイルの利得をεより多く上回る逸脱です（`deviation_states`：s と逸脱利得の下界）。この概念は部分ゲーム完全均衡より弱いものです。p の逸脱でしか到達しない状態でのほかのプレイヤーの脅しは、そのプレイヤー自身の利害に照らして検査されません。`concept: subgame_perfect` と `correlated` は予約されています（I-11の情報とI-9の誤り）。
- 限界。ファイルで名前を付ける戦略は、記憶なしで決定的です。混合戦略による均衡、同時手番、部分観測はありません（I.1）。マップの条件は十分条件であり、必要条件ではありません。均衡となる領域の境界に接するセルは、分割をどれだけ細かくしても `undecided` のまま残ります。あるプレイヤーの緩和した過程に、目標に決して達しない方策があるセル（たとえば、ある確率が0になって閉路ができる付値を含むセル）は、プロファイルがそこで許容であっても `undecided` のまま残ります。`not_equilibrium` のために試す逸脱は、セルの中心での標準の最適応答です。セルに挙げる逸脱者は、セル全体で確かめられたものなので、粗いセルでは、それを分けたセルより少ないことがあります。I-3は、付録Hのキーワードをプレイヤーのidにすることを禁じていますが、I.12の例はプレイヤーを `R` と名付けています。この処理系は、`F`・`U`・`P`・`R` をプレイヤーのidとして受け付けます（出力では、H.11のとおり名前を変えます）。

### AI統合

`cadl ai` は自然言語の記述からClaude APIを用いてCADL定義を生成します：

1. ユーザが自然言語（日本語・英語対応）でSoSを記述
2. Claudeがfew-shot例を参考にCADL定義を生成
3. 生成されたCADLをパース・型検査
4. `--verify` を指定すると、`cadl verify` と同じSMT検証とデッドロック検出も行い、失敗した検査をバリデーションエラーとして扱う
5. バリデーションエラー時はエラーフィードバック付きでリトライ

`ANTHROPIC_API_KEY`環境変数の設定と `pip install "cadl-lang[ai]"` が必要です。

### 型検査の内容

型検査器（`cadl check`）は以下を検証します：

1. **アクター参照の存在確認** — 当事者、権限、情報共有、プロトコルステップ、`assume:` / `guarantee:` の述語で参照されるアクターが定義済みであること。述語の中では、添字付きの名前（`ROBOT[i]`）とメンバー参照の対象（`ROBOT.battery`）をアクターとして扱い、単独の名前は状態変数とみなします
2. **契約当事者の整合性** — すべての契約に当事者があり、同じ当事者が重複していないこと
3. **プロトコルステップの妥当性** — 送信者・受信者がアクター定義と一致すること
4. **情報共有の整合性** — 共有宣言が有効なアクターを参照していること
5. **パラメータ値域制約** — `0 <= alpha, beta, lambda <= 1`
6. **idの一意性と深刻度** — アクター・契約・プロトコル・メトリクスのidが重複していないこと、`severity:` が `Minor` / `Major` / `Critical` のいずれかであること
7. **識別子** — アクター・契約・プロトコル・メトリクスのidと運用モードの名前がASCIIの識別子であり、予約語（`AND`、`OR`、`NOT`、`IN`、`in`、`exists`、`true`、`false`）でないこと
8. **共有の項目** — `sharing:` の各項目が引用符付きの文字列 `"SOURCE -> TARGET : item"` であること。引用符のない項目や形式に合わない項目はエラーとし、引数付きの item は警告を出して受け付けます
9. **自律度** — `autonomy:` が `low` / `medium` / `high` のいずれかであること
10. **SoS-DSLの静的意味論**（付録E、E.4） — `lifecycle:` の初期状態・終端状態・`from`・`to`・`on_violation.transition` が `states` に列挙されていること、終端状態が存在し到達可能であること（L-1、L-2、L-3、L-5）。モニタの `on_match.transition` が同じ契約の状態であること（M-3）。状態名・遷移id・モニタidが識別子であり、idが重複していないこと。`on:` のメッセージイベントのアクターと、`observe:` の各項目の対象が定義済みであること（M-1）。`deadline:` が期間（整数に `ms`・`s`・`min`・`h` を付けたもの）であり、`sampling:` が `event` か `periodic(<期間>)` であること。読めない値はエラーとします。寛容な形式として、負でない秒数（`2.5`）、単位の前の空白（`5 s`、`periodic( 500ms )`）、マッピング `{kind: periodic, period_ms: <整数>}` を受け付けます。警告：`on_violation:` のない `deadline:`（L-4）、付録Eの規則として読めない `on:` / `when:` / `rule:`
11. **規範拡張の静的意味論**（付録F、F.7。`norms: 0.1` を宣言したファイルのみ） — 規範のidが識別子であり、契約内で重複せず、`creates:` が名付ける制度的事象と異なること（N-1）。`kind:` が `obligation` / `permission` / `prohibition` / `power` のいずれかであること（N-2）。`bearer:` と `counterparty:` がワイルドカードや範囲を含まない定義済みのアクターであること（N-3）。`deadline:` が期間であり、`deadline:` と `on_violation:` は義務と禁止にだけ書けること（N-4）。`effects:` は権能にだけ書け、権能には一つ以上必要で、契約内のほかの規範を名指すこと（N-5）。`reparation:` がトリガのない義務か禁止であり、`reparation:` をたどって循環しないこと（N-6）。`on_violation.transition` が契約の `lifecycle:` の状態であること（N-7）。メッセージイベントのアクターが定義済みであり、権能はメッセージから作られる制度的事象か担い手が送るメッセージで行使されること（N-9）。`within(d, p)`・`throughout(d, p)`・`until(p, q, d)` の `d` が0より大きい期間リテラルであり、`assume:`・`guarantee:`・規範の `content:` にだけ現れること（N-10）。`on_amendment: {transform: X}` が識別子を名指すこと（N-11）。警告：`trigger:` / `until:` / `content:` 全体として使われた規範のid（N-8）、担い手と内容が同じ許可と禁止（N-12）、メッセージのトリガがメッセージの内容の発生でもあり、発効させた観測で違反となる禁止（counts_asの事象を経るものも含む。N-13、最善の努力）、記号の境界と同じ名前の限量子・内包表記の変数（N-14）、読み方が意外かもしれないワイルドカード参照や範囲参照（`NOT` の下、1つの比較の中の2つの集合、`OR` の両方の被演算子の同じ集合、2つの集合にわたる集約。N-15）、契約の当事者でない担い手・相手方（N-3）、担い手以外が送るメッセージを内容とする義務・禁止（N-9）。宣言がない場合、`norms:` と `counts_as:` は未宣言の拡張キーとして警告を一つ出し、検査もIRへの変換もしません
12. **確率モデル拡張の静的意味論**（付録H、H.10。`stochastic: 0.1` を宣言したファイルのみ） — `model:` に `variables:` と `commands:` があり、`kind:` が `dtmc` であること（S-1）。id・ラベル名・列挙リテラル・アクションラベルが識別子であり、キーワードでなく、それぞれの名前空間で一度だけ宣言されること、ラベル名が `init`・`deadlock` でないこと、列挙リテラルの出力名 `x_L` が宣言済みのどの名前とも、また互いにも異なること（S-2）。`from:` が定義済みの契約の `alpha`・`beta`・`lambda`、または `context.environment` の数値の項目を指すこと（S-3）。`range:` が `[lo, hi]` であり、契約に束縛したパラメータ以外では必須で、すべての値がその範囲に入ること（S-4）。変数の型が `bool`・`"[lo..hi]"`・`"{A, B}"` のいずれかで、`init:` が定義域の値であること（S-5）。状態述語が文法に従い、型が正しいこと（S-6）。更新が宣言済みの変数にその型の値を代入し、ガードが真になるすべての状態で値が定義域に収まること（S-7）。確率が数値・パラメータの式・`rest` のいずれかで、`rest` はコマンドごとに一つ以下、合計がちょうど1であること（S-8）。どの設計のどの状態でも、有効なコマンドが一つ以下であること（S-9）。`model.regimes:` と `when:` がファイルの運用モードを指し、`transitions:` があること（S-10）。すべてのパラメータがすべての設計で固定値を持ち、そこで確率が `[0, 1]` に入り報酬が負でないこと（S-11）。報酬構造に項目があり、その `action:` がコマンドのアクションラベルであり、`value:` が運用モードマップの開いたパラメータを含まないこと（S-12）。性質が性質の文法に従い、`R` は歩数の上限のない `F` にだけ使い、しきい値が範囲内であること（S-13）。運用モードマップが、1〜4個の開いたパラメータ、2個以上の運用モード、問い合わせである目的関数一つ、しきい値性質である制約、`(0, 1)` の分解能を持ち、領域の頂点で各設計が適切に定義されること（S-14）。S-7とS-9は状態空間を列挙して判定します（200 000状態まで）。警告：出所が同じ二つのパラメータ（S-3）、`model.regimes:` に項目のない運用モード（S-10）。情報：将来の版のために予約されたキーと値（S-15）。宣言がない場合、`model:` は未宣言の拡張キーとして警告を一つ出し、検査もIRへの変換も出力もしません

13. **エージェントガバナンス拡張の静的意味論**（付録G、G.9。`agents: 0.1` を宣言したファイルのみ） — ファイルが `norms: 0.1` も宣言していること（G-1）。`agent:` の `kind:` が `llm_agent` / `tool_server` / `human` / `service` のいずれかで、`protocol:` が名前であること（G-2）。`tools:` はツールサーバにだけ書け、ツールサーバには一つ以上必要で、ツール名が重複しない識別子であること（G-3）。受信者がツールサーバであるメッセージイベントがそのツールを名指すこと（G-4。プロトコルのステップでは警告）。`enforcement.default` が `deny` か `allow`、`enforcement.point` が `caller` / `receiver` / `gateway` のいずれかであること（G-5）。執行対象の契約では、許可・禁止の内容であるメッセージの送信者が担い手であること（G-6）。`on_denied:` は執行対象の契約の、メッセージを内容とする禁止にだけ書け、キーが一つ以上あり、`timeout:` が0より大きい期間で `escalate_to:` を伴うこと（G-7）。`escalate_to:` と `notify:` が定義済みの単一のアクターを名指し、`escalate_to:` が担い手以外の当事者であること（G-8）。`escalate_to: A` を持つ契約に、`A` のメッセージから `approval` を作る `counts_as:` ルールがあり、執行対象の契約では `approval` と `rejection` が予約されていること（G-9）。`fallback:` が、担い手から定義済みのアクターへの、禁止されたメッセージとは異なるメッセージイベントであること（G-10）。`delegations:` は執行対象の契約にだけ書け、各項目が、担い手に束縛される添字のない、メッセージを内容とする許可を一つ名指すこと（G-11）。`to:` にツールサーバ以外の定義済みのアクターを並べ、`max_depth:` が1以上であること（G-12）。`grant:` と `revoke:` が識別子で、互いに異なり、`approval`・`rejection` や当事者のツールとも異なること（G-13）、執行対象の契約では、すべてのメッセージイベント（規範の `trigger:`・`until:`・`content:`、`counts_as:` ルールの `when:`、委任の `until:`）の送信者と受信者が当事者の一つの参照に含まれ（契約は自分が統治するメッセージだけを観測するため）、宣言された要素だけを名指すこと（G-17）、契約のすべての添字変数（担い手、相手方、`escalate_to:`、`notify:`、`fallback:`、メッセージイベントの送信者・受信者・引数、述語。`ROBOT[j+1]` のような添字の式の中も含む）が、当事者（当事者 `ROBOT[i]` の `i`）、限量子、または内包表記によって束縛されていること（G-18）。警告：`if:` のもとで作られる制度的事象が、禁止のトリガまたは許可の `until:` であること（G-19）、`timeout:` のない `escalate_to:`（G-7）、`human` でない承認者（G-8）、`rejection` のルールがないこと（G-9）、`to:` に含まれる担い手（G-12）、内容がメッセージイベントでない、または引数を持つ許可・禁止（G-14）、`default: allow` で開いたままのツールと、執行対象の契約の当事者でないツールサーバ（G-15）、契約が必要としながら自ら拒否するメッセージ（G-16）。宣言がない場合、`agent:`・`enforcement:`・`delegations:`・`on_denied:` は未宣言の拡張キーとして警告を一つ出し、検査もIRへの変換もしません

14. **戦略モデル拡張の静的意味論**（付録I、I.10。`games: 0.1` を宣言したファイルのみ） — `stochastic: 0.1` も宣言していること（I-1）。`kind:` が `dtmc` か `smg` であり、`kind: smg` のモデルに `players:`・`profiles:`・`compliance:`・`payoffs:` があり、それ以外のモデルに本拡張のキーがないこと（I-2）。プレイヤーのidが一意な識別子であり、各プレイヤーが宣言済みのアクターを挙げ、一つのアクターを2人のプレイヤーが代表しないこと（I-3）。`owner:` がプレイヤーを指し、所有されたコマンドがアクションラベルを持ち、同じラベルのコマンドの所有者が一つであるか、どれも所有者を持たないこと（I-4）。どの設計のどの状態でも、有効なコマンドが一つ以下であるか、すべて1人のプレイヤーのものでラベルが互いに異なること（I-5。S-9に代わる）。プロファイルの名前が一意な識別子であり、規則がラベルを使わないガードのもとで所有されたコマンドのラベルを選び、二つ以上のコマンドが有効などの状態でもどのプロファイルもコマンドを選び、`compliance:` がプロファイルを指すこと（I-6）。どのプレイヤーにも利得がちょうど一つあり、その目標が真偽値の述語で、項が一つ以上あり、宣言済みの報酬構造を一度ずつ使い、重みが宣言済みのパラメータの式であること。`sanction_of:` または `reward_of:` をもつ項には `grounding: 0.1` の宣言が要ること（I-7）。どのプロファイルも、どの設計でも、固定値で許容であること（I-8）。`equilibria:` の項目が、性質と運用モードマップの中で一意なid、プロファイル、設計、負でない `epsilon:` を持つこと。マップでは、範囲内にあって、どの運用モードも値を割り当てず、どの報酬の値にも現れない1〜4個の開いたパラメータと、`(0, 1)` の `resolution:` を持ち、領域の頂点で設計が適切に定義され、領域のすべての面の中心でプロファイルが許容であること（I-9）。性質と運用モードマップの `profile:` がプロファイルを指すこと（I-10）。I-5とI-6は状態空間を列挙して判定し、I-8とI-9は到達可能なゲームの上で判定します。警告：どの状態でも最初に適用される規則にならないプロファイルの規則（I-6）、目標が初期状態で成り立つ利得（I-7）。情報：予約されたキーと値（I-11）、`games: 0.1` を宣言していないファイルにある本拡張のキー（そのファイルは付録Hに従って読みます。I.15）。`stochastic: 0.1` なしで `games: 0.1` を宣言したファイルは、I-1の誤りになります

15. **根拠付け拡張の静的意味論**（付録J、J.8。`grounding: 0.1` を宣言したファイルのみ） — ファイルが `norms: 0.1` も宣言していること（J-1）。`grounding.items` のキーが識別子であって予約語でなく、値が契約の当事者を指すアクター参照であること、`tolerance:` が `[0, 1]` の数であること（J-2）。`shares:` が、内容がメッセージ事象である義務または許可にあり、宣言された項目を一つ以上、重複なく挙げていること（J-3）。大きさが有限で負でない数であること、`on_fulfilment:` が義務または禁止にあり、`reward:`（文言）か `magnitude:` を持つこと（J-4）。次は警告であり、エラーにはなりません。項目をその出所に与える共有規範、当事者でない宛先やアクター集合の添字なしの名前である宛先、宣言されていない項目を持つ共有エントリ、項目が二つ以上あるか引数を持つために対を与えない共有エントリ（J-3）。制裁や報酬の文言と並ぶ大きさ0（J-4）。与えられた対がある（指標が未定義のときは共有エントリがある）のに `alpha: 0`、与えられていない対があるのに `alpha: 1`、最も多くの権限を担う単一アクターが担っていない権限があるのに `beta: 1`、均等な割合を超えて権限を担う単一アクターがいるのに `beta: 0`（J-6）。0でも1でもない宣言された `alpha` または `beta` が指標から許容差を超えて離れていること、決定権者の権限がほかのアクターより少ないこと（J-7）。規範を持つ契約で、制裁も報酬も持つ義務・禁止がないのに `lambda` が0より大きいこと、持つものがあるのに `lambda: 0` であること（J-8）。`stochastic: 0.1` も宣言したファイルでは、`C.alpha` または `C.beta` に束縛されたパラメータが指標から許容差を超えて離れている設計を、J-6かJ-7が同じ宣言値をすでに報告している場合を除き、情報として報告します（J-9）。比較は正確です（小数は書かれたとおり、比は分数で扱います）。宣言がない場合、`grounding:`、`shares:`、`on_fulfilment:`、`on_violation.magnitude` は未宣言の拡張キーとして警告を一つ出し、解釈せず、指標も計算しません（J.12）。戦略モデル（付録I）では、項が `weight:`・`sanction_of:`・`reward_of:` のうち一つ以下を持つこと、参照が定義済みの契約の義務または禁止を指し、その鍵に対応する大きさが0より大きいこと、`C.lambda` に束縛したパラメータがちょうど一つあること、規範の担い手を代表する（担い手を覆うアクター参照を持つ）プレイヤーの利得であること、一つの規範を鍵ごとに一度しか名指さないこと（J-10、誤り）。警告：結び付けた契約の、大きさを持ち、担い手をプレイヤーが代表するのに、どの項も名指さない規範、`C.lambda` に束縛したパラメータを含む書かれた重み（J-11）。情報：各 `sanction_of:`・`reward_of:` の項の、遵守プロファイルのもとでの期待回数（設計ごと、J-12）。J-14は `cadl game-check --certify` が使います

このほか、検査を失敗させずに次を報告します。`cadl codegen` が出力できない `codegen:` のターゲット（警告）、未知の拡張名、`motivation:` ブロック（情報）。`motivation:` ブロックは解釈しませんが、保持して `cadl sim-ir` の出力に引き渡します。

## ハンズオン

ロボット配送 System of Systems を題材に、**CADL モデリング → SoS-DSL 契約（lifecycle + monitors） → 可視化 → コード生成 → ライブシミュレーション** までを一気通貫で体験する、単独で読めるテキストブックが用意されています。自習なら約 95 分で進められ、5 回の授業としても使えます。

教材内容：

- **テキストブック（コース A）** — 英日バイリンガルの全 11 章と付録 4 つ。SoS の背景と CADL の要点から始め、エンドツーエンド実行スクリプト（`scripts/sos_dsl_handson_e2e.sh`）を使う 15 分 × 6 の手順に進む。第 4〜9 章の終わりに、5 回の授業向けの ★／★★／★★★ の演習（縮小 3 回版とルーブリックは付録 C）。
- **そのほかのコース** — コース B（SUMO による都市モビリティ）、コース D（規則を自分たちで直す街）、コース C（自分の SoS）。
- **学術背景** — Maier の 5 条件、**ISO/IEC/IEEE 21839 / 21840 / 21841** 規格、関連研究領域（ADL、規範的 MAS、実行時検証）、注釈付き参考文献。

| 想定読者 | 入口 |
| --- | --- |
| 自習で素早く全体像を掴みたい方 | [テキストブック — コース A：ロボット配送](https://www.ertl.jp/cadl-spec/ja/docs/textbook/) (JA) / [EN](https://www.ertl.jp/cadl-spec/docs/textbook/) |
| 授業で学ぶ学生 | [テキストブック 付録 C — 授業での使い方](https://www.ertl.jp/cadl-spec/ja/docs/textbook/appendix-c-teaching/) (JA) / [EN](https://www.ertl.jp/cadl-spec/docs/textbook/appendix-c-teaching/) |
| 引用したい研究者 | [Why SoS-DSL?（学術背景）](https://www.ertl.jp/cadl-spec/ja/docs/handson/academic-background) (JA) / [EN](https://www.ertl.jp/cadl-spec/docs/handson/academic-background) |

教材本体は [cadl-spec リポジトリ](https://github.com/ertlnagoya/cadl-spec) にあり（英語ソースは `docs/handson/`、日本語ソースは `i18n/ja/docusaurus-plugin-content-docs/current/handson/`）、[仕様サイトの Hands-on セクション](https://www.ertl.jp/cadl-spec/ja/docs/handson/) でレンダリングされます。

ロボット配送のサンプルでパイプライン全体をローカル実行：

```bash
./scripts/sos_dsl_handson_e2e.sh
```

IR JSON と Unity C# ツリーが生成され、後者が [cadl-raspimouse-simulator](https://github.com/ertlnagoya/cadl-raspimouse-simulator) の Unity プロジェクトに配置されます（このリポジトリの隣にクローンしてある場合。無い場合は `--unity ""` を付けるとコード生成までで終了します）。続きはハンズオン教材を参照してください。

## サンプル

### CADL定義ファイル

| ファイル | 概要 | SoSタイプ | 主な特徴 |
|---|---|---|---|
| `robot_delivery.cadl` | 自律配送ロボット群 | 認知型 | 経路調整、障害時再計画、2つの運用モード |
| `smart_city_traffic.cadl` | 交通信号制御 | 協調型 | 3つの運用モード（通常/渋滞/緊急）、緊急車両優先 |
| `supply_chain.cadl` | 製造サプライチェーン | 協調型 | 4アクター連鎖、品質リコール、4つの運用モード |
| `iot_data_sharing.cadl` | IoTセンサデータ共有 | 仮想型 | データ鮮度契約、プライバシーポリシー、異常検知 |
| `household_chores.cadl` | 家庭内家事分担 | 協調型 | 人間中心設計、金銭的インセンティブ、紛争解決 |
| `a_sos_robot_delivery.cadl` | MAPF配送ロボット（A-SoS） | 認知型 | 中央ECBSプランナ、3つの運用モード、安全保証 |
| `c_sos_taxi_fleet.cadl` | 自律タクシー群（C-SoS） | 協調型 | 分散LRA*、ピア衝突解決、3つの運用モード |
| `raspimouse_d_sos.cadl` | Raspimouse群ロボット（D-SoS） | 指示型 | NATS経由の集中調停、NaiveDijkstra、beta=0.9 |
| `raspimouse_c_sos.cadl` | Raspimouse群ロボット（C-SoS） | 協調型 | ローカルDirectionDijkstra＋中央検証、離散時間同期 |
| `raspimouse_mcp_sos.cadl` | Raspimouse群ロボット（MCP-SoS） | 認知型 | MCPツールによるLLM制御、Static/Dynamicパスモード、3つの運用モード |
| `sos_dsl_robot_delivery.cadl` | SoS-DSL拡張を使ったロボット配送 | 認知型 | 契約ライフサイクル（7状態）、3つのモニタ。ハンズオン講座で使うサンプル |
| `agents_tool_governance.cadl` | LLMプランナ、サブエージェント、2つのツールサーバ（agents 0.1） | — | 執行対象の契約、人へのエスカレーション、来歴つきの委任。サンプル入力は `agents_inputs/`、`cadl agents-run` 用のトレースは `traces/` |
| `grounding_robot_delivery.cadl` | 規範と根拠付け拡張を使ったロボット配送（grounding 0.1） | 認知型 | 付録J、J.9の例。情報項目、`shares:`、制裁の大きさ。`cadl norms-indices` で指標5/8、1/2、被覆率1/3 |
| `agents_change_control.cadl` | 所有者、リードエージェント、2つのワーカー、リポジトリ（agents 0.1） | — | トリガから `until:` までの間だけ発効する禁止、`max_depth: 1` と `until:` を持つ委任。`cadl agents-run` 用のトレースは `traces/` |

### デモスクリプト

デモスクリプトを実行すると、ツールチェーン全体の動作を確認できます：

```bash
# 一連のワークフロー: パース -> 検証 -> コード生成(Python/Solidity/Rego) -> モードマップ -> IEC 62853
python examples/demo_robot_delivery.py

# 運用モード遷移の分析、ディペンダビリティ要約、マルチターゲット生成
python examples/demo_smart_city.py

# Python / Solidity / Rego出力の比較
python examples/demo_codegen_targets.py

# 3層シミュレータIRと、A-SoS / C-SoSの比較
python examples/demo_sim_ir.py
```

## プロジェクト構成

```
src/cadl/
  __init__.py          パッケージルート
  ast_nodes.py         ASTノード定義（30以上のdataclass）
  grammar.lark         式サブ言語のLark文法
  parser.py            ハイブリッドパーサ（YAML構造 + Lark式解析）
  type_checker.py      静的意味検査
  verifier.py          SMTベース契約検証（Z3）
  deadlock.py          プロトコルデッドロック検出
  regime_map.py        運用モードの遷移グラフ分析
  iec62853.py          IEC 62853を参考にした要約
  unparse.py           式ASTをCADLのソーステキストに戻す
  norms_eval.py        記録済みトレースに対する規範拡張の判定
  norms_check.py       規範の設計時の検査：PRISMへの変換、問い、証拠
  agents_runtime.py    エージェントガバナンス拡張のランタイム：トレース上の判定、保留、委任、監査記録。オフラインまたは1行ずつ
  audit_log.py         監査ログのハッシュ連鎖（SHA-256またはHMAC-SHA256）とその検証（`cadl audit-verify`）
  gateway_core.py      ゲートウェイに共通の部分：判定点、トレース、監査ログ、保留、受け箱、`mcp-approve`、`mcp-message`
  mcp_gateway.py       ランタイムでツール呼び出しを判定するMCPのstdioゲートウェイ（`cadl mcp-gateway`、`mcp-approve`、`mcp-message`）
  a2a_gateway.py       ランタイムでSendMessageを判定するA2Aゲートウェイ（JSON-RPCとHTTP+JSON。`cadl a2a-gateway`）
  a2a_rest.py          ゲートウェイのためのA2AのHTTP+JSONのバインディング：経路、エラー、JSON-RPCとの変換
  a2a_push.py          A2Aゲートウェイのプッシュ通知：許可リスト、アドレスの検査、送信
  a2a_grpc.py          A2AゲートウェイのgRPCのバインディング（任意で入れる追加の依存 cadl-lang[grpc]）
  a2a_grpc_front.py    gRPCのバインディングの前段の待ち受け：接続の上限、最初の呼び出しまでの期限、中継
  norms_diff.py        改正が規範に及ぼす効果（二つの版の比較）
  grounding.py         根拠付け拡張：導出指標、規則J-1からJ-9とJ-13
  cli.py               コマンドラインインタフェース
  codegen/
    __init__.py        公開generate() API（マルチターゲット対応）
    runtime_support.py 生成コード用基底クラス
    expr_compiler.py   式AST → Pythonソース変換
    emitter.py         コード出力ユーティリティ
    actor_gen.py       アクタークラス生成
    contract_gen.py    契約監視クラス生成
    protocol_gen.py    プロトコル状態機械生成
    transition_gen.py  運用モードコントローラ生成
    metric_gen.py      評価指標コレクタ生成
    runtime_gen.py     オーケストレータ生成
    norms_monitor_gen.py       規範モニタ生成（Pythonターゲットの norms_monitor.py）
    norms_monitor_template.py  生成する規範モニタに写すテキスト（インポートしない）
    norms_ir.py        契約の規範をデータにしたもの（OPAとSolidityのモニタ用）
    solidity/
      solidity_expr.py 式AST → Solidityソース変換
      solidity_gen.py  Solidityスマートコントラクト生成
      norms_monitor_sol.py    Solidityターゲットの規範モニタ
      norms_monitor_base.sol  その基底コントラクトとライブラリ（そのまま写す）
    opa/
      rego_expr.py     式AST → Regoソース変換
      rego_gen.py      OPA/Regoポリシー生成
      norms_monitor_rego.py   OPAターゲットの規範モニタ
      norms_monitor_lib.rego  そのステップ関数（そのまま写す）
    agent_policy/
      rego_gen.py      境界ポリシーのRego生成（agents 0.1）
      cedar_gen.py     境界ポリシーのCedar生成と付随する表（agents 0.1）
    unity_csharp/
      contract_emitter.py  契約ごとのC#（状態enum・契約・モニタ）
      runtime_template.py  生成のたびに出力するC#ランタイム
    safety.py          コード生成前の入力検査
  ai/
    __init__.py        公開generate_cadl() API
    prompts.py         システムプロンプト・few-shot例
    llm_client.py      Claude APIクライアント
    nl_to_cadl.py      自然言語→CADL生成パイプライン
  sim/
    __init__.py        公開API: lower_to_ir, validate_ir, generate_config
    ir.py              3層IR dataclass定義（SimIR, InstitutionLayer等）
    lower.py           AST → IR 変換（lowering）
    validate.py        IR検証
    gen_python.py      IR → Pythonシミュレータ設定（YAML）
    gen_unity.py       IR → Unityシミュレータ設定（JSON）
    gen_go.py          IR → Goシミュレータ設定（JSON）

tests/
  test_parser.py         パーサのテスト
  test_type_checker.py   型検査のテスト
  test_verifier.py       SMT検証のテスト
  test_deadlock.py       デッドロック検出のテスト
  test_codegen.py        Pythonコード生成のテスト
  test_ai.py             AI統合のテスト
  test_runtime.py        ランタイム基底クラスのテスト
  test_regime_map.py     モードマップ分析のテスト
  test_solidity_gen.py   Solidityコード生成のテスト
  test_opa_gen.py        OPA/Regoコード生成のテスト
  test_iec62853.py       IEC 62853要約のテスト
  test_sim_ir.py         シミュレータIR変換・検証のテスト
  test_sim_gen.py        シミュレータ設定ジェネレータのテスト
  test_sim_ir_sos_dsl.py シミュレータIR内のSoS-DSL（lifecycle / monitors）
  test_sos_dsl_extension.py       SoS-DSLパーサのテスト
  test_unity_csharp_codegen.py    Unity C#生成のテスト
  test_unity_csharp_structural.py 生成C#の構造チェック
  test_expr_parser.py    式文法のテスト
  test_unparse.py        式の往復変換テスト
  test_norms_eval.py     トレースに対する規範の判定（`cadl norms-eval`）
  test_norms_check.py    規範の設計時の検査と差分テスト（`cadl norms-check`）
  test_norms_amend_diff.py  改正：ランダムなバージョンの組で `cadl norms-diff` と `cadl norms-eval` を比較し、ミューテーションで検出力を確かめる
  norms_amend_diff.py    test_norms_amend_diff.py のバージョンの組とトレース
  test_grounding_extension.py  根拠付け拡張：指標、規則、IR、`cadl norms-indices`
  test_grounding_sos.py  根拠付け拡張：すべての契約の上のSoSの量
  test_norms_wildcards.py 述語の中のワイルドカード参照と範囲参照（F.5）：書き換え、限量子の形との一致、N-15
  test_norms_monitor.py  生成した規範モニタと `cadl norms-eval` の比較（サンプル、記録したケース、ランダムなトレース）
  norms_monitor_cases.py そのために test_norms_eval.py のケースを記録するpytestプラグイン
  test_norms_monitor_targets.py  OPAとSolidityのターゲットの規範モニタと `cadl norms-eval` の比較
  norms_targets_support.py       その参照、OPAのドライバ、EVM用の変換
  norms_evm_driver.py            SolidityのモニタをEVMで実行する（別のPython。cadlを使わない）
  conftest.py                    test_norms_eval.py のケースをセッションごとに一度記録する
  test_name_resolution.py 生成コードでのアクター／状態変数の解決
  test_method_dispatch.py 検証メソッドの振り分けテスト
  test_codegen_safety.py コード生成の入力安全性テスト
  test_examples_verify.py 全サンプルが `cadl verify` を通ることの確認
  test_actor_reference_check.py   `cadl check` がアクターとして扱う名前
  test_conformance_checks.py      識別子の規則、共有の項目、codegenのターゲット、拡張
  test_codegen_deterministic.py   生成コードが実行ごとに同一であること
  test_keyword_boundaries.py      キーワードが長い名前から切り出されないこと
  test_agents_extension.py        エージェントガバナンス拡張：G-1〜G-19、判定、agent-rego / agent-cedar
  test_agent_policy_diff.py       agent-cedar（cedar CLI）とagent-rego（OPA）を同じ入力空間で `cadl agents-decide` と比較（G.10、G.13）
  agent_policy_diff.py            test_agent_policy_diff.py の入力、ランダムなファイル、G.10の規則
  test_agents_runtime.py          エージェントガバナンス拡張のランタイム（`cadl agents-run`）：保留、委任、監査記録
  test_audit_log.py               監査ログのハッシュ連鎖：1バイトの変更、削除、挿入、入れ替え、鍵の有無（`cadl audit-verify`）
  test_mcp_gateway.py             偽のサーバとクライアントによるMCPゲートウェイ：`cadl agents-run` で再生するシナリオ、不正な入力、異常終了、並行処理
  mcp_fake_server.py              test_mcp_gateway.py 用の偽のMCPサーバ（stdio）
  test_a2a_gateway.py             偽のエージェントとクライアントによるA2Aゲートウェイ：`cadl agents-run` で再生するシナリオ、認証、不正な入力、タスクの分離
  a2a_fake_agent.py               A2Aゲートウェイの試験用の偽のA2Aエージェント（JSON-RPCとHTTP+JSON、プッシュ通知）
  test_a2a_bindings.py            A2Aゲートウェイ：相互TLS、HTTP+JSON、プッシュ通知とSSRFの検査、gRPC
  a2a_certs.py                    test_a2a_bindings.py 用の試験用CAと証明書（openssl）
  test_a2a_grpc_front.py          gRPCのバインディングの前段の待ち受け：大量の接続、遅いハンドシェイクと何も送らない接続、ハーフオープンの接続、クライアントのアドレス
  test_sdk_interop.py             公式のMCPとA2AのSDKに対するゲートウェイの試験（SDKがなければ飛ばす）
  sdk_mcp_server.py               test_sdk_interop.py 用の mcp SDK のMCPサーバ
  sdk_a2a_agent.py                test_sdk_interop.py 用の a2a-sdk のA2Aエージェント

examples/
  robot_delivery.cadl        ロボット配送SoS（認知型）
  smart_city_traffic.cadl    スマートシティ交通制御（協調型）
  supply_chain.cadl          サプライチェーン管理（協調型）
  iot_data_sharing.cadl      IoTデータ共有（仮想型）
  household_chores.cadl      家庭内家事分担（協調型）
  demo_robot_delivery.py     エンドツーエンドワークフローデモ
  demo_smart_city.py         モードマップ・IEC 62853デモ
  demo_codegen_targets.py    マルチターゲットコード生成デモ
  a_sos_robot_delivery.cadl  A-SoS MAPFロボット配送サンプル
  c_sos_taxi_fleet.cadl      C-SoS 自律タクシー群サンプル
  raspimouse_d_sos.cadl      Raspimouse群ロボット D-SoS（指示型）
  raspimouse_c_sos.cadl      Raspimouse群ロボット C-SoS（協調型）
  raspimouse_mcp_sos.cadl    Raspimouse群ロボット MCP-SoS（LLM制御）
  test_raspimouse.sh         Raspimouse用パース・検証・Unity設定生成テスト
  sos_dsl_robot_delivery.cadl  SoS-DSL（lifecycle / monitors）付きロボット配送
  demo_sim_ir.py             3層シミュレータIRとA-SoS / C-SoS比較のデモ

scripts/
  sos_dsl_handson_e2e.sh     ハンズオン用パイプライン：check → Sim-IR → Unity C# → Unityプロジェクト
```

## 開発状況

CADLはアルファ版です（[CHANGELOG.md](CHANGELOG.md)を参照）。言語仕様と生成物はリリース間で変わる可能性があります。

| 機能 | 状態 |
|---|---|
| 言語コア・パーサ・型検査器 | 利用可能 |
| 検証（SMTベース無矛盾性検証、デッドロック検出） | 利用可能 |
| Pythonランタイムのコード生成 | 利用可能 |
| Claude APIによる自然言語→CADL変換 | 利用可能 |
| 運用モード遷移・モードマップ | 利用可能 |
| IEC 62853レポート、Solidity・OPA/Rego生成 | 利用可能 |
| シミュレータIR・設定生成（Python, Unity, Go） | 利用可能 |
| SoS-DSL拡張（`lifecycle:` / `monitors:`）とUnity C#生成 | 利用可能 |
| 規範拡張：静的検査と、記録済みトレースに対する判定と改正（`cadl norms-eval`、`cadl norms-diff`） | 利用可能 |
| 規範拡張：生成したPythonパッケージのオンラインモニタ（`norms_monitor.py`） | 利用可能 |
| 規範拡張：生成したRego（ステップ関数）とSolidityのモニタ | 量化子、集約、ワイルドカード参照、改正を除いて利用可能（生成時に警告して拒否する）。Unity C#は規範を監視しない |
| 規範拡張：PRISMによる1つの契約インスタンスの設計時の検査（`cadl norms-check`） | 利用可能（互いに大小を比べる4個以上の量は拒否する） |
| 根拠付け拡張（grounding 0.1）の規範側：導出指標とインセンティブ被覆率（`cadl norms-indices`）、規則J-1からJ-9、IR、二つの版の比較（`cadl norms-diff` のJ-13） | 利用可能 |
| 戦略モデル拡張（games 0.1）：均衡検査と保証付きの均衡マップ（`cadl game-check`）、付録Jの規範との結び付け（`sanction_of:` / `reward_of:`、J-10からJ-12、J-14） | 利用可能（出力はPRISM-games 3.2.4と比較済み） |
| エージェントガバナンス拡張（agents 0.1）：検査、判定関数、記録済みトレースに対するランタイム（保留、委任、ハッシュ連鎖つきの監査記録。`cadl agents-run`、`cadl audit-verify`）、MCPのstdioゲートウェイ（`cadl mcp-gateway`）、JSON-RPC、HTTP+JSON、gRPCによるA2Aゲートウェイ（`cadl a2a-gateway`）、Rego / Cedarの境界ポリシー | 利用可能（ゲートウェイは参照実装で、偽のサーバ、エージェント、クライアントと、公式のMCPとA2AのSDKで試験している） |
| 検証メソッド `model_check` / `simulation` / `proof` | 未対応（未対応である旨を明示的に報告） |

## 関連プロジェクト

- [cadl-spec](https://github.com/ertlnagoya/cadl-spec) — 仕様書とハンズオン講座。<https://www.ertl.jp/cadl-spec/ja/> で公開しています。
- [cadl-explorer](https://github.com/ertlnagoya/cadl-explorer) — `cadl sim-ir` が出力するIRから契約ライフサイクルを描画し、合成モデル上でガバナンス設定を試せるStreamlitアプリケーション。
- [cadl-raspimouse-simulator](https://github.com/ertlnagoya/cadl-raspimouse-simulator) — ハンズオン講座で使うシミュレータ。C-SoS のロボット配送シナリオ用の Unity プロジェクト、Go アービトレータ、Python 参照ランタイムを収録しています。
- raspimouse-swarm-simulator（研究用リポジトリで非公開。公開版は [cadl-raspimouse-simulator](https://github.com/ertlnagoya/cadl-raspimouse-simulator)） — マルチエージェント群ロボットシミュレーションプラットフォーム。`examples/raspimouse_*.cadl` で3つのSoSモードを記述し、Unity設定ジェネレータでシミュレータ用の構成JSONを生成できます。

## 参考文献

- Benveniste et al., "Contracts for System Design," Foundations and Trends in EDA, 2018.
- ISO/IEC/IEEE 21841:2019, Taxonomy of Systems of Systems.
- Saoud et al., "Assume-guarantee contracts for continuous-time systems," Automatica, 2021.
- IEC 62853:2018, Open Systems Dependability.
- C. Shimoyama and Y. Matsubara, "Governance as a Structural Design Variable: An Empirical Study of Performance-Autonomy Value Spaces in Systems of Systems," in Proc. 21st International Conference on System of Systems Engineering (SoSE), 2026.

## ライセンス

[Apache License 2.0](LICENSE)。0.3.2 までのリリースは MIT License で公開されています。
