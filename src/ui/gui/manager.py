"""GUI ViewManager：组合 QmlAppHost / 主界面 / 设置控制器，实现 ViewPort."""

from PySide6.QtCore import QObject, QUrl, Slot

from src.core.event_bus import EventBus, Events
from src.core.task_manager import TaskManager
from src.logging import get_logger
from src.ui.gui.html_ui_bridge import HtmlUiBridge
from src.ui.gui.main_controller import MainWindowController
from src.ui.gui.qml_host import QmlAppHost
from src.ui.gui.services import TrayService
from src.ui.gui.settings_controller import SettingsController
from src.ui.shared.bridge import EventBridge
from src.utils.resource_finder import get_assets_dir

logger = get_logger()


def _html_ui_url() -> str:
    index_html = (get_assets_dir() / "index.html").resolve()
    if not index_html.is_file():
        logger.warning("GuiViewManager: assets/index.html not found at %s", index_html)
        return ""
    return QUrl.fromLocalFile(str(index_html)).toString()


class GuiViewManager(QObject):
    """GUI 界面入口（ViewPort + 设置辅助）.

    设备激活在容器启动前由 GuiActivation 独立窗口完成，主界面不再挂激活 Model/API。
    """

    def __init__(self, event_bus: EventBus, task_manager: TaskManager | None = None):
        super().__init__()
        self._event_bus = event_bus
        self._running = False

        self._owns_tasks = task_manager is None
        if task_manager is not None:
            self._tasks = task_manager
        else:
            self._tasks = TaskManager()
            self._tasks.initialize()

        self._bridge = EventBridge(event_bus, task_manager=self._tasks)
        self._html_ui_bridge = HtmlUiBridge(self._bridge, parent=self)
        self._host = QmlAppHost()
        self._main = MainWindowController()
        self._settings = SettingsController(event_bus, self._tasks, self._bridge)
        self._tray_service: TrayService | None = None

        self._event_bus.on(Events.UI_TOGGLE_WINDOW, self._on_toggle_window)
        logger.debug("GuiViewManager: 已订阅窗口切换事件")

    def _bind_html_event_bus(self) -> None:
        async def on_protocol_connected(_=None) -> None:
            self._html_ui_bridge.notify_connection_state("connected")

        async def on_protocol_disconnected(_=None) -> None:
            self._html_ui_bridge.notify_connection_state("disconnected")

        async def on_network_error(_=None) -> None:
            self._html_ui_bridge.notify_connection_state("disconnected")
            self._html_ui_bridge.notify_error("Network connection lost")

        async def on_mic_input_level(level) -> None:
            try:
                self._html_ui_bridge.notify_input_level(float(level))
            except (TypeError, ValueError):
                pass

        async def on_phone_metrics_updated(data) -> None:
            if isinstance(data, dict):
                self._html_ui_bridge.notify_phone_metrics(data)

        async def on_phone_map_show(_=None) -> None:
            self._html_ui_bridge.notify_show_phone_map()

        self._event_bus.on(Events.PROTOCOL_CONNECTED, on_protocol_connected)
        self._event_bus.on(Events.PROTOCOL_DISCONNECTED, on_protocol_disconnected)
        self._event_bus.on(Events.NETWORK_ERROR, on_network_error)
        self._event_bus.on(Events.MIC_INPUT_LEVEL, on_mic_input_level)
        self._event_bus.on(Events.PHONE_METRICS_UPDATED, on_phone_metrics_updated)
        self._event_bus.on(Events.PHONE_MAP_SHOW, on_phone_map_show)

    async def start(self, mode: str = "gui"):
        if mode == "cli":
            logger.info("GuiViewManager: CLI 模式，跳过 GUI 初始化")
            return

        logger.info("GuiViewManager: 启动 GUI...")
        self._running = True
        self._bind_html_event_bus()

        self._host.create_engine()
        self._host.inject_context(
            {
                "eventBridge": self._bridge,
                "htmlUiBridge": self._html_ui_bridge,
                "mainModel": self._main.main_model,
                "settingsModel": self._settings.ensure_model(),
                "emotionService": self._main.emotion_service,
                "htmlUiUrl": _html_ui_url(),
            }
        )
        self._host.load_main()
        # 冷启动只显示、不抢前台，避免 macOS 把其它全屏 App 的 Space 挤掉
        self._host.show_root(activate=False)
        self._setup_tray()
        self._main.set_neutral_emotion()
        self._html_ui_bridge.notify_connection_state("disconnected")
        logger.info("GuiViewManager: GUI 启动完成")

    async def close(self):
        logger.info("GuiViewManager: 正在关闭...")
        self._running = False
        if self._tray_service:
            self._tray_service.hide()
        self._host.shutdown()
        logger.info("GuiViewManager: 已关闭")

    def _setup_tray(self) -> None:
        root = self._host.root_window()
        if root is None:
            return
        self._tray_service = TrayService(root)
        self._tray_service.setup(
            on_show=self._host.show_root,
            on_quit=self._request_quit,
        )

    def _request_quit(self) -> None:
        self._tasks.spawn(
            self._event_bus.emit(Events.UI_QUIT_REQUEST), name="ui:quit_request"
        )

    async def _on_toggle_window(self, data=None):
        logger.debug("GuiViewManager: 收到窗口切换事件")
        self.toggle_window()

    # ----- ViewPort -----

    @property
    def is_running(self) -> bool:
        return self._running

    def set_chat_text(self, text: str) -> None:
        self._main.set_chat_text(text)
        self._html_ui_bridge.notify_chat_text(text)

    def set_music_line(self, text: str) -> None:
        self._main.set_music_line(text)
        self._html_ui_bridge.notify_music_line(text)

    def set_emotion(self, emotion: str) -> None:
        self._main.set_emotion(emotion)
        self._html_ui_bridge.notify_emotion(emotion)

    def set_status(self, status: str, connected: bool = True) -> None:
        self._main.set_status(status, connected)
        self._html_ui_bridge.notify_status(status, connected)

    def set_button_text(self, text: str) -> None:
        self._main.set_button_text(text)
        self._html_ui_bridge.notify_button_label(text)

    def set_auto_mode(self, auto_mode: bool) -> None:
        self._main.set_auto_mode(auto_mode)
        self._html_ui_bridge.notify_auto_mode(auto_mode)

    def is_auto_mode(self) -> bool:
        return self._main.is_auto_mode()

    def notify_protocol_message(self, message: dict) -> None:
        """Forward protocol JSON to HTML for processing-phase visuals."""
        if not isinstance(message, dict):
            return
        msg_type = message.get("type")
        if msg_type in ("stt", "llm") and message.get("text"):
            self._html_ui_bridge.notify_ui_phase("processing")
        elif msg_type == "tts" and message.get("state") == "start":
            self._html_ui_bridge.notify_ui_phase("speaking")

    def notify_core_device_state(self, state) -> None:
        """Map core DeviceState enum directly to HTML visual phase."""
        from src.constants.constants import DeviceState

        phase_map = {
            DeviceState.IDLE: "wake_ready",
            DeviceState.LISTENING: "listening",
            DeviceState.SPEAKING: "speaking",
        }
        if phase := phase_map.get(state):
            self._html_ui_bridge.notify_device_state(phase)

    # ----- 设置 / 窗口 -----

    @property
    def main_model(self):
        return self._main.main_model

    @property
    def settings_model(self):
        return self._settings.settings_model

    @Slot()
    def toggle_mode(self):
        self._tasks.spawn(
            self._event_bus.emit(Events.UI_AUTO_TOGGLE), name="ui:auto_toggle"
        )

    @Slot()
    def toggle_window(self):
        self._host.toggle_root_visible()

    def open_settings(self):
        if not self._host.engine:
            logger.warning("GuiViewManager: 引擎未初始化，无法打开设置")
            return
        self._settings.open_settings()
