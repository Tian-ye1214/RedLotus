import asyncio
from types import SimpleNamespace

import pytest

from redlotus.sessions.control import UserMessage


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/help", "/typo", "/pets", "/exit", "/api", "/agent worker x"])
async def test_chat_commands_never_reach_model(phone, command):
    bot, state, send, replies, calls = phone
    await bot.dispatch_user_message("wx_owner", UserMessage(command), send)
    await state.queue.join()
    assert calls == []
    assert replies and "final answer" not in replies
    if command == "/help":
        assert "bot.owner_channels" in replies[-1]
    if command in {"/pets", "/exit", "/api"}:
        assert "CLI" in replies[-1]


@pytest.mark.asyncio
async def test_unknown_command_does_not_answer_pending_question(phone):
    bot, state, send, replies, calls = phone
    state.question = asyncio.get_running_loop().create_future()
    await bot.dispatch_user_message("wx_owner", UserMessage("/typo"), send)
    assert not state.question.done()
    assert "未知命令" in replies[-1]
    state.question.cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/clear typo", "/stop typo", "/voice on typo", "/help extra", "/cancel abc extra", "/cancel agent abc extra", "/voice rollback"])
async def test_extra_command_arguments_have_no_effect(phone, command):
    bot, state, send, replies, calls = phone
    generation = state.generation
    await bot.dispatch_user_message("wx_owner", UserMessage(command), send)
    assert state.generation == generation
    assert bot._session("wx_owner") is state
    assert not calls
    assert "用法" in replies[-1]
