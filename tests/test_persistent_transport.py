"""Persistent WebSocket transport tests."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.constants.constants import DeviceState


@pytest.mark.asyncio
async def test_connect_persistent_transport_keeps_idle():
    from src.bootstrap.session import ConversationSession

    state = MagicMock()
    state.is_idle.return_value = True
    state.is_listening.return_value = False
    state.is_speaking.return_value = False
    state.set_keep_listening = MagicMock()
    state.set_device_state = AsyncMock()

    protocol = MagicMock()
    protocol.is_audio_channel_opened.return_value = False
    protocol.connect = AsyncMock(return_value=True)
    protocol.protocol = MagicMock()
    protocol.protocol.__class__.__name__ = "WebsocketProtocol"

    plugins = MagicMock()
    plugins.notify_protocol_connected = AsyncMock()

    session = ConversationSession(state, protocol, plugins)
    session._persistent_transport_enabled = MagicMock(return_value=True)

    ok = await session.connect_persistent_transport()

    assert ok is True
    assert session._keep_idle_on_channel_open is False
    protocol.connect.assert_awaited_once()
    plugins.notify_protocol_connected.assert_awaited_once()


@pytest.mark.asyncio
async def test_audio_channel_opened_with_keep_idle_sets_idle():
    from src.bootstrap.session import ConversationSession

    state = MagicMock()
    state.set_keep_listening = MagicMock()
    state.set_device_state = AsyncMock()

    session = ConversationSession(state, MagicMock(), MagicMock())
    session._keep_idle_on_channel_open = True

    await session._on_audio_channel_opened()

    state.set_keep_listening.assert_called_once_with(False)
    state.set_device_state.assert_awaited_once_with(DeviceState.IDLE)


@pytest.mark.asyncio
async def test_audio_channel_closed_schedules_reconnect():
    from src.bootstrap.session import ConversationSession

    state = MagicMock()
    state.set_device_state = AsyncMock()

    session = ConversationSession(state, MagicMock(), MagicMock())
    session._schedule_persistent_reconnect = MagicMock()

    await session._on_audio_channel_closed()

    state.set_device_state.assert_awaited_once_with(DeviceState.IDLE)
    session._schedule_persistent_reconnect.assert_called_once()
