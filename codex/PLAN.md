# 跨 Session 任務排程計畫

工作項目開始前，先加上 `WIP`。完成驗證後，移除 `WIP` 並勾選 `[x]`。尚未開始的項目保留 `[ ]`。

## 目標

- 在多個 Codex session 之間分配執行機會。
- 降低重要任務的等待時間。
- 避免背景任務長期無法執行。
- 用 Pareto frontier 比較延遲與公平性的取捨。

## 架構

- 本次範圍：tool call 排程、人工偏好、必要的 LM expansion、BT fit 與跨 process service。
- MoM（多模型路由、OpenAI／vLLM 分流）延後，不列入本次 demo 或驗收條件。
- 本次固定 model backend。LM expansion 是偏好資料擴充功能，不隨 MoM 延後。
- 後續若加入 model 請求排程，可共用 importance，但使用獨立 slot、入列時間與 rate。
- 目前：Rust scheduler 位於 `codex-rs/ext/scheduler/`，接入 app-server。
- 目前：設定 `CODEX_SCHEDULER_SERVICE` 後，不同 Codex process 共用 Python service 的 queue；未設定時保留本機 scheduler。
- 目前：獨立 Python service 位於 `tools/task-scheduler/`，支援終端人工問答與背景 LM 擴充。
- Python service 管理全域 queue、PairStore、LM 擴充、BT fit、lookup 和人工問答。
- Rust adapter 負責 enqueue、等待 grant、完成後 release。
- 接入 Python service 後，由 service 決定 tool call 順序，Rust adapter 只執行 gate。
- slot 空出時選擇下一個 tool call。不搶佔正在執行的 tool。
- Serving 是後續整合。候選分流為高重要性走 OpenAI API，背景任務走 Modal vLLM，另測延遲、品質與成本。
- Router 分流不等於 model 請求排程。要控制等待順序，需明確指定 router admission queue 或 backend scheduler 的責任。
- Rust 保留 cwd tier baseline。設定 `CODEX_SCHEDULER_IMPORTANCE` 後，改用 task evolver 發布的 JSON snapshot，依 cwd/key 明確對應查詢 BT importance。

## 已完成

- [x] 實作 FIFO、strict priority、aging 和 linear 策略。
- [x] 加入 slot 限制與 JSONL trace。
- [x] 記錄等待時間、持有 slot 時間、thread ID、turn ID 和 call ID。
- [x] 修正 code-mode 外層 cell 與內層 tool 爭用同一 slot 的問題。
- [x] 修正 linear 的 confidence fallback。非零分差決定順序，同分才走 FIFO。
- [x] 使用 `CallKey { thread_id, turn_id, call_id }` 隔離不同呼叫。
- [x] 執行初步多 session 實驗與參數掃描。
- [x] 核心驗證：25 個 Rust 測試與 47 個 Python 測試通過；兩個真實 app-server 使用固定 SSE model 回應完成共享 queue、refit、取消與工具輸出驗證。
- [x] 均勻 workload（sleep 2s）：FIFO、priority、aging 2/4/8/16s、linear rate 1/4/8/15，各 3 輪。
- [x] 有界長尾 workload（sleep 從 `[1, 1, 1, 2, 2, 3, 5, 8]` 抽樣）：FIFO、priority、aging 2/4s、linear rate 2/8/15，各 3 輪。每個 task 與輪次使用固定 seed。
- [x] Router smoke：純 stdlib，依 `x-priority` 分流到 fast / slow mock backend。本輪以 mock 重跑通過。
- [x] Modal vLLM 離線 generate 有歷史紀錄。該次環境使用 `nvidia/cuda` devel image，避免當次缺少 nvcc 的建置問題。這不是所有 vLLM 部署的必要條件。
- MoM 歷史狀態：先前記錄的 Modal HTTP 200 尚待複核，隨 MoM 延後。已有 `vllm_serve.py`。
- 實驗腳本與 trace 位於 session scratchpad：`workload.py`、`frontier.py`、`router/smoke.py`、`modal/vllm_serve.py`。
- [x] 在 repo 保存可重跑的受控 workload、畫圖腳本、逐輪 trace 與結果，位於 `tools/task-scheduler/experiments/` 與 `results/`。舊 scratchpad 實驗保留為歷史，Router／Modal 隨 MoM 延後。

## 已觀察

