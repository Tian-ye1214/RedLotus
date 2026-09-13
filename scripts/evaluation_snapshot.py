"""Export inspectable evidence from the real application's saved journals."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def export(root: Path):
    from redlotus.infra.persist_utils import file_lock

    summaries = []
    for path in sorted((root / "sessions").rglob("coordinator*_ModelMessages.json")):
        with file_lock(path):
            saved = json.loads(path.read_text(encoding="utf-8"))
        messages = saved["model_messages"]
        journal = path.with_name(path.name.replace("_ModelMessages.json", ".jsonl"))
        with file_lock(journal):
            original = (
                [
                    json.loads(line)["message"]
                    for line in journal.read_text(encoding="utf-8").splitlines()
                ]
                if journal.exists()
                else messages
            )
        body = []
        for message in original:
            for part in message["parts"]:
                kind = part["part_kind"]
                if kind == "thinking":
                    continue
                content = part.get("content", part.get("args", ""))
                if not isinstance(content, str):
                    content = json.dumps(content, ensure_ascii=False, indent=2)
                body.append(
                    f"### {message['kind']} / {kind} / {part.get('tool_name', '')}\n\n{content}"
                )
        readable = path.with_name(
            path.stem.replace("_ModelMessages", "") + "-readable.md"
        )
        readable.write_text("\n\n".join(body), encoding="utf-8")
        responses = [m for m in messages if m["kind"] == "response"]
        summaries.append(
            {
                "path": str(path),
                "readable": str(readable),
                "messages": len(messages),
                "responses": len(responses),
            }
        )
    rows, usage = [], []
    for wire in (root / "evidence").rglob("wire.jsonl"):
        with wire.open(encoding="utf-8-sig") as stream:
            for line in stream:
                if not line.endswith("\n"):
                    continue
                row = json.loads(line)
                row["wire_file"] = str(wire.relative_to(root))
                rows.append(row)
                if row.get("usage") and row["path"].endswith("/chat/completions"):
                    usage.append(row)
    unique = {(row["wire_file"], row["request_id"]): row["usage"] for row in usage}
    tokens = sum(item.get("prompt_tokens", 0) for item in unique.values())
    cached = sum(
        item.get(
            "prompt_cache_hit_tokens",
            item.get("prompt_tokens_details", {}).get("cached_tokens", 0),
        )
        for item in unique.values()
    )
    audit_path = root / "reports/compression-audit.json"
    audit = (
        json.loads(audit_path.read_text(encoding="utf-8"))
        if audit_path.exists()
        else []
    )
    report = {
        "sessions": summaries,
        "model_responses": len(unique),
        "input_tokens": tokens,
        "cache_read_tokens": cached,
        "cache_ratio": cached / tokens if tokens else None,
        "output_tokens": sum(
            item.get("completion_tokens", 0) for item in unique.values()
        ),
        "compression_artifacts": len(
            list((root / "sessions/compression").glob("*/compressor_output.md"))
        ),
        "compression_count": sum(row.get("accepted", False) for row in audit),
        "completed_streams_without_usage": [
            {key: row[key] for key in ("wire_file", "request_id", "time")}
            for row in rows
            if row.get("kind") == "usage" and not row.get("usage")
        ],
    }
    (root / "reports/progress.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    links = "\n".join(
        f"- [会话 {index + 1}]({item['readable']}) · {item['messages']} 条模型消息"
        for index, item in enumerate(summaries)
    )
    (root / "00-INDEX.md").write_text(
        f"# RedLotus 用户场景校准\n\n正在进行，未将辅助检查当成真实通过。\n\n{links}\n\n- [实际用量与进度](reports/progress.json)\n- [原始提交](evidence/submissions.jsonl)\n- [配置基线](configuration-baseline.json)\n- [固定全量题库](reports/full-evaluation-manifest.json)\n- [问题清单](reports/findings.json)\n\n当前实际模型响应：{len(unique)}；压缩产物：{report['compression_artifacts']}；经独立审查计入验收的自动压缩：{report['compression_count']}。\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {key: value for key, value in report.items() if key != "sessions"},
            ensure_ascii=False,
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    export(parser.parse_args().root)


if __name__ == "__main__":
    main()
