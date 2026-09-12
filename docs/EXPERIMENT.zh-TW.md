# 最初實驗

日期：2026-09-11

第一輪普適性 canary 使用兩個本地編寫的合成修復案例，比較普通倉庫執行與隔離的 task-local runner。

| 案例 | 模型／推理強度 | Ordinary input | Fast input | Input 變化 | Ordinary tools | Fast tools | 耗時變化 |
|---|---|---:|---:|---:|---:|---:|---:|
| Python retry | gpt-5.6-sol / medium | 118,614 | 90,071 | -24.06% | 5 | 4 | -35.75% |
| Node path prefix | gpt-6-astra / high | 107,324 | 65,176 | -39.27% | 7 | 5 | -42.58% |

四個執行組全部：

- 返回模型 exit code `0`；
- 通過同一組七項精確測試；
- 只修改已宣告的實作檔案；
- 沒有產生未預期的變更檔案。

兩個 fast 執行組也都在 allowlist copyback 之前，產生了指定權限 profile 與隔離工作目錄的 runtime 證據。

## 這些結果不能證明什麼

- 樣本只有兩個合成任務。
- 兩案都先跑 ordinary，因此沒有平衡順序與 cache 影響。
- 兩個案例使用不同模型。
- 這個實驗沒有測量 ChatGPT Pro 額度行為。
- 觀察到的百分比不是平均值、預測或對其他使用者的保證。

因此，公開的 `compare` 指令會將單次比較標記為 `DIRECTIONAL_ONE_PAIR`，並支援反轉兩邊的執行順序。

## 公開 runner 自測

打包後的 runner 使用 `gpt-5.6-luna / low` 與 `fast-first` 順序，在內附 Python fixture 上完成端到端測試。

| 執行組 | 驗證 | 變更檔案 | Input | 工具呼叫 | 耗時 |
|---|---|---|---:|---:|---:|
| Ordinary | 7/7 PASS | 只有 `src/retry_policy.py` | 110,524 | 6 | 45.148s |
| Fast | 7/7 PASS | 只有 `src/retry_policy.py` | 65,110 | 3 | 20.052s |

兩邊的 allowlisted 輸出逐位元組一致，原始 fixture 沒有被修改，結果為 `HELPED / DIRECTIONAL_ONE_PAIR`。
在這單次配對中，fast 少用 41.09% input tokens，耗時少 55.59%。這些百分比只描述這次記錄，
不是平均值、因果估計、額度預測或對其他測試者的承諾。

在成功配對之前，曾有一個 MCP override key 產生無效 TOML 語法。Preflight 在啟動任何模型前就拒絕執行，
並返回 `INVALID_COMPARISON`。修正 key writer、回讀確認已設定的 MCP servers 全部關閉後，才重新執行 A/B 自測。
被保留的失敗 receipt 用來證明：當邊界無法證明時，程式會停止，而不是靜默放寬權限。
