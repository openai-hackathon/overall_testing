# Client-side scheduling demo

這份指南從 `overall_testing/` 根目錄開始。適用於 macOS 或 Linux 的 Bash／Zsh。

Demo 展示多個 Codex process 共用工具執行名額。人工輸入 0–1 的比較偏好後，LM 擴充同類任務，BT fit 產生 importance，scheduler 依 `importance + 等待秒數 × rate` 選擇下一個工具。

這裡排的是 tool call，不會搶停已開始的工具。這份 Demo 不需要啟動 vLLM router、responses converter 或 Modal。

## 1. 下載與編譯

需要 Git、Python 3.11 以上與 rustup。Rust 版本依 `codex/codex-rs/rust-toolchain.toml`，目前為 1.95.0。重畫 Pareto 圖時才需要 uv 與 matplotlib。

```bash
git clone https://github.com/openai-hackathon/overall_testing.git
cd overall_testing
cd codex/codex-rs
cargo build -p codex-cli
cd ../..
```

如果已經 clone，從現有 `overall_testing/` 目錄開始，省略 clone 與第一個 cd。

成功後會有 `codex/codex-rs/target/debug/codex`。後續使用這個 binary；系統安裝的 `codex` 不一定包含 scheduler。

若編譯出現 `No space left on device`，先釋放磁碟空間再重跑。不要將失敗編譯留下的舊 binary 當成新版。

## 2. 先跑可重現的自動 Demo

在 `overall_testing/` 根目錄執行：

```bash
PYTHONPATH=codex/tools/task-scheduler python3 codex/tools/task-scheduler/experiments/app_server_demo.py \
  --codex codex/codex-rs/target/debug/codex \
  --output /tmp/client-side-scheduling-demo-results
```

腳本會自行啟動共享 scheduler、兩個真實 app-server 與本機 SSE model stub，不需要 API key 或 Codex 登入。LM expansion 在這個測試使用固定假回應，工具則實際執行短 Python 指令。

腳本驗證：

1. 開源任務先排隊，incident 任務後排隊。
2. 測試偏好從 0.1 改為 0.9，經 expansion 與 refit 後，incident 先取得下一個 slot。
3. Refit 保留原本入列時間。
4. 取消其中一個等待中的 turn，另一個 client 仍能完成。
5. 兩個 client 身分不同，總共有三個成功工具輸出，取消的 turn 沒有取得 grant。

成功時程式以 exit code 0 結束，`app-server-report.json` 的重點欄位為：

```json
{
  "app_server_processes": 2,
  "grant_order": ["incident", "oss"],
  "waiting_timestamps_preserved": true,
  "queued_cancellation": "passed",
  "other_waiter_after_cancellation": "passed",
  "successful_tool_outputs": 3
}
```

檢查結果：

```bash
cat /tmp/client-side-scheduling-demo-results/app-server-report.json
tail -n 20 /tmp/client-side-scheduling-demo-results/app-server-trace.jsonl
```

已保存的範例見 [app-server report](codex/tools/task-scheduler/results/app-server/app-server-report.json)。這個結果驗證真實 process 接線，並不代表全程使用真實模型。

## 3. 設定真實 LM expansion

編輯 **`overall_testing/codex/.env`**，加入或更新下列欄位。保留你已有的金鑰，不要將 `.env` 提交到 Git。

```dotenv
OPENAI_API_KEY=your_real_api_key
TASK_EVOLVER_MODEL=gpt-5.6-luna
TASK_EVOLVER_REASONING_EFFORT=medium
```

環境變數優先於 `.env`。如果曾依 router 範例設定 `OPENAI_API_KEY=dummy`，請在啟動 Python service 的終端先執行：

```bash
unset OPENAI_API_KEY
```

這讓 service 改讀 `codex/.env` 的真實金鑰。若你本來就使用有效的環境變數，不需 unset。

這組 model 設定只控制 LM expansion。Codex agent 的 model 與登入沿用其本身設定，`.env` 不會替 Codex 登入。需要登入時，在根目錄執行：

```bash
./codex/codex-rs/target/debug/codex login
```

## 4. 建立目錄對應與初始 snapshot

以下命令從 `overall_testing/` 根目錄執行。兩個目錄代表不同任務情境，也可改成你的既有專案目錄；每次 export 或 compare 都必須保留完整的 binding 清單。

