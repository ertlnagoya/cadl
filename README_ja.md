# CADL: Contract Architecture Description Language

[English](README.md) | 日本語

CADL（Contract Architecture Description Language）は、System of Systems（SoS）における制度設計を形式的に記述・検証・配備するためのドメイン固有言語（DSL）です。エンジニアから市民まで、幅広いステークホルダがガバナンスルール・契約・プロトコル・インセンティブ構造を、計算機が処理可能かつ検証可能な形式で定義できます。

名古屋大学 大学院情報学研究科 ERTL にて開発しています。

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

### 制度パラメータ

CADLは制度の特性を[0, 1]の連続パラメータで定量化します：

- **alpha (情報共有度)** — 0: 各アクターはローカル情報のみ、1: 全情報を完全共有
- **beta (意思決定分散度)** — 0: 完全集中（単一アクターが全決定）、1: 完全分散（各アクターが自律的に決定）
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
        beta: 0.2
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
pip install -e ".[dev]"

# AI機能を使う場合（Anthropic APIキーが必要）
pip install -e ".[ai]"
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

# 運用モードの遷移を分析
cadl regime-map examples/smart_city_traffic.cadl
cadl regime-map examples/smart_city_traffic.cadl --format dot -o regime.dot

# IEC 62853準拠レポートを生成
cadl iec62853 examples/robot_delivery.cadl
cadl iec62853 examples/smart_city_traffic.cadl --format json

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
    [検証済みAST]
          |
    +-----+-----+-----------+-----------+
    v           v           v           v
  SMT検証    コード生成   モード      IEC 62853
  (Z3)      (codegen/)   マップ     準拠検査
              |
        +-----+-----+
        v     v     v
     Python Solidity OPA/Rego
