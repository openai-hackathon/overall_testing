# Client-side scheduling demo

這份指南從 `overall_testing/` 根目錄開始。手動 Demo 以 GCP Linux VM 的三個 SSH 終端操作，也可在 macOS 使用。

Demo 展示多個 Codex process 共用工具執行名額。人工輸入 0–1 的比較偏好後，LM 擴充同類任務，BT fit 產生 importance，scheduler 依 `importance + 等待秒數 × rate` 選擇下一個工具。

這裡排的是 tool call，不會搶停已開始的工具。這份 Demo 不需要啟動 vLLM router、responses converter 或 Modal。

## 1. 下載與編譯

在 GCP VM 上準備 Git、Python 3.11 以上與 rustup。用 `python3 --version` 確認版本；系統預設 Python 若低於 3.11，請先安裝合適版本。Rust 版本依 `codex/codex-rs/rust-toolchain.toml`，目前為 1.95.0。重畫 Pareto 圖時才需要 uv 與 matplotlib。

```bash
git clone https://github.com/openai-hackathon/overall_testing.git
cd overall_testing
cd codex/codex-rs
cargo build -p codex-cli
cd ../..
```

如果已經 clone，從現有 `overall_testing/` 目錄開始，省略 clone 與第一個 cd。

請在 GCP VM 上編譯；macOS binary 不能直接拿到 Linux 執行。成功後會有 `codex/codex-rs/target/debug/codex`。後續使用這個 binary；系統安裝的 `codex` 不一定包含 scheduler。

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

## 3. 設定真實 OpenAI

編輯 **`overall_testing/codex/.env`**，保留既有金鑰，設定：

```dotenv
OPENAI_API_KEY=your_real_api_key
TASK_EVOLVER_MODEL=gpt-5.6-luna
TASK_EVOLVER_REASONING_EFFORT=medium
```

三終端啟動器會讓 agent 推論與 LM expansion 都使用這組 OpenAI 設定。它使用獨立的 `CODEX_HOME`，不需另做 Codex login，也不修改你的日常 Codex 設定。金鑰只透過程序環境傳入，不寫入產生的 config 或提交到 Git。

環境變數優先於 `.env`。如果曾使用 router 的 `OPENAI_API_KEY=dummy`，先在三個終端各執行 `unset OPENAI_API_KEY`。有效的環境變數可以保留。

## 4. GCP 上只開三個 SSH 終端

在你的電腦開三個終端，分別 SSH 到**同一台 VM、同一個 Linux 使用者**。三個 SSH session 都需要互動式 TTY，例如：

```bash
ssh -t USER@VM_IP
```

把 `USER` 與 `VM_IP` 換成你的 VM 登入資訊。也可以使用既有的 `gcloud compute ssh` 連線方式。不要把 A、B、C 分散在不同 VM。

三個服務只監聽 VM 的 `127.0.0.1`。不需為 scheduler 或 app-server 新增 GCP 入站防火牆規則，也不需把這些埠公開。VM 必須能連出 `https://api.openai.com`，並在 VM 的 `codex/.env` 設定金鑰。

每個終端先進入 `overall_testing/` 根目錄。終端 A 管理 scheduler 與兩個背景 app-server，B、C 是聊天介面。

**終端 A：scheduler 與人工問答**

```bash
PYTHONPATH=codex/tools/task-scheduler python3 codex/tools/task-scheduler/experiments/manual_demo.py serve
```

看到 `Background app servers ready` 與 `{"port": 8765}` 後，再開 B、C。啟動器會建立 incident／oss 工作目錄、bindings 與初始 snapshot；既有比較與分數會保留。

**終端 B：incident 聊天**

```bash
PYTHONPATH=codex/tools/task-scheduler python3 codex/tools/task-scheduler/experiments/manual_demo.py incident
```

**終端 C：開源聊天**

```bash
PYTHONPATH=codex/tools/task-scheduler python3 codex/tools/task-scheduler/experiments/manual_demo.py oss
```

啟動器會把 `CODEX_SCHEDULER_SERVICE` 設在背景 app-server，並讓 B、C 連到正確的 server。兩個 server 不需要 Terminal 視窗；log 存在 Demo 資料目錄。

預設資料目錄為 `~/.local/share/codex-scheduling-demo/repo-demo`。這和先前五終端指南的 `/tmp/client-side-scheduling-demo` 是不同資料目錄，不會自動匯入舊資料。預設埠是 scheduler `8765`、incident `4501`、oss `4502`。

