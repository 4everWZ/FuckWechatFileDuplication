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
  install_task.ps1
  uninstall_task.ps1
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

默认每天 03:30 运行 `run_once.bat`。日常模式只扫描最近 `recent_months` 个月；每月 `monthly_full_scan_day` 做一次全量扫描。

## 6. 卸载定时任务

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\uninstall_task.ps1
```

## 配置说明

```json
{
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
  "hash_buffer_mb": 8
}
```

关键项：

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

## 注意

硬链接不是快捷方式。多个路径指向同一份文件内容，所以重复副本不再额外占空间。

脚本只在发现内容完全相同后替换重复文件。它不是“按文件名删除”，也不是“按时间删除”。

默认跳过最近 72 小时的新文件，主要是为了避开微信正在写文件、刚接收文件、半落盘文件。

不要把微信安装目录和聊天数据目录混在同一个自动任务里。如果要处理安装目录，建议另建单独配置，微信更新后手动跑 dry-run 再决定。
