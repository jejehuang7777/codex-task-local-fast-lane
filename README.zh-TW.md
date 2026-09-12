# Codex 任務局部快線

[English](README.md) | [繁體中文](README.zh-TW.md)

這是一個 **fail-closed（無法證明安全就停止）**的 beta 工具，用來測量與執行
**小型、語義完整、可獨立驗證的 Codex 任務**，避免每一輪模型往返都帶著無關的專案歷史。

它不是「Codex 無限流量」技巧，不會使用 ChatGPT 網頁版額度、繞過計費，
也不保證一定省 Token。它會建立一份明確的本地任務包，只將 allowlist 內的檔案
放入臨時隔離目錄，要求指定的無網路權限 profile，執行精確驗證，並在證據不完整時拒絕寫回原始檔案。

## 測試者會得到什麼

`compare` 會在兩個可丟棄副本中執行同一個合成任務：

- `ordinary`：使用同一個本地安全 profile，另外提供事先宣告為安全的基準專案上下文；
- `fast`：在專用權限 profile 下，只提供任務包與必要的 runtime 檔案。

報告會列出精確驗證結果、變更檔案範圍、兩邊輸出是否逐位元組一致、Codex 回傳的
每一項數字型用量（分開列出）、工具呼叫次數與總耗時。結果等價的判斷基礎是兩邊都
通過同一條程式化驗證，且變更範圍都合規；逐位元組一致是更強的診斷訊號，不是硬性
條件，因為同一組精確測試可能容許多種正確實作。最後結果只有三種：

- `HELPED`：兩邊結果等價，而 fast 使用較少 input tokens；
- `NO_CLEAR_GAIN`：兩邊結果等價，但這一組沒有省下 input tokens；
- `INVALID_COMPARISON`：驗證、變更範圍、輸出回讀或用量證據失敗。即使 Token 較少，也不得推翻這個結果。

單次 A/B 只是方向性證據。要得到較強結論，請把執行順序反過來再跑一次。

如果要宣稱某一類任務的正確率，必須在執行前先定義 benchmark 與驗收方式。內附範例只測試
harness 與一個極小修復，不是一般程式能力的 benchmark。

這個 beta 不會自動選模型。A/B 兩邊都使用測試者明確指定的相同模型與推理強度。
「日常模型失敗後自動升級強模型」屬於下一階段的獨立實驗，不在目前的安全或省量宣稱內。

## 使用要求

- Codex CLI `0.138.0` 或更新版本，並已登入自己的帳號；
- Python `3.11` 或更新版本；
- 第一個 beta 目前支援 macOS 與 Linux；
- 一個可丟棄、不含敏感資料的測試 fixture。

