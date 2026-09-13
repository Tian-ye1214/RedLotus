"""Two-hour installed-app acceptance with 200 real, paced user turns."""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import threading
import time
from pathlib import Path

from real_acceptance import (
    ApplicationDriver,
    WireAudit,
    isolate_environment,
    read_json,
    write_json,
)


def cache_usage(path):
    hits = misses = output = 0
    requests = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        usage = row.get("usage")
        if not usage or not row["path"].endswith("/chat/completions"):
            continue
        identifier = row["request_id"]
        if identifier in requests:
            continue
        requests.add(identifier)
        cached = usage.get(
            "prompt_cache_hit_tokens",
            usage.get("prompt_tokens_details", {}).get("cached_tokens", 0),
        )
        hits += cached
        misses += usage.get("prompt_cache_miss_tokens", usage["prompt_tokens"] - cached)
        output += usage["completion_tokens"]
    return dict(
        requests=len(requests),
        hit=hits,
        miss=misses,
        output=output,
        hit_ratio=hits / (hits + misses) if hits + misses else None,
    )


def expected(rows, threshold):
    return dict(
        rows=len(rows),
        total=sum(value for _, value in rows),
        North=sum(v for r, v in rows if r == "North"),
        South=sum(v for r, v in rows if r == "South"),
        threshold=threshold,
        eligible=sum(v for _, v in rows if v >= threshold),
    )


def request_for(index, rows, threshold):
    stage, step = divmod(index - 1, 5)
    if index == 1:
        return (
            "我们持续维护销售核验任务。原始文件是 sales.csv，只操作本项目。"
            "请创建标准库脚本 WorkDatabase/analyze.py，读取 sales.csv 和 WorkDatabase/rules.json，"
            "将真实计算结果写入 WorkDatabase/summary.json。结果字段固定为 rows（数据行数）、total、North、South、"
            "threshold（规则中的整数门槛）、eligible（amount 大于等于门槛的金额合计）。初始门槛为 20。"
            "真实运行脚本核验，之后复用它。地区和金额保持原值，不四舍五入，不安装依赖。"
            "本次任务审计约束名为 LOTUS-AUDIT-KEEP，后面仍须遵守；它只属于会话上下文，不要持久保存。"
        )
    if step == 0:
        return f"第 {stage} 阶段开始。请检查现有分析脚本，给出一个有依据的边界情况，并确认地区与整数金额的约束仍被保留。只检查，不改变计算规则。"
    if step == 1:
        region = "North" if stage % 2 == 0 else "South"
        amount = 13 + (stage * 17) % 89
        rows.append((region, amount))
        return f"第 {stage} 阶段新增一条销售：region={region}, amount={amount}。只在 sales.csv 追加这一行一次，保留其他行，然后运行分析脚本更新 summary.json。不要把实际金额提前写死到结果里。"
    if step == 2:
        return f"本阶段需求更正：金额门槛改为 {threshold}，包含恰好等于门槛的记录。请更新规则文件并真实运行分析脚本，保留其余输出字段和历史数据。"
    if step == 3:
        material = "\n".join(
            f"运行记录 {stage}-{row}: 地区={'North' if row % 2 else 'South'}；批次数值={(row * 31 + stage) % 997}；状态=待外部审计；销售原始值及计算规则不在此日志中修改。"
            for row in range(1600)
        )
        return (
            f"这是阶段 {stage} 的长篇临时参考日志，不能据此修改 sales.csv 或将待审计信息当成已验证经验。"
            "请概括两点数据质量注意事项，保留原任务约束，回答不超过两句话。\n"
            + material
        )
    return (
        f"第 {stage} 阶段收尾：实际读取 rules.json、sales.csv 并运行分析脚本，核验 summary.json。"
        "本阶段给出行数、总金额和当前门槛。若发现实现缺陷请修复并重跑，不改变原始数据。"
        "本轮任务的审计约束名是什么？从会话上下文回答。"
    )


