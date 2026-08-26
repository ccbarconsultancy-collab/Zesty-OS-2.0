"""唤醒词插件.

检测唤醒词并触发对话。

Battery / Privacy Design:
  - ARMED (idle): 仅本地 sherpa-onnx 关键词检测运行在麦克风输入上。
    音频帧被本地处理（KeywordSpotter），从不编码或发送到服务器。
    这是低功耗的 always-on 监听，等同于 Siri 的 "嘘" 监听。
  - LISTENING (post-wake): 音频被 Opus 编码并发送到服务器进行 STT/LLM/TTS。
  - SPEAKING: 如果 AEC + REALTIME 模式开启，麦克风仍采集用于回声消除；
    否则麦克风采集暂停。唤醒词检测在此期间暂停，避免重复触发。

  仅当应用窗口打开时，ARMED 状态默认激活。唤醒词检测器在应用启动时
  启动，并在会话结束后自动重新武装。
"""

import asyncio
from typing import TYPE_CHECKING, Optional

from src.constants.constants import AbortReason, DeviceState
from src.logging import get_logger
from src.plugins.base import Plugin

if TYPE_CHECKING:
    from src.bootstrap.protocols import PluginCommands, PluginContext

logger = get_logger()


class WakeWordPlugin(Plugin):
    name = "wake_word"
    priority = 30
    requires = ["audio"]

    def __init__(self) -> None:
        super().__init__()
        self.detector = None

    @property
    def _audio_plugin(self):
        """通过依赖注入获取 AudioPlugin."""
        return self.get_dep("audio")

    async def setup(self, ctx: "PluginContext", cmd: "PluginCommands") -> None:
        await super().setup(ctx, cmd)
        # 订阅配置变更事件（轻量，不加载模型）
        from src.core.event_bus import Events
        ctx.event_bus.on(Events.CONFIG_CHANGED, self._on_config_changed)
        # 订阅设备状态变更，协调唤醒词检测的暂停/恢复
        ctx.event_bus.on(Events.DEVICE_STATE_CHANGED, self._on_device_state_changed)

    async def _on_device_state_changed(self, data=None) -> None:
        """EventBus 回调：从事件载荷提取 new_state 并协调检测器."""
        if not data:
            return
        state = data.get("new_state") if isinstance(data, dict) else data
        if state is not None:
            await self.on_device_state_changed(state)

    async def _on_config_changed(self, data=None):
        """配置变更时重新加载唤醒词模型."""
        logger.info("WakeWordPlugin: 收到配置变更事件，重新加载唤醒词模型")
        await self.reload_model()

    async def on_device_state_changed(self, state) -> None:
        """设备状态变更时协调唤醒词检测的暂停/恢复.

        ARMED (IDLE)   → 恢复唤醒词检测，麦克风仅用于本地关键词检测
        LISTENING/SPEAKING → 暂停唤醒词检测，麦克风用于服务器音频发送
        """
        if not self.detector:
            return

        try:
            if state == DeviceState.IDLE:
                # 返回 ARMED 状态：重新启用唤醒词检测
                self._resume_detection()
                logger.info("状态变为 ARMED (IDLE)，唤醒词检测已恢复")
            elif state in (DeviceState.LISTENING, DeviceState.SPEAKING):
                # 进入会话：暂停唤醒词检测，避免重复触发和额外 CPU 消耗
                self._pause_detection()
                logger.info(f"状态变为 {state.value.upper()}，唤醒词检测已暂停")
        except Exception as e:
            logger.error(f"协调唤醒词检测状态变更失败: {e}", exc_info=True)

    async def start(self) -> None:
        try:
            # 延迟加载模型到 start() 阶段，避免 setup() 时与 PortAudio DLL 冲突
            if self.detector is None:
                from src.audio_processing.wake_word_detect import WakeWordDetector

                self.detector = WakeWordDetector()
                if not await self.detector.initialize():
                    logger.info("唤醒词检测器未启用或初始化失败")
                    self.detector = None
                    return
                self.detector.on_detected(self._on_detected)
                self.detector.on_error = self._on_error

            if not self._audio_plugin or not self._audio_plugin.codec:
                logger.warning("未找到 audio_codec，无法启动唤醒词检测")
                return
            await self.detector.start(self._audio_plugin.codec)
            logger.info("唤醒词检测器已就绪 (ARMED)，等待唤醒词...")
        except ImportError as e:
            logger.error(f"无法导入唤醒词检测器: {e}", exc_info=True)
            self.detector = None
        except Exception as e:
            logger.error(f"启动唤醒词检测器失败: {e}", exc_info=True)

    async def stop(self) -> None:
        if self.detector:
            try:
                await self.detector.stop()
            except Exception as e:
                logger.warning(f"停止唤醒词检测器失败: {e}", exc_info=True)

    def register_resources(self, pool) -> None:
        detector = self.detector
        if detector:
            pool.register("wake_word.detector", detector.shutdown)

    async def reload_model(self, model_path: Optional[str] = None) -> bool:
        """热重载唤醒词模型.

        Args:
            model_path: 新模型路径（如 "models/en"）。如果为 None，从配置读取。

        Returns:
            是否重载成功
        """
        if not self.detector:
            logger.warning("检测器未初始化，无法热重载")
            return False

        try:
            return await self.detector.reload(model_path)
        except Exception as e:
            logger.error(f"热重载唤醒词模型失败: {e}", exc_info=True)
            return False

    async def _on_detected(self, wake_word, full_text):
        """
        唤醒词检测回调.

        ARMED → LISTENING 状态转换：检测到唤醒词后自动建立协议连接并开始监听，
        无需用户额外按下按钮。
        """
        logger.info(f"唤醒词 detected: {wake_word}, 进入 LISTENING 状态")
        try:
            logger.info("WAKE_CALLBACK_TRIGGERED: connect_protocol -> start_listening")
            # 暂停唤醒词检测，进入会话监听
            self._pause_detection()

            if self._ctx.is_speaking():
                # 如果 AI 正在说话，中止 TTS，然后开始新的监听会话
                await self._cmd.abort_speaking(AbortReason.WAKE_WORD_DETECTED)
                if self._audio_plugin and self._audio_plugin.codec:
                    await self._audio_plugin.codec.clear_audio_queue()
                # 等待打断完成后再开始监听
                await asyncio.sleep(0.1)
            elif self._ctx.is_listening():
                # 已在监听中（可能来自手动按键），唤醒词触发重新发送
                await self._send_wake_word_detected(wake_word)
                return

            # 建立协议连接并开始监听
            logger.info("CONNECT_PROTOCOL_CALLED (wake word)")
            ok = await self._cmd.connect_protocol()
            if not ok:
                logger.error("唤醒词触发后协议连接失败，无法开始对话")
                self._resume_detection()
                return

            from src.constants.constants import ListeningMode

            mode = (
                ListeningMode.REALTIME
                if self._ctx.get_config().get_config("AEC_OPTIONS.ENABLED", True)
                else ListeningMode.AUTO_STOP
            )
            logger.info(f"START_LISTENING_CALLED (wake word, mode={mode})")
            await self._cmd.start_listening(mode)
            # 单次唤醒会话：TTS 结束后回到 IDLE，恢复唤醒词待命（Siri 式循环）
            self._cmd.set_keep_listening(False)
            logger.info(f"开始监听会话 (mode={mode})，发送麦克风音频到服务器")
        except Exception as e:
            logger.error(f"处理唤醒词检测失败: {e}", exc_info=True)
            # 确保唤醒词检测器在错误后重新武装
            self._resume_detection()

    async def _send_wake_word_detected(self, wake_word: str) -> None:
        """发送唤醒词给服务器 (用于唤醒后立即发送文本触发)."""
        try:
            await self._cmd.send_wake_word_detected(wake_word)
        except Exception as e:
            logger.error(f"发送唤醒词到服务器失败: {e}", exc_info=True)

    def _pause_detection(self) -> None:
        """暂停唤醒词检测（进入 LISTENING/SPEAKING 时调用）"""
        if self.detector:
            self.detector.pause()

    def _resume_detection(self) -> None:
        """恢复唤醒词检测（返回 ARMED 时调用）"""
        if self.detector:
            self.detector.resume()

    async def _on_error(self, error):
        """
        唤醒词检测错误回调.
        """
        logger.error(f"唤醒词检测错误: {error}", exc_info=True)