- 均勻 workload：priority 讓 tier 0 平均等待 2.7s → 1.5s，tier 3 max wait 4.0s → 6.1s。
- 均勻 workload：aging 4s 的樣本均值兩軸都略優於 strict priority。這不證明策略普遍較好，誤差線也不是顯著性檢定。
- 長尾 workload：priority 讓 tier 0 平均等待約 5.8s → 2.9s；tier 3 每輪最大等待的平均約 17.2s → 16.7s。
- 長尾 workload：aging 2/4s 的重要任務等待接近 FIFO，不代表執行順序相同。門檻效果需同時考慮 call 長度、到達流量與 queue 深度。
- 長尾 workload 每策略僅 3 輪。現有資料不足以宣稱 max wait 或 slowdown 有統計顯著差異。
- 舊 linear 圖受 `ChainScorer` 門檻 0.5 影響，分差小於 5 點時掉回 FIFO。保留原始資料，但標成舊版混合策略，不用來評估修正後的純 linear。
- 固定 seed 只固定 sleep 序列，不固定 LM 產生 tool call 的到達時間。現有實跑結果不是受控排程比較。
- Aging threshold 是開始提升順位的門檻，不是最大等待保證。Non-preemptive tool 可讓等待超過門檻。

## 分工

- call-scheduler session：第一階段、第三階段。
- task-evolver session：第二階段，程式碼放 `tools/task-scheduler/task_evolver/`。純 stdlib，測試在 `tools/task-scheduler/tests/`。
- 核心收尾包含第二至第四階段；MoM／Serving 維持延後。

## 第一階段：完成 Rust 修正（call-scheduler）

- [x] 統一 PairTable 正反方向，合併 `A/B` 與 `B/A` 的回答。
- [x] 加入 PairTable 回歸測試，驗證合併後的勝率、confidence 與不同記錄順序。此表仍累計投票，第二階段再實作來源覆蓋與最新人工修正。
- [x] 將 code-mode `exec`、`wait` 豁免限定在 default namespace。
- [x] 為 code-mode namespace 豁免加入回歸測試。其他 namespace 的同名工具需等待 slot；default namespace 的 wrapper 可直接通過。
- [x] 定義本機取消、斷線和 timeout 的 slot 清理行為，詳見下方 contract。
- [x] 將 timeout 放行事件加入 JSONL，驗證事件欄位與超額放行後的 slot 回收。保留 300 秒 fail-open，會突破 slot 限制。
- [x] 處理拒絕、取消、重複 release 與 grant 交錯。驗證取消等待、取消未接收的 grant，以及終止通知不影響其他 call。
- [x] 加入 thread 停止時的 slot 回收，驗證其他 thread 不受影響。

### Client-side gate 行為

- Host 每個 `CallKey` 只呼叫一次 admission，不重用同一 turn 的 call ID。跨 process 的重試與冪等性在第三階段處理。
- 等待中的 admission future 被取消或丟棄時，立即移除等待項目。
- Grant 已送出但 admission 尚未返回時，丟棄 future 會釋放 slot，再選下一個等待項目。
- `Completed`、`Blocked`、`Failed`、`Aborted` 都走 release。未開始或已釋放的 call 不影響其他 call。
- Release 先移除等待與執行紀錄，再補足可用 slot。取消已先移除的 call 不會因 grant 或 timeout 再次放行。
- Tool lifecycle gate 只回傳 `()`，無拒絕回傳值。若 host 在 gate 等待時先送出終止通知，gate 保持 pending，直到 host 丟棄 future，避免返回後執行已取消工具。
- Host 停止 thread 後，清除該 thread 的等待與執行紀錄，再讓其他 thread 使用 slot。
- UI 斷線不等於 thread 停止。仍存活的工作繼續持有 slot；本機 process 結束時，記憶體 queue 隨之消失。遠端 lease 與斷線回收屬第三階段。
- 保留 300 秒 fail-open。只有仍在等待的 call 可超時放行；它會計入 running，直到 release。Running 降到 slot 上限以下前，不補一般 grant。
- JSONL 新增 `event=grant_timeout`，記錄 call 身分、等待時間、放行後 running 數量與 slot 上限。一般釋放使用 `event=release`，保留原欄位。
- 分析腳本只用 release 計算完成 call 指標。舊 trace 無 `event` 時視為 release；timeout 事件另計，不當成完成 call。
- Grant 信號分 `Granted` 與 `Cancelled`。等待中的 call 被 release 或 thread 停止移除時，收到 `Cancelled` 並保持 pending。
- 收到 `Granted` 但已不在 running，代表 release 先於 admission 返回，同樣保持 pending。兩種情況都寫 info log。
- Timeout 或 grant 信號遺失時，若 call 既不在等待也不在 running，以 `event=waiter_lost` 放行，避免無聲 hang。
- Thread 停止造成的釋放寫 `event=cancelled`，分析腳本不計入完成 call。
- Rust `PairTableScorer` 只要有非平手的投票就回傳 certain，單筆人工回答即可改變順序。平手回 UNKNOWN。

