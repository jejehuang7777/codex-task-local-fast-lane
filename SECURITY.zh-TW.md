# 安全模型

## 信任邊界

Launcher 信任本地操作者提供的 fixture 確實可丟棄、且不含敏感資料。它不會自動對原始碼分類、
偵測個人資料，或證明 verifier 本身無害。

Fast 執行組會：

1. 驗證明確的 manifest；
2. 拒絕以 symlink 宣告的路徑與 parent traversal；
3. 只把已宣告的 task／runtime 輸入複製到新生成的 Git 邊界；
4. 要求 `task-local-fast-lane` 權限 profile，並禁止本地指令使用網路；
5. 關閉已知的 browser／app／plugin／memory／agent／hook 功能、使用者基礎 `config.toml` 中所有可列舉的 MCP servers，並在已審查的 profile 與指令 override 中都明確設定 `web_search = "disabled"`；
6. 從本地 Codex rollout 記錄回讀實際使用的權限 profile 與 staging 工作目錄；
7. 在同一指定權限 profile 下執行精確 verifier；
8. 拒絕未預期變更檔案、驗證失敗、沒有產生變更或來源 preimage 衝突；
9. 在 hashing 前拒絕模型執行後才出現的 symlink 輸出、symlink ancestors、非一般檔案與解析後位於 staging root 外的路徑，並在 copyback 前立即重複這項檢查；
10. 持有每個 fixture 的專用 lock 時，只寫回 manifest allowlist 內的輸出檔案。

## 重要限制

Codex 權限 profiles 約束的是本地指令執行，不是 browser、connectors、MCP servers、computer use、
cloud execution 或 Codex 服務連線的全域政策。這些介面有自己的控制方式。Launcher 會關閉它能發現的本地介面，
但受管理或未來新增的整合可能不會被它看見。

因此，這個 beta 不得接收憑證、`.env` 檔案、private keys、客戶／付款／健康資料、私人日記或生產環境存取權。
請先使用內附的合成範例。

## A/B 基準組

Ordinary 組刻意允許讀取較多的倉庫上下文，但 ordinary 與 fast 都使用同一個無網路本地權限 profile，
並只在含有明確宣告安全檔案的新生成 fixtures 中執行。`compare` 永遠不會把任何一邊寫回來源 fixture。

任何一邊啟動前，model-free preflight 會拒絕舊版 sandbox keys，要求已審查 profile 將 hosted web search 解析為 `disabled`，
並實際測試 profile 允許 workspace 寫入，同時拒絕外部讀取、外部寫入、網路使用與未列入的環境變數機密。
模型 rollout 也必須獨立顯示指定的 profile 與精確 staging 目錄。缺少任何證據都會使執行失敗。

## 並行與復原

每個 fixture 的專用 lock 會防止兩個 launcher 同時寫入同一來源。程式異常中斷可能在
`~/.codex-fast-lane/locks/` 留下過期 lock。手動移除該單一 lock 目錄前，請先確認沒有相關行程仍在執行。

## 回報安全漏洞

只能在 GitHub issue 提供不敏感的重現資料。不要附上憑證、私人倉庫、rollout logs 或個人資料。
若要私下回報，請在該功能開啟後使用 GitHub private vulnerability reporting。
