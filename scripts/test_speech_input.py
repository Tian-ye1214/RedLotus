"""Manual microphone diagnosis with repeated, explicitly started recordings."""

from __future__ import annotations

import argparse
import asyncio

from redlotus.TTS import ModelKind, ModelStage, NoSpeechDetected, SpeechError
from redlotus.TTS.asr import AudioCapture, StreamingRecognizer
from redlotus.TTS.audio import AudioDevices
from redlotus.TTS.service import SpeechService


class SpeechInputTest:
    def __init__(self, seconds: float, device_index: int | None = None):
        self.seconds = seconds
        self.device_index = device_index

    async def choose_device(self):
        devices = await AudioDevices.inputs(refresh=True)
        if not devices:
            raise SpeechError("没有可用的麦克风；请连接设备后重试。")
        print("可用输入设备：")
        for device in devices:
            default = "（系统默认）" if device.is_default else ""
            print(f"  {device.index}: {device.name} · {device.hostapi}{default}")
        choice = (str(self.device_index) if self.device_index is not None
                  else await asyncio.to_thread(input, "请选择设备编号："))
        try:
            index = int(choice.strip())
        except ValueError as exc:
            raise SpeechError("请输入列出的设备编号。") from exc
        matches = [device for device in devices if device.index == index]
        if len(matches) != 1:
            raise SpeechError("所选设备不存在或无法唯一识别；请重新选择。")
        return matches[0]

    async def record_once(self, service, device, number: int):
        capture = AudioCapture(device=device, pcm_seconds=service.config.pcm_seconds)
        recognizer = StreamingRecognizer(service)
        started = asyncio.Event()
        task = asyncio.create_task(recognizer.record(
            capture, lambda result: print(f"预览：{result.text}") if not result.is_final else None,
            on_started=started.set))
        try:
            start_waiter = asyncio.create_task(started.wait())
            try:
                done, _ = await asyncio.wait({task, start_waiter}, timeout=10, return_when=asyncio.FIRST_COMPLETED)
                if task in done:
                    await task
                if start_waiter not in done:
                    raise TimeoutError("麦克风启动超时")
            finally:
                start_waiter.cancel()
                await asyncio.gather(start_waiter, return_exceptions=True)
            print(f"第 {number} 次：录音中，请说话…（{self.seconds:g} 秒）")
            await asyncio.sleep(self.seconds)
            await capture.stop()
            try:
                result = await task
            except NoSpeechDetected:
                print("未识别到语音，请重试。")
            else:
                print(f"最终转写：{result.text}")
            print(f"采样统计（仅用于诊断）：{capture.recording_stats}")
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await capture.close()

    async def run(self):
        service = await asyncio.to_thread(SpeechService.shared)
        try:
            await service.prepare(ModelKind.ASR, warm=True)
            if service.status()[ModelKind.ASR].stage != ModelStage.READY:
                raise SpeechError("语音识别模型尚未就绪。")
            device = await self.choose_device()
            number = 0
            while True:
                command = await asyncio.to_thread(input, "按 Enter 开始录音，输入 q 退出：")
                if command.strip().lower() == "q":
                    break
                if command.strip():
                    print("请只按 Enter 开始，或输入 q 退出。")
                    continue
                number += 1
                await self.record_once(service, device, number)
        finally:
            await SpeechService.close_shared()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=15, help="每次录音秒数，大于 0 且不超过 300（默认 15）")
    parser.add_argument("--device", type=int, help="明确指定输入设备编号；省略时交互选择")
    args = parser.parse_args()
    if not 0 < args.seconds <= 300:
        parser.error("--seconds 必须大于 0 且不超过 300")
    try:
        asyncio.run(SpeechInputTest(args.seconds, args.device).run())
    except (SpeechError, ValueError, TimeoutError) as exc:
        parser.exit(1, f"语音诊断未完成：{exc}\n")
    except KeyboardInterrupt:
        print("\n已取消测试。")