## 第二階段：建立偏好學習元件（task-evolver）

- [x] 設定 LM expansion 使用 `gpt-5.6-luna` 與 `reasoning.effort=medium`；根目錄 `.env` 已設定，真實 API 回傳四個同類 key，38 個 Python 測試通過。
- [x] LM expansion 改為 OpenAI Responses API；API key 使用 `OPENAI_API_KEY`，支援根目錄 `.env` 且環境變數優先；model 使用 `--model` 或 `TASK_EVOLVER_MODEL`。

### 資料

- [x] 修正 BT 收斂與數值穩定性。使用回溯步長與梯度檢查；未收斂時回報錯誤，不回傳分數。`lr` 是初始步長，`l2` 必須為正。
- [x] 保證 PairStore 更新失敗時完整 rollback。使用單一寫入交易處理舊資料停用與新資料插入。
- [x] 人工覆蓋 seed 時停用舊擴充；後寫入的 seed 不影響有效 human 與其擴充。
- [x] 驗證 expanded parent 必須是有效 human／seed。拒絕不存在、已停用、已被覆蓋與 expanded parent。
- [x] 拒絕無效偏好、權重與 importance。Probability 限 0–1，weight 為有限正數，importance 限 0–100；無效 score 更新保留上一版。
- Task evolver 最新驗證：47 個 Python 測試通過。真實 OpenAI expansion 已驗證 `gpt-5.6-luna` 與 medium effort；完整排序測試使用固定人工分數與假 LM，避免外部模型波動。
- 使用方式見 `tools/task-scheduler/README.md`。支援 init、compare、lookup 自動問答、唯讀 lookup 與共享 service 終端問答。


- Pair：`pair_id, a_key, b_key, p_a_wins, weight, source, parent_pair_id, revision`。
- Source：`human`、`seed`、`expanded`。
- Score：`key, importance, fit_version`。
- Task：`task_id, key`；每筆排隊請求另存 `client_id, thread_id, turn_id, call_id, resource, queued_at`。
- Task key 表示任務類型與情境，例如「廠房事故處理」和「廠房事故演練」要分開。Repo 名稱本身不足以決定重要性。
- 固定 key 正規化規則。不要讓 LM 每次產生不同字串而重複 miss。
- `p_a_wins` 表示偏好，`weight` 表示訓練權重。兩者分開保存。

### 規則

- 每對 key 使用固定方向。反轉方向時，將 `p` 改成 `1 - p`。
- 同一對 key 的有效資料採 `human > seed > expanded`。
- 第一版採單一使用者偏好。保存回答歷史，最新人工修正取代該對先前人工回答；BT 只讀目前有效資料。
- 每個 `pair_id` 指向不可變的資料版本，`revision` 決定同來源的更新順序。Expanded pair 指向產生它的原始版本。
- `A/B` 的回答只取代同一對比較，不刪除 `A/C`。
- Human 與 seed 的初始權重為 1.0。
- 每個原始 pair 的擴充總權重最多 0.2。用 `parent_pair_id` 追蹤來源。
- LM 只擴充 human／seed，不遞迴擴充 expanded。限制每次候選數並去重。
- 每個擴充 key 必須連到既有比較群組。例如從 `(A, B)` 產生 `(A', B)`，不要只產生孤立的 `(A', B')`。
- 原始比較遭修正時，停用其舊 expanded pairs，再產生新版本。
- 「同類」不保證偏好相同。Expanded pairs 是低權重假設，不是新的人類證據。
- Seed 必須包含比較結果。Keywords 本身不提供重要性。
- Importance 全由 BT fit 產生，不手填。
- 人工直接輸入 0–1 的 `p_a_wins`：1 表示 A 較重要，0 表示 B 較重要，0.5 表示同等；接受 0.7 等中間值。這是 pair 偏好，不是單一 task 的 importance。

