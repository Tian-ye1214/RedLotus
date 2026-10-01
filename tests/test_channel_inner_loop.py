import asyncio
from types import SimpleNamespace

import pytest

from redlotus.core.system import AgentSystem
from redlotus.sessions.control import UserMessage


@pytest.mark.asyncio
async def test_channel_supplements_keep_turn_and_reply_to_last_consumed(phone):
    bot, state, first_reply, replies, calls = phone
    entered, release = asyncio.Event(), asyncio.Event()
    targets, consumed = [], []
    system = state.agent
    system._session = state
    system.workspace = system.toolkit._references.workspace
    system.presentation = SimpleNamespace(print_warning=lambda text: None)
    system.add_urgent_message = AgentSystem.add_urgent_message.__get__(system)

    def start(message, history, *, turn_id):
        async def run():
            async with state.turn(message.text, turn_id=turn_id):
                calls.append(message)
                state.consume_recorded_input(turn_id, message, (state.generation, turn_id), None)
                entered.set()
                await release.wait()
                for admission, item in await state.take_urgent():
                    consumed.append(item.text)
                    state.consume_recorded_input(admission.id, item, (state.generation, turn_id), None)
                return "combined answer"
        return asyncio.create_task(run())

    system._start_user_turn = start
    await bot.dispatch_user_message("wx_owner", UserMessage("first"), first_reply)
    await entered.wait()

    async def second_reply(text):
        targets.append(text)

    await bot.dispatch_user_message("wx_owner", UserMessage("supplement"), second_reply)
    release.set()
    await state.queue.join()
    assert len(calls) == 1
    assert consumed == ["supplement"]
    assert targets[-1] == "combined answer"
    assert "combined answer" not in replies
    assert not state.deliveries


@pytest.mark.asyncio
async def test_failed_urgent_attachment_does_not_cancel_task(phone):
    bot, state, send, replies, calls = phone
    system = state.agent
    system._session = state
    system.workspace = system.toolkit._references.workspace
    system.presentation = SimpleNamespace(print_warning=lambda text: None)
    system.add_urgent_message = AgentSystem.add_urgent_message.__get__(system)

    async def broken():
        raise ValueError("broken voice")

    async with state.turn("running", turn_id="outer"):
        await bot.dispatch_user_message("wx_owner", UserMessage(""), send, prepare=broken)
        assert await state.take_urgent() == []
        assert state.active
        assert not state.queue.pending
    assert any("broken voice" in text for text in replies)
    assert not calls


@pytest.mark.asyncio
@pytest.mark.parametrize("first_fails", [False, True])
async def test_question_answers_reserve_identity_and_preserve_receive_order(phone, first_fails):
    bot, state, send, replies, calls = phone
    system = state.agent
    system._session = state
    system.workspace = system.toolkit._references.workspace
    system.presentation = SimpleNamespace(print_warning=lambda text: None)
    system.add_urgent_message = AgentSystem.add_urgent_message.__get__(system)
    entered, release = asyncio.Event(), asyncio.Event()
    async def slow():
        entered.set()
        await release.wait()
        if first_fails:
            raise ValueError("attachment unavailable")
        return []
    async with state.turn("running", turn_id="outer"):
        state.question = asyncio.get_running_loop().create_future()
        first, second = UserMessage("first answer"), UserMessage("second answer")
        await bot.dispatch_user_message("wx_owner", first, send, prepare=slow)
        await entered.wait()
        await bot.dispatch_user_message("wx_owner", second, send)
        assert first.input_id != second.input_id
        assert first.input_id in state.deliveries and second.input_id in state.deliveries
        await asyncio.sleep(0)
        assert not state.question.done()
        release.set()
        answer = await asyncio.wait_for(state.question, 2)
        assert answer is (second if first_fails else first)
        supplements = await state.take_urgent()
        assert [message.text for _, message in supplements] == ([] if first_fails else ["second answer"])
        assert not state.queue.pending


@pytest.mark.asyncio
async def test_urgent_failure_notification_transport_error_does_not_abort_task(phone):
    bot, state, _, replies, calls = phone
    system = state.agent
    system._session = state
    system.workspace = system.toolkit._references.workspace
    system.presentation = SimpleNamespace(print_warning=lambda text: None)
    system.add_urgent_message = AgentSystem.add_urgent_message.__get__(system)
    async def broken():
        raise ValueError("bad attachment")
    async def send(text):
        if "未加入" in text:
            raise OSError("network unavailable")
    async with state.turn("running", turn_id="outer"):
        await bot.dispatch_user_message("wx_owner", UserMessage(""), send, prepare=broken)
        assert await state.take_urgent() == []
        assert state.active
        assert not state.deliveries


@pytest.mark.asyncio
async def test_idle_command_confirmation_does_not_start_agent(phone):
    bot, state, send, replies, calls = phone
    state.question = asyncio.get_running_loop().create_future()
    await bot.dispatch_user_message("wx_owner", UserMessage("yes"), send)
    answer = await asyncio.wait_for(state.question, 2)
    await state.queue.join()
    assert answer.text == "yes" and answer.input_id
    assert not calls and not state.deliveries