```bash
export SCHEDULER_DEMO_DIR="${TMPDIR:-/tmp}/client-side-scheduling-demo"
mkdir -p "$SCHEDULER_DEMO_DIR/incident" "$SCHEDULER_DEMO_DIR/oss"

PYTHONPATH=codex/tools/task-scheduler python3 -m task_evolver \
  --db "$SCHEDULER_DEMO_DIR/pairs.sqlite3" \
  --reference "開源維護" \
  --snapshot "$SCHEDULER_DEMO_DIR/importance.json" \
  --bind "$SCHEDULER_DEMO_DIR/incident" "生產事故處理" \
  --bind "$SCHEDULER_DEMO_DIR/oss" "開源維護" \
  export
```

首次 export 可以只有 bindings、沒有學習分數。這不是將手填 importance 寫進資料庫。未命中的 call 暫用 50 分加 aging；fallback 不會寫入 importance table。

資料庫固定使用同一個 reference。不要用不同的 reference 重開同一份資料庫。

## 5. 啟動共享 service 與兩個 session

準備五個終端。**每個終端先進入 `overall_testing/` 根目錄。**

### 終端 A：scheduler 與人工問答

```bash
export SCHEDULER_DEMO_DIR="${TMPDIR:-/tmp}/client-side-scheduling-demo"
PYTHONPATH=codex/tools/task-scheduler python3 -m task_evolver.admission \
  --port 8765 --slots 1 --rate 1 \
  --snapshot "$SCHEDULER_DEMO_DIR/importance.json" \
  --db "$SCHEDULER_DEMO_DIR/pairs.sqlite3" \
  --reference "開源維護" \
  --trace "$SCHEDULER_DEMO_DIR/scheduler.jsonl"
```

看到 `{"port": 8765}` 表示 service 已開始監聽。保持終端開啟，人工問題會出現在這裡。

### 終端 B：incident app-server

```bash
export SCHEDULER_DEMO_DIR="${TMPDIR:-/tmp}/client-side-scheduling-demo"
export CODEX_DEMO_BIN="$PWD/codex/codex-rs/target/debug/codex"
cd "$SCHEDULER_DEMO_DIR/incident"
CODEX_SCHEDULER_SERVICE=127.0.0.1:8765 \
  "$CODEX_DEMO_BIN" app-server --listen ws://127.0.0.1:4501
```

### 終端 C：開源 app-server

```bash
export SCHEDULER_DEMO_DIR="${TMPDIR:-/tmp}/client-side-scheduling-demo"
export CODEX_DEMO_BIN="$PWD/codex/codex-rs/target/debug/codex"
cd "$SCHEDULER_DEMO_DIR/oss"
CODEX_SCHEDULER_SERVICE=127.0.0.1:8765 \
  "$CODEX_DEMO_BIN" app-server --listen ws://127.0.0.1:4502
```

### 終端 D：操作 incident session

```bash
export SCHEDULER_DEMO_DIR="${TMPDIR:-/tmp}/client-side-scheduling-demo"
./codex/codex-rs/target/debug/codex \
  --remote ws://127.0.0.1:4501 \
  -C "$SCHEDULER_DEMO_DIR/incident"
```

### 終端 E：操作開源 session

```bash
export SCHEDULER_DEMO_DIR="${TMPDIR:-/tmp}/client-side-scheduling-demo"
./codex/codex-rs/target/debug/codex \
  --remote ws://127.0.0.1:4502 \
  -C "$SCHEDULER_DEMO_DIR/oss"
```

兩個 session 都請它執行短測試指令，例如等待兩秒並輸出一行文字，不修改檔案。依 Codex 提示完成工作目錄信任與必要的工具核准。

Scheduler 環境變數必須設在 **B、C 的 app-server process**。只設在 D、E 的 TUI，不會改變已經啟動的 server。一般 `codex exec`、已開啟的 Desktop session，以及 router README 的啟動命令，不是本指南驗證的接線方式。

這些手動 WebSocket／TUI 指令已核對目前 CLI 參數；已執行的自動 process 驗證使用 stdio JSON-RPC，沒有自動操作上述兩個 TUI。

## 6. 回答與觀察

新 bound key 尚無分數時，終端 A 會問它與「開源維護」誰較重要。輸入 0 到 1：

- `1`：第一個任務較重要。
- `0`：reference 較重要。
- `0.5`：相同重要性。
- `0.9`：偏好第一個任務，可用於這個模擬 incident 範例。

