# CADL: Contract Architecture Description Language

[English](README.md) | 日本語

CADL（Contract Architecture Description Language）は、System of Systems（SoS）における制度設計を形式的に記述・検証・配備するためのドメイン固有言語（DSL）です。エンジニアから市民まで、幅広いステークホルダがガバナンスルール・契約・プロトコル・インセンティブ構造を、計算機が処理可能かつ検証可能な形式で定義できます。

名古屋大学 大学院情報学研究科 松原研究室にて開発しています。

## 動機

SoS環境では、独立に運用される複数のシステムが共通のルールのもとで協調する必要があります。現状、これらのルールは自然言語の契約書や暗黙の合意に依存しており、曖昧性・矛盾・設計意図と実装の乖離が避けられません。

CADLは以下の3つの目的を実現します：

1. **制度を計算可能にする** — 権限構造・情報共有方針・インセンティブ機構などの現実世界のルールを、コピー・合成・バージョン管理・検索が可能なデータオブジェクトとしてモデル化する。
2. **矛盾と違反を検出する** — SMTソルバやモデル検査器による無矛盾性の自動検証と、制度制約のランタイム監視を可能にする。
3. **実行可能な成果物を生成する** — 検証済みの制度設計から、制御ロジック・監視コード・スマートコントラクトをターゲットプラットフォーム向けに自動生成する。

## 言語の概要

CADLファイル（`.cadl`）はYAMLライクな宣言的構文を使用します。各ファイルは1つのSoS定義を記述し、以下のセクションで構成されます：

| セクション | 役割 |
|---|---|
| `actors` | 構成システム（アクター）の役割・自律性レベル・能力の定義 |
| `contracts` | 権限(beta)・情報共有(alpha)・インセンティブ(lambda)パラメータを含むassume-guarantee契約 |
| `protocols` | メッセージ送受信・タイミング制約・フォールバックを含む協調手順 |
| `algorithms` | 中央/局所アルゴリズムの参照 |
| `transitions` | 安全不変条件を伴う制度遷移（レジーム遷移） |
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
```

## インストール

Python 3.9以上が必要です。

```bash
pip install -e ".[dev]"
```

## 使い方

```bash
# CADLファイルをパースして概要を表示
cadl parse examples/robot_delivery.cadl

# パース＋型検査を実行
cadl check examples/robot_delivery.cadl

# ASTを出力
cadl parse examples/robot_delivery.cadl --ast

# 全検証（型検査 + SMT検証 + デッドロック検出）
cadl verify examples/robot_delivery.cadl
```

## アーキテクチャ

CADL処理系は標準的なコンパイラパイプラインに従います：

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
     (Phase 2以降)
          |
    +-----+-----+-----------+
    v           v           v
  SMT検証    コード生成   ランタイム監視
```

### 型検査の内容（Phase 1）

型検査器は以下の5項目を検証します：

1. **アクター参照の存在確認** — 参照されるアクターがすべて定義済みであること
2. **契約当事者の整合性** — 当事者の重複・不在がないこと
3. **プロトコルステップの妥当性** — 送信者・受信者がアクター定義と一致すること
4. **情報共有の整合性** — 共有宣言が有効なアクターを参照していること
5. **パラメータ値域制約** — `0 <= alpha, beta, lambda <= 1`

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
  cli.py               コマンドラインインタフェース

tests/
  test_parser.py       パーサのテスト
  test_type_checker.py 型検査のテスト
  test_verifier.py     SMT検証のテスト
  test_deadlock.py     デッドロック検出のテスト

examples/
  robot_delivery.cadl      ロボット配送SoS（認知型）
  household_chores.cadl    家庭内家事分担（協調型）
```

## 実装ロードマップ

| フェーズ | 目標 | 状態 |
|---|---|---|
| Phase 1 | 言語コア設計・パーサ・型検査器 | 完了 |
| Phase 2 | 検証エンジン（SMTベース無矛盾性検証、デッドロック検出） | 完了 |
| Phase 3 | ランタイム・コード生成（Python/TypeScript） | 計画中 |
| Phase 4 | AI統合（LLMによる自然言語→CADL変換） | 計画中 |
| Phase 5 | 制度遷移・レジームマップ構築 | 計画中 |
| Phase 6 | IEC 62853連携・スマートコントラクト生成 | 計画中 |

## 参考文献

- Benveniste et al., "Contracts for System Design," Foundations and Trends in EDA, 2018.
- ISO/IEC/IEEE 21841:2019, Taxonomy of Systems of Systems.
- Saoud et al., "Assume-guarantee contracts for continuous-time systems," Automatica, 2021.
- IEC 62853:2018, Open Systems Dependability.
- 下山・松原, "Governance as a Structural Design Variable," submitted, 2026.

## ライセンス

MIT