Codex 權限 profiles 目前仍是 beta 功能。Codex 的 profile schema 變更時，這個專案可能需要同步更新。
請參考 OpenAI 官方的
[權限指南](https://developers.openai.com/codex/permissions) 與
[進階設定指南](https://developers.openai.com/codex/config-advanced)。

## 五分鐘安全試跑

> **用量警告：**`compare` 會啟動兩次 hosted Codex 模型執行，並將已宣告的合成任務檔案
> 傳送給 Codex 服務。兩邊都可能消耗帳號額度或產生計費用量。無網路 profile 限制的是本地工具指令，
> 不是 Codex 服務本身的連線。

```bash
git clone https://github.com/jejehuang7777/codex-task-local-fast-lane.git
cd codex-task-local-fast-lane
python3 fastlane.py install-profile
python3 fastlane.py preflight examples/python-retry \
  --semantic-self-containment PASS \
  --packet-complete YES \
  --external-dependency NONE
python3 fastlane.py compare examples/python-retry \
  --semantic-self-containment PASS \
  --packet-complete YES \
  --external-dependency NONE \
  --model gpt-5.6-sol \
  --effort medium
```

內附範例刻意保留一個單行 bug，並有七項精確測試。`compare` 不會修改原始範例，
證據會儲存在 `~/.codex-fast-lane/runs/`。

為了降低執行順序對結果的影響，請先記下第一次結果的 `order`，再以相反順序執行：

```bash
python3 fastlane.py compare examples/python-retry \
  --semantic-self-containment PASS \
  --packet-complete YES \
  --external-dependency NONE \
  --order fast-first
```

## 直接交給 Codex 的一句話

> 請用這個專案的 `compare` 指令執行內附的合成範例。兩邊必須使用完全相同的模型與推理強度。
> 不要使用我的真實倉庫、憑證、帳戶資料、部署環境或網路。請用白話解釋最後的 `HELPED`、
> `NO_CLEAR_GAIN` 或 `INVALID_COMPARISON`，並告訴我 `comparison.json` 的路徑。

## 適用的任務

只有下列每一項都為真時，才能使用快線：

- fixture 可丟棄，而且不含敏感資料；
- `TASK.md` 包含完整的決策上下文；
- 所有可讀與可寫檔案都已逐一列出；
- 已提供一條精確、可離線執行的驗證指令；
- 任務不需要網路、機密、帳戶資料、外部記憶、owner 或商業判斷、部署、runtime、權限或公開狀態變更；
- 修改只發生在本地、可還原，而且限定於該 fixture。

只要有任何一項不確定，就不要強行填成 `PASS`；請回到普通 Codex 任務。

## 不適用的任務

不要將這個 beta 用於生產環境部署、客戶／付款／健康資料、憑證、私人日記、瀏覽器操作、MCP 流程、
資料庫變更、安裝套件、大範圍重構，或任何需要依賴未寫明歷史才能判斷「正確答案」的任務。

## 測試自己的 fixture

複製 `examples/python-retry/FAST_LANE.toml`，並把每一份清單寫精確。普通組可能會讀、
但 fast 組不會收到的額外**非敏感**上下文，放在 `baseline_reads`。已存在的寫入目標也必須出現在
`allowed_reads`。

執行 `compare` 或 `run` 前先跑 `preflight`。只有 `run` 可以把通過驗證、且出現在 allowlist 的輸出寫回原始 fixture；
`compare` 永遠不會寫回。

`run` 會先準備並雜湊全部輸出，再開始碰原始 fixture；寫回時保留既有檔案權限，並留下 copyback transaction journal。
若其中一個檔案替換失敗，程式會復原先前已替換的檔案，並回傳失敗 receipt。指令逾時時，launcher 會終止本輪啟動的
process group，並把實際清理證據寫入 receipt。POSIX 的 process group 無法證明刻意用 `setsid`／`setpgid` 脫離的後代
程序也已結束；因此 receipt 會把整棵程序樹的清理標成「未證明」，逾時的 arm 直接 fail closed，不做 copyback。

如果 launcher 在多檔 commit 中途被強制終止，尚未完成的 transaction journal 會保留下來。之後對同一 fixture 執行
`preflight`、`compare` 或 `run` 時，程式會在持有 fixture lock 的情況下先查 journal，並在啟動模型或接受新 preimage
以前回傳 `RECOVERY_REQUIRED`。這是一道重啟閘門，不代表程式會自行猜測如何復原。
閘門會驗證 journal digest、schema、transaction id、write set、preimage、artifact／檔案狀態一致性與終態證據；只把
殘缺 journal 的 status 改成 `ROLLED_BACK`，不會打開閘門。
正常終態還必須由另一份 owned run marker 封存 journal 狀態與 digest。crash 後即使把 status、restore 清單、hash 與
digest 改成彼此一致，缺少這份 seal 仍然是 `RECOVERY_REQUIRED`。launcher 寫 seal 前會重新計算現場 fixture hashes，
必須和 journal 宣稱的 terminal hashes 完全一致。

## 卸載

如果要透過單一、會核對所有權的路徑移除 beta profile 及它生成的 staging／receipt 目錄，請執行：

```bash
python3 fastlane.py uninstall
```

卸載器只會移除這份原樣出貨的 profile，以及帶有本 beta 所有權標記的 run 目錄。
如果它發現 profile 被修改、存在執行中／過期 lock，或出現不認識的項目，就會停止而不自行猜測。
它會保留你原本的 Codex 設定、登入狀態、倉庫、憑證與其他檔案。

Profile 路徑：

```text
~/.codex/task-local-fast-lane.config.toml
```

執行證據會獨立保留在 `~/.codex-fast-lane/`，直到執行 `uninstall`。

## 目前證據

最初實驗在兩個合成案例中觀察到較低的 input tokens 與耗時；四個執行組全部通過同一組七項精確測試，
而且只修改已宣告的檔案。這兩案**不能**用來估計一般使用者的平均節省幅度。

打包後的公開 runner 也完成了一次同模型、單方向的 A/B 自測；這是 smoke test，不是節省保證。
精確數字、第一次 fail-closed 的無效實驗與限制，請見
[`docs/EXPERIMENT.zh-TW.md`](docs/EXPERIMENT.zh-TW.md)。

## 安全

在內附範例以外進行測試前，請先閱讀 [`SECURITY.zh-TW.md`](SECURITY.zh-TW.md)。
權限 profiles 限制的是本地指令執行；瀏覽器、connectors、MCP 與其他工具各有獨立控制。
這個 launcher 會關閉它所知的本地功能與可列舉的使用者 MCP servers，但這個 beta 仍只允許使用不含敏感資料的 fixtures。

## 一句話說完

這不是「無限流量」或偷接網頁版額度；它是把一個可丟棄、無隱私、有精確測試的小任務切成安全 A/B，
讓程式自己判斷有沒有真的省，而不是靠感覺。