同 key 同時只會有一個待回答問題。Reference 本身不需與自己比較。回答後先保存 human pair 並 refit，再呼叫真實 OpenAI 擴充，完成後再次 refit。

等待回答或 LM 回應時，工具可能已按 fallback 執行。新分數只改變後續選擇，不會搶停正在執行的工具。要穩定展示「人工修正後超前」，使用第 2 節的受控 Demo；手動操作的模型產生時間並不固定。

在另一個終端檢查：

```bash
export SCHEDULER_DEMO_DIR="${TMPDIR:-/tmp}/client-side-scheduling-demo"
cat "$SCHEDULER_DEMO_DIR/importance.json"
tail -n 20 "$SCHEDULER_DEMO_DIR/scheduler.jsonl"
```

確認 `fit_version` 增加、`scores` 出現對應 key，且 trace 出現兩種 `client_id` 的 enqueue／grant。`disconnect` 只表示 lease 關閉，不單獨代表工具成功；仍需核對 session 的完成或取消狀態。

## 7. 修正偏好、停止與重新啟動

先取消／完成 D、E 的工作，再停止 B、C，最後停止 A。A 啟用學習時，不要同時執行另一個寫入資料庫的 compare。

若要修正既有比較，停止上述工作後，在根目錄執行：

```bash
export SCHEDULER_DEMO_DIR="${TMPDIR:-/tmp}/client-side-scheduling-demo"
PYTHONPATH=codex/tools/task-scheduler python3 -m task_evolver \
  --db "$SCHEDULER_DEMO_DIR/pairs.sqlite3" \
  --reference "開源維護" \
  --snapshot "$SCHEDULER_DEMO_DIR/importance.json" \
  --bind "$SCHEDULER_DEMO_DIR/incident" "生產事故處理" \
  --bind "$SCHEDULER_DEMO_DIR/oss" "開源維護" \
  compare "生產事故處理" "開源維護" --score 0.7
```

完成後依 A、B、C、D、E 的順序重開。資料庫與 snapshot 保留分數；queue 只在記憶體中，不支援重啟後的 lease recovery。

## 8. 常見問題

| 現象 | 檢查方式 |
| --- | --- |
| `No module named task_evolver` | 確認目前在 `overall_testing/`，並使用 `PYTHONPATH=codex/tools/task-scheduler`。 |
| LM expansion 失敗 | 檢查 `codex/.env`、model 與環境變數是否仍為 `dummy`；人工分數會保留。 |
| 一直沒有問題 | 檢查 call 的 cwd 是否命中 binding；已知 key、reference 與未綁定目錄不會出現新比較問題。 |
| 工具一直等待 | 確認 A 存活、B／C 有 scheduler 環境變數，且 slot 沒被其他工具持有。 |
| 埠被占用 | 為 service、兩個 app-server 選不同埠，同步修改連線端設定。 |
| 已回答但次序沒變 | 確認 fit version 更新，且確實有工具等待；已執行的工具不會被中斷。 |
| 關閉問答輸入後不再提問 | EOF 會停止自動問題，admission 繼續；停止工作並重開 service 才會恢復問答。 |
| 開了兩個視窗仍沒有共享 trace | 確認連到 B／C，而非其他已啟動的 daemon 或 Desktop process。 |

## 9. 重跑公平性實驗

```bash
PYTHONPATH=codex/tools/task-scheduler python3 codex/tools/task-scheduler/experiments/replay.py \
  --output /tmp/client-side-scheduling-replay
uv run --with matplotlib python codex/tools/task-scheduler/experiments/plot.py \
  /tmp/client-side-scheduling-replay
```

這會產生每個策略十個配對 seed、兩種 workload，共 200 輪與 16,000 個模擬 call 的結果。橫軸是背景最大等待，縱軸是重要任務平均等待；兩者越低越好。

見 [Pareto 圖](codex/tools/task-scheduler/results/replay/frontier.png)、[結果表](codex/tools/task-scheduler/results/replay/summary.csv)與[實驗方法](codex/tools/task-scheduler/DEMO.md#reproduce-the-frontier)。誤差棒是 sample SD，不是顯著性證明，沒有通用最佳 threshold。

更多 protocol 與參數細節見 [scheduler README](codex/tools/task-scheduler/README.md)。故障恢復、MoM 與 Modal 分流不在本次核心 Demo 範圍。