async def run(root, frozen):
    import psutil
    import redlotus
    from loguru import logger
    from redlotus.infra.logger import ensure_configured
    from redlotus.infra.shared_http import close_all_clients

    installed = Path(redlotus.__file__).resolve()
    assert "site-packages" in installed.parts, installed
    ensure_configured()
    problems = []
    sink = logger.add(lambda message: problems.append(str(message)), level="WARNING")
    project = root / "中文 项目"
    project.mkdir(parents=True)
    rows = [("North", 10), ("South", 15), ("North", 25), ("South", 25)]
    with (project / "sales.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["region", "amount"])
        writer.writerows(rows)
    driver = ApplicationDriver(project, root / "evidence")
    process, samples, report = (
        psutil.Process(),
        [],
        dict(status="running", installed_package=str(installed)),
    )
    started = time.monotonic()
    try:
        await driver.start()
        threshold = 20
        for index in range(1, 201):
            due = started + (index - 1) * 7200 / 199
            while time.monotonic() < due:
                await asyncio.sleep(min(10, due - time.monotonic()))
            if (index - 1) % 5 == 2:
                threshold = 20 + 5 * (((index - 1) // 5) % 5)
            prompt = request_for(index, rows, threshold)
            before = time.monotonic()
            answer = await driver.say(prompt)
            if index == 1 or (index - 1) % 5 in (1, 2, 4):
                actual = read_json(project / "WorkDatabase/summary.json")
                assert actual == expected(rows, threshold), (
                    index,
                    actual,
                    expected(rows, threshold),
                )
                with (project / "sales.csv").open(
                    encoding="utf-8-sig", newline=""
                ) as stream:
                    saved = [
                        (r["region"], int(r["amount"])) for r in csv.DictReader(stream)
                    ]
                assert saved == rows, (index, "Original rows changed or duplicated")
            if (index - 1) % 5 == 4:
                assert "LOTUS-AUDIT-KEEP" in answer, (
                    index,
                    "Early constraint was lost",
                )
            assert not problems, "\n".join(problems[-3:])
            assert not driver.system._memory.last_error, (
                driver.system._memory.last_error
            )
            samples.append(
                dict(
                    turn=index,
                    elapsed=time.monotonic() - started,
                    latency=time.monotonic() - before,
                    rss=process.memory_info().rss,
                    threads=threading.active_count(),
                    handles=process.num_handles(),
                    subprocesses=len(process.children(recursive=True)),
                    memory=await driver.system._memory.short_term_snapshot(),
                )
            )
            report.update(
                turns=index,
                seconds=time.monotonic() - started,
                samples=samples,
                cache=cache_usage(root / "wire.jsonl"),
                warning_error_count=len(problems),
            )
            write_json(root / "report.json", report)
            if index % 25 == 0:
                await driver.system.process_cli_line(
                    "/usage", driver.state, wait_for_turn=True
                )
        await driver.close()
        observed = driver.system._memory.observations
        report["final_memory"] = dict(
            observed_turns=len(observed.order()), consumed_turns=observed.cursor()
        )
        assert (
            report["final_memory"]["observed_turns"]
            == report["final_memory"]["consumed_turns"]
        )
        assert not driver.system._memory.last_error
        assert not driver.system._memory_factory.handles
        assert not driver.system._orchestrator.factory.handles
        assert not problems, "\n".join(problems)
        assert time.monotonic() - started >= 7200
        assert driver.state.history.compress_summary_state, (
            "Actual automatic compression not observed"
        )
        assert (
            hashlib.sha256(Path(frozen["config_path"]).read_bytes()).hexdigest()
            == frozen["config_hash"]
        )
        report["cache"] = cache_usage(root / "wire.jsonl")
        assert report["cache"]["hit_ratio"] > 0.9, report["cache"]
        report["status"] = "passed"
    except Exception as exc:
        import traceback

        report.update(status="failed", error=str(exc), traceback=traceback.format_exc())
        print(report["traceback"], flush=True)
    finally:
        try:
            await driver.close()
        except Exception as exc:
            report.update(status="failed", shutdown_error=str(exc))
        await close_all_clients()
        if problems:
            report.update(
                status="failed",
                error="Unexpected WARNING/ERROR during the run or shutdown",
            )
        report.update(
            seconds=time.monotonic() - started,
            warning_error_count=len(problems),
            problems=problems,
        )
        if (root / "wire.jsonl").exists():
            report["cache"] = cache_usage(root / "wire.jsonl")
        write_json(root / "report.json", report)
        logger.remove(sink)
    return report["status"] == "passed"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    root = parser.parse_args().root.resolve()
    root.mkdir(parents=True)
    frozen = isolate_environment(root)
    WireAudit(root / "wire.jsonl").install()
    raise SystemExit(0 if asyncio.run(run(root, frozen)) else 1)


if __name__ == "__main__":
    main()