### Init

- [x] `init KEYWORD ...` 使用 reference 星狀比較集，每個正規化後的新 key 比較一次；無隨機抽樣，先保證比較圖連通。
- [x] 讓使用者直接輸入 0–1 的 pair 偏好。CLI 支援 `compare A B --score 0.7`，省略 score 時互動輸入。
- [x] 保存人工比較至 PairStore。
- [x] 接入必要的 LM expansion，使用 OpenAI Responses API 與 JSON schema。真實 API 呼叫已驗證；LM 只接收 task key，不接收人工分數。
- [x] Expanded pairs 繼承來源偏好，保留 parent ID，每個 parent 總權重最多 0.2；去重並跳過既有 key。
- [x] Workflow 在 refit 前驗證比較圖連通。首筆比較需包含固定 reference，資料庫禁止更換 reference。原始 BT 函式仍只負責數值求解。
- [x] 使用 `P(A > B) = sigmoid(z_A - z_B)` 的加權交叉熵，加入 L2，固定參考 key 的 `z=0`。
- [x] 用固定 `importance = 100 × sigmoid(z / temperature)` 轉換；固定 temperature 與正則化設定。不要每次 refit 都做 min-max。
- [x] 原子更新 importance table 與 fit version；失敗時保留上一版。新分數尺度需重新掃 rate，不能直接沿用 tier 實驗的參數。

### Online

- [x] 從新任務取得 key，先使用 exact lookup。
- [x] Lookup hit 時，使用 `importance + 等待秒數 × rate` 排序。
- [x] 驗證 rate 為有限且非負的數值；rate 為 0 表示不提升等待順位。Refit 不重設 `queued_at`。
- [x] Lookup miss 時，請使用者比較新任務與既有任務。
- [x] 未回答前使用共同的暫時 fallback 等級與 aging。同 key 不重複問，不把暫時等級寫入 importance table。
- [x] 人工回答後立即 refit，原子發布 snapshot；service 每次 grant 選擇時載入新分數。
- [x] 重複執行 CLI compare 可修正既有 pair，停用舊擴充並重建新版本。
- [x] 人工比較後執行 LM expansion，擴充完成後再 refit。LM 失敗會回報狀態，保留人工回答與上一版分數。
- [x] 新 key 至少與既有 key 比較一次，避免形成孤立的比較群組。
- Embedding 最近鄰暫不直接決定 importance，後續可用來推薦比較候選。
- 人工問題比較任務本身的重要性，不包含當下等待時間，避免 BT 與 aging 重複學習等待因素。
- LM 是必要功能，但每次 lookup 不呼叫 LM。LM 失敗時先使用人工比較與上一版分數，待恢復後再擴充。

## 第三階段：接入獨立 Service（call-scheduler）

- [x] 接入 importance snapshot：Python 原子發布分數與 cwd/key 對應，Rust 在每次選擇前更新，使用 importance + 等待秒數 × rate。
- [x] 分別驗證人工 refit 發布新分數，以及 Rust 在工具已排隊後依新分數改變 grant 順序，保留原入列時間。已使用兩個真實 app-server 與固定模型回應驗證完整流程。
- Snapshot 最多 1 MiB，保留最後有效版本，忽略舊版；未命中使用 50 分加 aging。每個 cwd 使用一個明確 task key，較深路徑優先。
- 使用 `--snapshot PATH --bind CWD KEY` 在每次 refit 後發布，或使用 `export` 重發資料庫分數。SQLite 已提交但發布失敗時，舊檔案保留，需修正錯誤後重新 export。
- Snapshot 是分數傳遞層；共享 TCP service 負責跨 process queue。

