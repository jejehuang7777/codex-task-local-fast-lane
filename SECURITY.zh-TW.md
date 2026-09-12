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
10. 在取得 startup preimages 時，同時綁定 fixture、輸出父目錄與既有目的檔案的 device／inode／file type；
11. 透過不跟隨 symlink 的目錄把手準備、備份與替換輸出，不在 commit 時重新解析可被更換的路徑字串；若綁定的路徑樹改變，回傳 `RECOVERY_REQUIRED`；
12. 變更 fixture 以前先準備並雜湊所有 allowlisted 輸出，保留既有目的檔案權限，並在持有每個 fixture 的專用 lock 時寫入 transaction journal；
13. 後續替換失敗時復原先前已替換的輸出；若復原不完整，保留備份並將結果標成需要人工處理；
14. 以專用 process group 啟動 verifier／model 指令並記錄 timeout 清理證據；POSIX 下無法證明刻意用 `setsid`／`setpgid` 脫離的後代已結束，因此逾時結果 fail closed，不做 copyback；
15. 在 durable copyback journal 記錄 fixture 與 source preimages；若 journal 沒有可信終態，之後同一 fixture 的執行會在模型啟動前被 restart fence 擋下；
16. 驗證 journal digest、schema、transaction identity、路徑 identity、allowed writes、preimages、artifact／檔案狀態一致性與終態證據；
17. 把可信的終態 journal digest／status 封存在另一份 owned run marker；只有重新計算的現場 fixture hashes 與 journal 終態完全相符時才寫 seal；journal 與 marker 都先 fsync 檔案、rename，再 fsync 所在目錄；
18. 準備檔、備份目錄項、fixture replace、rollback replace／unlink、cleanup unlink 與新建目錄的變更，都先完成對應 fsync，才讓 transaction journal 前進到下一個 durable 狀態；
19. copyback 開始以前，會逐層 durable 建立 state／run 目錄祖先並 durable 寫入初始 owned run marker，避免 fixture replace 已留存、但 restart-fence 整個目錄項因未 sync 而消失。

安裝或更新 profile 時也會拒絕 symlink／非一般檔案目的地，並使用同一種不跟隨連結的目錄把手替換方式。
路徑標籤或 filesystem identity 衝突一律使操作失敗，不會成為跟隨替代目標的理由。

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

多檔 copyback 是應用層的 prepare／commit／rollback protocol，不是整個檔案系統的原子 transaction。
一般替換失敗會復原並寫入 journal；commit 期間若遇到斷電或儲存裝置錯誤，仍可能需要依 transaction
證據中的 `.fast-lane-*.bak` 手動復原。`ROLLBACK_FAILED` 與 `RECOVERY_REQUIRED` 都不是可採用的成功結果。

## 回報安全漏洞

只能在 GitHub issue 提供不敏感的重現資料。不要附上憑證、私人倉庫、rollout logs 或個人資料。
若要私下回報，請在該功能開啟後使用 GitHub private vulnerability reporting。