```

### コード生成（Phase 3 + Phase 6）

`cadl codegen` は検証済みCADL定義から実行可能なコードを生成します。3つのターゲットに対応：

**Python** (`--target python`, デフォルト):

| 生成モジュール | 内容 |
|---|---|
| `actors.py` | 役割・自律性・能力スタブを持つアクター基底クラス |
| `contracts.py` | assume/guarantee検査を行う契約監視クラス |
| `protocols.py` | タイムアウト処理付き非同期プロトコル状態機械 |
| `transitions.py` | 遷移条件を持つ運用モードコントローラ |
| `metrics.py` | 計算式による評価指標コレクタ |
| `runtime.py` | 全コンポーネントを結合するオーケストレータ |

生成コードは `cadl.codegen.runtime_support` の基底クラスを継承します。

**Solidity** (`--target solidity`):

Ethereumスマートコントラクト（`.sol`）を生成：
- 各CADL `ContractDef` に対応するコントラクト（`checkAssumptions()`, `checkGuarantees()`, `runMonitorCycle()`）
- `RegimeController.sol` — enum型で運用モードを表す状態機械
- メインオーケストレータコントラクト
- 制度パラメータ（alpha, beta, lambda）をスケーリングされた `uint256` 定数として定義

**OPA/Rego** (`--target opa`):

Open Policy Agentポリシー（`.rego`）を生成：
- 各CADL `ContractDef` に対応するRegoパッケージ（`assumptions_hold`, `guarantees_hold`, `allow`, `violation` ルール）
- 全契約を集約するメインポリシーパッケージ
- 情報共有・権限ポリシールール

### 運用モード遷移の分析（Phase 5）

`cadl regime-map` は運用モード（regime）の遷移グラフを分析します：

- `transitions:` の定義から運用モードの有向グラフを構築
- **到達可能性分析** — 初期状態からのBFS探索で、到達できないモードを検出
- **行き止まり状態（dead state）の検出** — 出て行く遷移を持たないモードを特定
- **サイクル検出** — TarjanのSCCアルゴリズムでモード間の循環を検出
- **最短経路** — 任意の2つのモード間の最短経路をBFSで算出
- **出力形式**: テキスト要約、Graphviz DOT、JSON

### IEC 62853準拠検査（Phase 6）

`cadl iec62853` はCADL構成要素をIEC 62853オープンシステムディペンダビリティの概念にマッピングします：

| CADL概念 | IEC 62853概念 |
|---|---|
| alpha（情報共有度） | 情報透明性レベル |
| beta（権限集中度） | ガバナンス集中度指標 |
| lambda（インセンティブ整合度） | ステークホルダ整合性メトリクス |
| ContractDef | サービスレベル合意（SLA） |
| ViolationBlock | 障害応答仕様 |
| TransitionDef | 運用状態機械 |
| SoSタイプ（D/A/C/V） | システム統合レベル |

### シミュレータ設定生成（Phase 7）

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
| beta | 0.8（集中型） | 0.1（分散型） |
| 中央プランナ | ECBS | aggregation_only |
| 局所プランナ | tracking_only | LRA* + 局所衝突回避 |
| 共有方式 | アップリンク + ブロードキャスト | ピアツーピア ブロードキャスト |

#### Raspimouse群ロボットシミュレータ — D-SoS / C-SoS / MCP-SoS の比較

同一の5台ロボット・11ノードグラフネットワーク上で、異なるSoSパラダイムを記述する3つのCADL定義です。`cadl sim-gen --target unity` により、[raspimouse-swarm-simulator](https://github.com/ertlnagoya/raspimouse-swarm-simulator) のUnity設定JSONを自動生成できます。

| 特性 | D-SoS（指示型） | C-SoS（協調型） | MCP-SoS（認知型） |
|---|---|---|---|
| 意思決定主体 | ARBITRATOR | ROBOT[*]（提案）+ ARBITRATOR（検証） | LLM_AGENT |
| beta | 0.9 | 0.3 | 0.6 |
| alpha | 0.2 | 0.7 | 0.9 |
| 中央プランナ | NaiveDijkstra | DirectionDijkstra | LLM_Dijkstra |
| 局所プランナ | なし | DirectionDijkstra | NaiveDijkstra + OccupancyAware |
| 通信方式 | NATS req/res | NATS req/res + リソースクエリ | MCPツール |
| 運用モード | NORMAL ↔ CONGESTED | NORMAL ↔ CONGESTED | NORMAL ↔ COLLISION_RESOLUTION ↔ DEADLOCK |

```bash
# 3モードのUnity設定を生成
cadl sim-gen examples/raspimouse_d_sos.cadl --target unity -o output/raspimouse_d_sos_unity.json
cadl sim-gen examples/raspimouse_c_sos.cadl --target unity -o output/raspimouse_c_sos_unity.json
cadl sim-gen examples/raspimouse_mcp_sos.cadl --target unity -o output/raspimouse_mcp_sos_unity.json
```

### AI統合（Phase 4）

`cadl ai` は自然言語の記述からClaude APIを用いてCADL定義を生成します：

1. ユーザが自然言語（日本語・英語対応）でSoSを記述
2. Claudeがfew-shot例を参考にCADL定義を生成
3. 生成されたCADLをパース・型検査
4. バリデーションエラー時はエラーフィードバック付きでリトライ

`ANTHROPIC_API_KEY`環境変数の設定と `pip install cadl[ai]` が必要です。

### 型検査の内容（Phase 1）

型検査器は以下の5項目を検証します：

1. **アクター参照の存在確認** — 参照されるアクターがすべて定義済みであること
2. **契約当事者の整合性** — 当事者の重複・不在がないこと
3. **プロトコルステップの妥当性** — 送信者・受信者がアクター定義と一致すること
4. **情報共有の整合性** — 共有宣言が有効なアクターを参照していること
5. **パラメータ値域制約** — `0 <= alpha, beta, lambda <= 1`

## ハンズオン

ロボット配送 System of Systems を題材に、**CADL モデリング → SoS-DSL 契約（lifecycle + monitors） → 可視化 → コード生成 → ライブシミュレーション** までを一気通貫で体験する 90 分の自習ワークショップ（5 回の PBL コースとしても利用可）が用意されています。

教材内容：

- **メイン教材** — 15 分 × 6 ステップ、英日バイリンガル、エンドツーエンド実行スクリプト（`scripts/sos_dsl_handson_e2e.sh`）付き。
- **演習問題集** — 5 回構成の授業課題セット（縮小 3 回版あり）、★／★★／★★★ の段階的難易度とルーブリック。
- **学術背景** — Maier の 5 条件、**ISO/IEC/IEEE 21839 / 21840 / 21841** 規格、関連研究領域（ADL、規範的 MAS、実行時検証）、注釈付き参考文献。
- **PBL コース設計（教員向け）** — 各回に学術的意義と学びの観点を併記。よくあるつまずき、「なるほど」と思わせる工夫、卒研・修論につながるテーマも収録。

| 想定読者 | 入口 |
| --- | --- |
| 自習で素早く全体像を掴みたい方 | [Course A — ロボット配送（メイン教材）](https://ertlnagoya.github.io/cadl-spec/ja/docs/handson/main-textbook) (JA) / [EN](https://ertlnagoya.github.io/cadl-spec/docs/handson/main-textbook) |
| 授業で学ぶ学生 | [Course A — 演習問題集](https://ertlnagoya.github.io/cadl-spec/ja/docs/handson/exercises) (JA) / [EN](https://ertlnagoya.github.io/cadl-spec/docs/handson/exercises) |
| 授業を設計する教員 | [PBL コース設計](https://ertlnagoya.github.io/cadl-spec/ja/docs/handson/pbl-course-design) (JA) / [EN](https://ertlnagoya.github.io/cadl-spec/docs/handson/pbl-course-design) |
| 引用したい研究者 | [Why SoS-DSL?（学術背景）](https://ertlnagoya.github.io/cadl-spec/ja/docs/handson/academic-background) (JA) / [EN](https://ertlnagoya.github.io/cadl-spec/docs/handson/academic-background) |

教材本体は [cadl-spec リポジトリ](https://github.com/ertlnagoya/cadl-spec) にあり（英語ソースは `docs/handson/`、日本語ソースは `i18n/ja/docusaurus-plugin-content-docs/current/handson/`）、[仕様サイトの Hands-on セクション](https://ertlnagoya.github.io/cadl-spec/ja/docs/handson/) でレンダリングされます。

ロボット配送のサンプルでパイプライン全体をローカル実行：

```bash
./scripts/sos_dsl_handson_e2e.sh
```

IR JSON と Unity C# ツリーが生成され、後者が `raspimouse-swarm-simulator` の Unity プロジェクトに配置されます。続きはハンズオン教材を参照してください。

## サンプル

### CADL定義ファイル

| ファイル | 概要 | SoSタイプ | 主な特徴 |
|---|---|---|---|
| `robot_delivery.cadl` | 自律配送ロボット群 | 認知型 | 経路調整、障害時再計画、2つの運用モード |
| `smart_city_traffic.cadl` | 交通信号制御 | 協調型 | 3つの運用モード（通常/渋滞/緊急）、緊急車両優先 |
| `supply_chain.cadl` | 製造サプライチェーン | 協調型 | 4アクター連鎖、品質リコール、5つの運用モード |
| `iot_data_sharing.cadl` | IoTセンサデータ共有 | 仮想型 | データ鮮度契約、プライバシーポリシー、異常検知 |
| `household_chores.cadl` | 家庭内家事分担 | 協調型 | 人間中心設計、金銭的インセンティブ、紛争解決 |
| `a_sos_robot_delivery.cadl` | MAPF配送ロボット（A-SoS） | 認知型 | 中央ECBSプランナ、3つの運用モード、安全保証 |
| `c_sos_taxi_fleet.cadl` | 自律タクシー群（C-SoS） | 協調型 | 分散LRA*、ピア衝突解決、5つの運用モード |
| `raspimouse_d_sos.cadl` | Raspimouse群ロボット（D-SoS） | 指示型 | NATS経由の集中調停、NaiveDijkstra、beta=0.9 |
| `raspimouse_c_sos.cadl` | Raspimouse群ロボット（C-SoS） | 協調型 | ローカルDirectionDijkstra＋中央検証、離散時間同期 |
| `raspimouse_mcp_sos.cadl` | Raspimouse群ロボット（MCP-SoS） | 認知型 | MCPツールによるLLM制御、Static/Dynamicパスモード、3つの運用モード |

### デモスクリプト

デモスクリプトを実行すると、ツールチェーン全体の動作を確認できます：

```bash
# 一連のワークフロー: パース -> 検証 -> コード生成(Python/Solidity/Rego) -> モードマップ -> IEC 62853
python examples/demo_robot_delivery.py