- [x] 定義 localhost TCP JSONL 的 enqueue、grant、cancel、release；同連線重複操作不重複取得 slot。
- [x] 以 `client_id + CallKey` 隔離不同 process；由 service 記錄入列時間。
- [x] 將 Rust gate 接到 Python service，驗證外層 release 轉交。
- [x] 驗證多個獨立 process 共用 queue 與 slot，包含取消與斷線。
- 本次核心驗證：47 個 Python 測試、25 個 Rust 測試通過。共享 service 支援 JSONL trace。
- 共享 admission 已通過兩個獨立 Python process、Rust TCP adapter 與兩個真實 app-server 驗證。斷線回收 connection lease，但不會停止已執行的 tool；service 重啟前需停止 client 工作，尚無 lease recovery。
- [x] 驗證人工回答與 refit 會改變等待佇列的順序。
- [x] 使用假 LM、固定比較與可控 tool，測試人工修正、refit、grant、cancel 的完整流程。

## 第四階段：重新驗證排程策略

- [x] 重跑純 linear 的受控 replay，均勻與有界長尾各 10 個配對 seed；用實際 Python service 逐筆驗證 linear grant 一致。舊圖包含 confidence fallback，不沿用為本次結果。
- [x] 使用離線 replay 固定到達與服務時間；另以真實 Codex process 與固定 SSE mock 驗證接線。兩者證明不同層級的行為。
- [x] 加入固定 seed 的長尾 workload，例如多數短 call、少數 8 秒 call。
- [x] 長尾 workload 先補跑至每策略 10 組配對 seed，隨機化策略執行順序。10 輪不是顯著性保證。
- [x] 掃描 aging 2/4/8/16 秒與 linear 0.25/1/4/16 分每秒，保留每輪 trace。共 200 輪、16,000 calls。
- [x] 量測重要任務的平均等待與尾端延遲。
- [x] 量測背景任務的最大等待與平均 slowdown。
- Slowdown 定義為 `(等待時間 + 執行時間) / 執行時間`。
- 區分 slot 持有時間與背景 process 的實際生命週期。
- 每輪先計算各 task／session 的指標，再跨輪彙整，避免 call 多的 session 主導平均值。
- 背景等待與 slowdown 是公平性的代理指標。另記錄各 session 的獲准次數與最大無服務時間。
- 同一組工具服務時間、資源容量和模型設定才放在同一張排程 frontier。
- 相同 rate 下，已入列兩個任務的 linear 分差不隨時間改變；等待只會改變它們相對後來任務的優勢。參數掃描不保證形成平滑曲線。
- [x] 報告跨輪變異，只將不受其他點支配的結果列為觀察到的 frontier。
- 不以每策略 3 輪的結果宣稱統計顯著或通用最佳門檻。
- 不指定通用最佳 threshold。最新觀察 frontier 與 sample SD 見 `results/replay/frontier.svg`；每輪結果與 per-session 指標一併保存。

## 延後項目：MoM／Serving 層

- 狀態：Deferred。先完成第一至第四階段，不執行下列整合與部署工作。
- 恢復條件：核心 demo 與受控實驗完成後，再由使用者決定是否啟動 MoM。
- [ ] 複核既有 Modal 部署與 HTTP 200 紀錄，將 router／Modal 腳本整理入 repo。