如果要改路徑或埠，三個命令都加上相同的參數：

```text
--state-dir /absolute/path/demo
--codex /absolute/path/codex
--ports 8766 4503 4504
```

`--codex` 可用於指定自訂 Cargo target 目錄內的 binary。預設會使用本 checkout 的 `codex/codex-rs/target/debug/codex`。

## 5. 送出任務

在 B、C 各貼上：

```text
請執行一次 shell 指令：sleep 2; echo demo-ok
不要讀取或修改任何檔案。
```

依畫面完成工作目錄信任與必要的工具核准。兩個 session 使用獨立工作目錄，對應完整的事故與開源維護描述。

## 6. 回答與觀察

未知的事故 key 會在 A 與「開源專案例行文件維護,沒有服務中斷」比較。輸入 0 到 1：`1` 表示 A 較重要、`0` 表示 B 較重要、`0.5` 表示相同。輸入 `2` 會重問同一題。

回答後先保存 human pair 並 refit，再呼叫 OpenAI 擴充，若有新候選則再次 refit。`expanded_count` 是新增比較數；`expansion_error: None` 表示未回報擴充錯誤。LM 也可以合法回傳零個候選。

`{'known': ...}` 表示已有分數，不是另一題。已學過的 key 不會重問，reference 也不會與自己比較。

等待回答或 LM 回應時，工具可能已使用 fallback 50 分加 aging 執行。新分數只改變後續選擇，不會搶停正在執行的工具。兩個短任務不保證同時排隊；要穩定展示 incident 超前，使用第 2 節的受控 Demo。

檢查分數與 trace：

```bash
cat "$HOME/.local/share/codex-scheduling-demo/repo-demo/importance.json"
tail -n 20 "$HOME/.local/share/codex-scheduling-demo/repo-demo/scheduler.jsonl"
```

確認 `fit_version` 增加、`scores` 出現對應 key，且 trace 出現兩種 `client_id`。工具是否成功仍需核對聊天介面的完成狀態。

## 7. 停止、重新啟動與清空

先完成或取消 B、C 的工作並退出聊天，再在 A 按 `Ctrl+C`。A 收到 `SIGHUP` 或 `SIGTERM` 時也會清理背景 server；若要在 SSH 斷線後保留 A，可在 VM 的 tmux session 執行 A。啟動器會停止它啟動的兩個背景 app-server。VM 強制關機或 `SIGKILL` 不會執行這段清理。同一個資料目錄只啟動一個 `serve`；不要同時執行另一個寫入同一份資料庫的 compare。

再次依 A、B、C 的順序啟動，人工比較、擴充與分數會保留。聊天紀錄保存在各自的 `*-home`；啟動器會開新對話。Queue 只在記憶體中，不支援重啟後恢復未完成的工具。

若要清空重來，先停止三個終端的 Demo，再備份整個預設資料目錄：

```bash
mv "$HOME/.local/share/codex-scheduling-demo/repo-demo" \
  "$HOME/.local/share/codex-scheduling-demo/repo-demo-backup-$(date +%Y%m%d-%H%M%S)"
```

再執行第 4 節三個命令。系統會建立空的比較、分數與聊天資料。使用自訂 `--state-dir` 時，請備份那個目錄。

## 8. 常見問題

| 現象 | 檢查方式 |
| --- | --- |
| `No module named task_evolver` | 確認位於 `overall_testing/`，且命令帶有 `PYTHONPATH`。 |
| 找不到 binary | 先編譯，或用 `--codex` 指定本次編好的 binary。 |
| OpenAI 認證失敗 | 檢查 `codex/.env` 與環境變數，尤其是先前設定的 `dummy`。 |
| 埠被占用 | 三個命令都加相同的 `--ports`，選三個不同的可用埠。 |
| app-server 啟動失敗 | 查看資料目錄的 `incident-server.log` 或 `oss-server.log`。 |
| 一直沒有問題 | 已知 key 與 reference 不會重問；若要重新學習，依第 7 節備份並清空。 |
| 分數更新但順序沒變 | 確認有工具同時等待；已開始的工具不會被中斷。 |
| 關閉問答輸入後不再提問 | EOF 會停止人工問答；退出 Demo 並重新啟動 A。 |
| 只關 A 後聊天斷線 | A 負責背景 server 的生命週期；重開 A，再重新開 B、C。 |

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
