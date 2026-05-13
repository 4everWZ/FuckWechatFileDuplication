# Fuck_Wechat_File_Duplication

微信 PC 文件重复落盘治理脚本。核心策略：

- 按月份增量扫描：日常只扫最近 N 个月；首次或每月指定日期做全量扫描。
- `xxhash` 快速内容指纹。
- 重复文件替换为 NTFS 硬链接：路径保留，实际内容只占一份空间。
- SQLite 索引：避免每次全量重新 hash。
- Windows 计划任务定时运行。

## 目录

```text
Fuck_Wechat_File_Duplication/
  fuck_wechat_file_duplication.py
  config.json
  requirements.txt
  setup_venv.bat
  run_dry_run.bat
  run_full_once.bat
  run_once.bat
  run_watch.bat
  install_task.ps1
  install_watch_task.ps1
  uninstall_task.ps1
  uninstall_watch_task.ps1
```

## 1. 修改配置

打开 `config.json`，把 `roots` 改成你的微信文件根目录。例如：

```json
{
  "roots": [
    "D:\\xwechat_files"
  ]
}
```

你也可以更激进地指定到账号目录：

```json
{
  "roots": [
    "D:\\xwechat_files\\wxid_"
  ]
}
```

## 2. 安装依赖

双击或在 PowerShell 里运行：

```powershell
.\setup_venv.bat
```

## 3. 先 dry-run

```powershell
.\run_dry_run.bat
```

它只输出计划动作，不会删除或硬链接任何文件。

## 4. 首次全量扫描

```powershell
.\run_full_once.bat
```

首次会建立 SQLite 索引，之后日常运行会快很多。

## 5. 安装定时任务

PowerShell 中运行：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\install_task.ps1
```

默认每周日 03:30 运行 `run_once.bat`。日常模式只扫描最近 `recent_months` 个月；如果运行当天刚好是 `monthly_full_scan_day`，则做一次全量扫描。

## 6. 安装实时监听任务

`run_watch.bat` 会长期监听微信文件目录。微信新建或修改文件后，脚本会等文件稳定，再到 `source_roots` 里按相同大小懒查候选；只有 hash 和逐字节内容都一致时，才把微信副本替换为指向原始文件的硬链接。

实时监听模式不会启动时全树扫描清理旧 backup 或临时链接；中断恢复仍由每周兜底任务处理。

PowerShell 中运行：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\install_watch_task.ps1
```

这个任务在当前用户登录时启动，不会杀微信。要马上手动启动，可以运行：

```powershell
.\run_watch.bat
```

如果系统策略不允许创建登录计划任务，安装脚本会自动退回到当前用户 Startup 快捷方式，效果仍然是登录后启动监听。

## 7. 卸载定时任务

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\uninstall_task.ps1
.\uninstall_watch_task.ps1
```

## 配置说明

```json
{
  "source_roots": [
    "~\\Downloads",
    "~\\Desktop",
    "D:\\Paper"
  ],
  "min_size_bytes": 65536,
  "skip_recent_hours": 72,
  "recent_months": 3,
  "monthly_full_scan_day": 1,
  "process_non_month_dirs": true,
  "exclude_dir_names": [],
  "exclude_file_extensions": [],
  "kill_wechat_before_run": false,
  "verify_before_link": true,
  "byte_compare_before_link": true,
  "same_volume_only": true,
  "hash_buffer_mb": 8,
  "watch_stable_seconds": 8,
  "watch_poll_seconds": 1,
  "watch_timeout_seconds": 120
}
```

关键项：

- `source_roots`: 实时监听模式用来查找“原始文件”的目录。默认 `Downloads`、`Desktop`、`D:\Paper`。
- `min_size_bytes`: 小于该大小的文件跳过。默认 64KB，避免浪费时间处理碎片文件。
- `skip_recent_hours`: 跳过最近 N 小时内修改过的文件，避免处理微信仍在写入的文件。
- `recent_months`: 日常增量扫描最近 N 个月的 `YYYY-MM` 文件夹。
- `monthly_full_scan_day`: 每月几号自动全量扫描。
- `process_non_month_dirs`: 是否处理不在月份目录里的文件。默认 true，比较激进。
- `exclude_dir_names`: 要排除的目录名。默认空，符合“见蟑螂就打”的策略。
- `exclude_file_extensions`: 要排除的扩展名。默认空。
- `kill_wechat_before_run`: 配置层面的杀微信开关。`run_once.bat` 已经通过 `--kill-wechat` 开启。
- `verify_before_link`: 硬链接前再次检查待替换文件和候选文件没有变化。
- `byte_compare_before_link`: 硬链接前逐字节确认两份文件当前内容一致，避免 SQLite 索引陈旧时误链接。
- `same_volume_only`: 只在同一分区内硬链接。NTFS 硬链接本身也要求同卷。
- `watch_stable_seconds`: 实时监听模式里，文件连续稳定多少秒后才处理。
- `watch_poll_seconds`: 等待文件稳定时的检查间隔。
- `watch_timeout_seconds`: 单个监听事件等待文件稳定的最长时间。

## 注意

硬链接不是快捷方式。多个路径指向同一份文件内容，所以重复副本不再额外占空间。

脚本只在发现内容完全相同后替换重复文件。它不是“按文件名删除”，也不是“按时间删除”。

替换重复文件时不会先改走原文件名。脚本会先创建 `<原文件名>.dedupe_link.<pid>.<timestamp>` 临时硬链接，再用 `os.replace` 原子替换微信副本；如果替换失败，原文件仍在原路径，临时硬链接会重试删除。

每周兜底任务会清理崩溃遗留的 `.dedupe_link...` 临时硬链接，也会恢复旧版 `.dedupe_backup...`：原路径不存在就还原；原路径存在且内容一致就清理备份；内容冲突则保留备份并写 warning。恢复逻辑兼容旧版 `.原文件名.dedupe_backup.<pid>.<timestamp>` 临时备份。

默认跳过最近 72 小时的新文件，主要是为了避开微信正在写文件、刚接收文件、半落盘文件。

实时监听不是驱动层拦截。微信仍然会先创建副本；脚本会在文件稳定后尽快把副本替换为硬链接。原始文件和微信副本必须在同一个 NTFS 分区，才能硬链接。

不要把微信安装目录和聊天数据目录混在同一个自动任务里。如果要处理安装目录，建议另建单独配置，微信更新后手动跑 dry-run 再决定。