- [ ] 先補 Responses API 相容性。此 repo 的 provider 只支援 `wire_api="responses"`，目前 router smoke 只收 Chat Completions。
- [ ] 驗證 `/v1/responses`、SSE streaming、tool call/result 與多輪上下文。Chat Completions HTTP 200 不等於 Codex 能使用。
- [ ] 固定 vLLM 與模型版本；若目標版本缺少所需 API，先處理相容層，不只改 base URL。
- [ ] 第一版明確使用 HTTP SSE；若啟用 Responses WebSocket，再驗證 router 對該 transport 的支援。
- [ ] 在 model request 建立路徑接入 importance 查詢與 header 注入。`ToolLifecycleContributor` 只管工具執行，不能代替這個掛點。
- [ ] 定義 `x-priority` 的整數語意與路由門檻。現有 mock 使用低 tier 數字優先；BT importance 則是高分優先，不能直接沿用 `priority <= 1`。
- [ ] 例如將 importance 映射為 `100 - round(importance)`，讓低數字表示高優先。vLLM 使用 `--scheduling-policy priority`，並在支援的請求端點設定 `priority`。
- [ ] 不將 vLLM 專用欄位直接轉送 OpenAI backend。Backend 身分驗證由 router 分別設定。
- [ ] 若要 model-level aging，在 router 自有 queue 實作。傳送一次靜態 priority 不代表 vLLM 會套用本專案的動態策略。
- [ ] 驗證模型名稱、context 上限與工具能力。現有 smoke 模型為 `Qwen/Qwen3-0.6B`、context 上限 4096，尚未證明能承載 Codex workload。
- [ ] 相容性測試通過後，再把 slow backend 指向 Modal URL，並設定 `model_providers.router.base_url`。
- [ ] 額度資料先使用 mock。接入真實額度前，定義來源、更新頻率與缺值處理；只對已驗證相容的任務啟用降級。
- [ ] 明確設定 OpenAI API 與 Modal backend 憑證，不假設 Codex 登入能直接授權 router 呼叫 OpenAI API。
- [ ] Demo 前先暖機，依預算設定 `scaledown_window`，不把 600 秒當固定需求。
- [ ] vLLM 加入 API key，client 使用對應憑證。
- [ ] 工具排程實驗先固定 backend；模型分流另外量測延遲、任務成功率與成本。不要把更換模型的效果算成排程收益。
- [ ] 記錄模型 API 費用、LM expansion 費用與 GPU 暖機／閒置費用。若增加成本維度，稱為多目標 Pareto frontier。
- 價格與免費額度以 [Modal 官方頁面](https://modal.com/pricing) 為準，每次付費實驗記錄日期與實際用量，不固定舊價格。
- 開發優先使用 mock 與 CPU。LM 使用假回應驗證流程，交付前需驗證真實 LM expansion。Demo 的 L4 配置需通過模型相容性測試。

## 黑客松順序

1. 修正 Rust gate，保留現有策略作為 baseline。
2. 建立 PairStore、人工比較與 BT fit，產生可查詢的 importance table。
3. 接入必要的 LM expansion，驗證來源權重與人工覆蓋。
4. Rust gate 接 Python service，展示「人工修正 → refit → 等待佇列順序改變」。
5. 使用固定 backend 重跑受控實驗，完成核心 demo 與結果整理。

## 本次驗收條件

- Service 自動問答已使用測試偏好 0.9 與真實 `gpt-5.6-luna` medium 完成兩次 refit，產生 9 個 key 的 importance；紀錄見 `results/live-learning-report.json`。
- 核心交付完成。重跑指令與限制見 `tools/task-scheduler/DEMO.md`。
- 真實雙 app-server 驗證使用 mock model 與 fake expansion；真實 OpenAI expansion 分開驗證，不宣稱全程使用真實模型。

- [x] 人工比較與 LM 擴充可產生 BT importance，來源覆蓋規則有測試。
- [x] Lookup miss 可問人，回答後 refit 能改變等待任務順序。
- [x] 至少兩個 Codex process 共用同一 queue，取消與釋放不會干擾其他呼叫。
- [x] 修正後策略有可重現的延遲與公平性比較，腳本與精簡結果已保存至 repo。
- MoM、Modal 部署、雙 backend 分流與成本最佳化不列入本次驗收。

## 查核依據

- 本機核對：`codex-rs/ext/scheduler/src/`、`codex-rs/model-provider-info/src/lib.rs` 與現有 scratchpad 腳本、trace。
- [vLLM scheduler 文件](https://docs.vllm.ai/en/latest/api/vllm/config/scheduler/)：priority 的數值越低，越優先處理。實際請求支援以部署版本為準。

## 核心交付收尾

- [x] 提供 GCP 三個 SSH 終端的 Demo 啟動器，管理背景 app-server 並保留學習資料。49 個 Python 測試通過，本機實際 binary 的啟動與 SIGHUP 清理驗證通過；尚未在 GCP VM 實跑。

- [x] 在 overall_testing 根目錄加入 `client-side-scheduling-demo.md`，補齊部署與啟動步驟。

- [x] 修正實際 cwd 與 snapshot 的 symlink 路徑比對，防止意外 fallback。

- [x] 自動比較未知 key，去重問題，以 reference 連接比較圖。
- [x] 完整驗證人工修正、LM 擴充、refit 與共享 grant 順序。
- [x] 使用兩個真實 app-server 驗證共享 slot 與取消。
- [x] 保存受控 workload、trace、Pareto 圖與 demo 說明至 repo。
- [x] 核對驗收與修正過時計畫狀態。

## 交付與後續

- 依 `tools/task-scheduler/DEMO.md` 重跑 demo。核心範圍收尾，不增加功能；故障恢復與 MoM 維持 deferred。
