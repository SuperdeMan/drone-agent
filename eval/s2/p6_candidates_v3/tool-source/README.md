# 开发工具源码快照

这里保存三轮素材工作实际使用的 **20 份源码原始字节及 1 份测试所需的开发条目夹具**。[manifest.json](manifest.json)记录原文件名、数据目录相对路径、长度与 SHA-256；第三轮源码还与[验证回执](../verification.json)绑定。`.txt` 后缀与 `-text` 属性用于归档，避免格式化或 Git 换行转换改变历史摘要。

- `round1/`：UAVVaste 获取、元数据 / 图像审计与清单导出。
- `round2/`：RescueNet 获取、图像检查与清单导出。
- `round3/`：开发输入构建、复核页、Node / 浏览器测试、云端诊断与本地回放。

这些是一次性开发脚本的历史快照，尚未改造成可移植的仓库命令。部分脚本写死了 `D:/drone-agent-data/` 和原工作树路径，浏览器检查使用本地端口 8879；原图、掩码、输入 JPEG 和截图仍在仓库外。源码存在不代表素材已随仓库交付。

## 校验与恢复

在仓库根目录校验全部源码摘要：

```powershell
@'
import hashlib, json
from pathlib import Path
root = Path('eval/s2/p6_candidates_v3/tool-source')
manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
for row in manifest['files'] + manifest['fixtures']:
    data = (root / row['snapshot']).read_bytes()
    assert len(data) == row['bytes']
    assert hashlib.sha256(data).hexdigest() == row['sha256'], row['snapshot']
print(f"{len(manifest['files'])} sources and {len(manifest['fixtures'])} fixtures verified")
'@ | uv run python -
```

恢复时按 `original_relative_path` 放入一个新的仓库外目录，并去掉最后的 `.txt` 后缀；不要覆盖原数据目录里的文件。仅检查复核核心时，把 `round3/review-core.js.txt`、`round3/test_review_core.cjs.txt` 和 `round3/audit/review-bundle.json.txt` 恢复为原文件名与相对目录后运行 `node --test test_review_core.cjs`，不需要图像或模型。夹具包含源标签与 AI 建议，真实人工标签仍为空；不是独立真值。

完整复核页需要按开发输入清单恢复相对目录中的原图、`assets/`、`targets/` 和 `data.js`。`summarize_diagnostic.py` 是历史结果的本地严格回放，但仍会写出摘要与预览；运行前应检查其固定输出路径。`run_cloud_diagnostic.py` 会创建云端诊断任务并产生新的付费模型调用，不能当成离线校验命令自动重跑。

本批只补充源码可追溯性，未改提示、输入或历史模型回答，也未把旧结果转记为当前提交的模型效果。