# 運用モード遷移の分析、準拠検査、マルチターゲット生成
python examples/demo_smart_city.py

# Python / Solidity / Rego出力の比較
python examples/demo_codegen_targets.py
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
  iec62853.py          IEC 62853準拠マッピング
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
    solidity/
      solidity_expr.py 式AST → Solidityソース変換
      solidity_gen.py  Solidityスマートコントラクト生成
    opa/
      rego_expr.py     式AST → Regoソース変換
      rego_gen.py      OPA/Regoポリシー生成
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
  test_iec62853.py       IEC 62853準拠マッピングのテスト
  test_sim_ir.py         シミュレータIR変換・検証のテスト
  test_sim_gen.py        シミュレータ設定ジェネレータのテスト

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
```

## 実装ロードマップ

| フェーズ | 目標 | 状態 |
|---|---|---|
| Phase 1 | 言語コア設計・パーサ・型検査器 | 完了 |
| Phase 2 | 検証エンジン（SMTベース無矛盾性検証、デッドロック検出） | 完了 |
| Phase 3 | ランタイム・コード生成（Python） | 完了 |
| Phase 4 | AI統合（Claude APIによる自然言語→CADL変換） | 完了 |
| Phase 5 | 運用モード遷移・モードマップ構築 | 完了 |
| Phase 6 | IEC 62853連携・スマートコントラクト生成（Solidity, OPA/Rego） | 完了 |
| Phase 7 | シミュレータIR・設定生成（Python, Unity, Go） | 完了 |

## 関連プロジェクト

- [raspimouse-swarm-simulator](https://github.com/ertlnagoya/raspimouse-swarm-simulator) — マルチエージェント群ロボットシミュレーションプラットフォーム。`examples/raspimouse_*.cadl` で3つのSoSモードを記述し、Unity設定ジェネレータでシミュレータ用の構成JSONを生成できます。

## 参考文献

- Benveniste et al., "Contracts for System Design," Foundations and Trends in EDA, 2018.
- ISO/IEC/IEEE 21841:2019, Taxonomy of Systems of Systems.
- Saoud et al., "Assume-guarantee contracts for continuous-time systems," Automatica, 2021.
- IEC 62853:2018, Open Systems Dependability.
- 下山・松原, "Governance as a Structural Design Variable," submitted, 2026.

## ライセンス

MIT
