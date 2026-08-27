"""唤醒词插件.

检测唤醒词并触发对话。

Battery / Privacy Design:
  - ARMED (idle): 仅本地 OpenWakeWord 检测运行在麦克风输入上。
    音频帧被本地处理，从不编码或发送到服务器。
  - LISTENING (post-wake): 音频被 Opus 编码并发送到服务器进行 STT/LLM/TTS。
  - SPEAKING: 如果 AEC + REALTIME 模式开启，麦克风仍采集用于回声消除；
    否则麦克风采集暂停。唤醒词检测在此期间暂停，避免重复触发。

  OpenWakeWord 模型在后台线程/任务中加载，不阻塞 Qt GUI 启动。
"""

import asyncio
from typing import TYPE_CHECKING, Optional

from src.constants.constants import DeviceState
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
        self._bootstrap_task: Optional[asyncio.Task] = None

    @property
    def _audio_plugin(self):
        """通过依赖注入获取 AudioPlugin."""
        return self.get_dep("audio")

    async def setup(self, ctx: "PluginContext", cmd: "PluginCommands") -> None:
        await super().setup(ctx, cmd)
        from src.core.event_bus import Events
        ctx.event_bus.on(Events.CONFIG_CHANGED, self._on_config_changed)

    async def _on_config_changed(self, data=None):
        """配置变更时重新加载唤醒词模型."""
        logger.info("WakeWordPlugin: 收到配置变更事件，重新加载唤醒词模型")
        await self.reload_model()

    async def on_device_state_changed(self, state) -> None:
        """设备状态变更时协调唤醒词检测的暂停/恢复."""
        if not self.detector:
            return

        try:
            if state == DeviceState.IDLE:
                self._resume_detection()
                logger.info("状态变为 ARMED (IDLE)，唤醒词检测已恢复")
            elif state in (DeviceState.LISTENING, DeviceState.SPEAKING):
                self._pause_detection()
                logger.info(f"状态变为 {state.value.upper()}，唤醒词检测已暂停")
        except Exception as e:
            logger.error(f"协调唤醒词检测状态变更失败: {e}", exc_info=True)

    async def start(self) -> None:
        """Return immediately; OpenWakeWord loads on a detached background task."""
        try:
            if self.detector is None:
                from src.audio_processing.wake_word_detect import WakeWordDetector

                self.detector = WakeWordDetector()
                self.detector.on_detected(self._on_detected)
                self.detector.on_error = self._on_error

            self._bootstrap_task = self._cmd.spawn(
                self._bootstrap_wake_word(),
                name="wake_word:bootstrap",
            )
            logger.info(
                "WakeWordPlugin started (ARMED); OpenWakeWord loading in background"
            )
        except ImportError as e:
            logger.error(f"无法导入唤醒词检测器: {e}", exc_info=True)
            self.detector = None
        except Exception as e:
            logger.error(f"启动唤醒词检测器失败: {e}", exc_info=True)

    async def _bootstrap_wake_word(self) -> None:
        """Background: load models, then attach to the live audio codec."""
        if not self.detector:
            return

        try:
            load_task = self.detector.schedule_background_initialize()
            ok = await load_task
            if not ok:
                logger.info("唤醒词检测器未启用或初始化失败")
                return

            codec = self._audio_plugin.codec if self._audio_plugin else None
            if not codec:
                logger.warning("未找到 audio_codec，无法启动唤醒词检测")
                return

            await self.detector.start(codec)
            self._resume_detection()

            wake_phrase = (
                self._ctx.get_config().get_config("WAKE_WORD_OPTIONS.WAKE_WORD")
                or "Zesty"
            )
            banner = (
                f"[AUDIO CHECK] Continuous Mic Stream: ACTIVE | "
                f"Wake Word: '{wake_phrase}'"
            )
            print(banner, flush=True)
            logger.info(banner)
            logger.info("唤醒词检测器已就绪 (ARMED)，等待唤醒词...")
        except asyncio.CancelledError:
            logger.debug("Wake-word bootstrap cancelled")
            raise
        except Exception as e:
            logger.error(f"后台唤醒词初始化失败: {e}", exc_info=True)

    async def stop(self) -> None:
        if self._bootstrap_task and not self._bootstrap_task.done():
            self._bootstrap_task.cancel()
            try:
                await self._bootstrap_task
            except asyncio.CancelledError:
                pass
            self._bootstrap_task = None

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
        if not self.detector:
            logger.warning("检测器未初始化，无法热重载")
            return False

        try:
            return await self.detector.reload(model_path)
        except Exception as e:
            logger.error(f"热重载唤醒词模型失败: {e}", exc_info=True)
            return False

    async def _on_detected(self, wake_word, full_text):
        phrase = (wake_word or full_text or "Zesty").strip()
        logger.info(f"WAKE_WORD_HIT: {phrase!r} → activate_on_wake_word (bypass PTT)")
        self._pause_detection()
        try:
            ok = await self._cmd.activate_on_wake_word(phrase)
            if not ok:
                logger.warning("Wake-word activation failed — resuming KWS")
                self._resume_detection()
        except Exception as e:
            logger.error(f"处理唤醒词检测失败: {e}", exc_info=True)
            self._resume_detection()

    def _pause_detection(self) -> None:
        if self.detector:
            self.detector.pause()

    def _resume_detection(self) -> None:
        if self.detector:
            self.detector.resume()

    async def _on_error(self, error):
        logger.error(f"唤醒词检测错误: {error}", exc_info=True)
