// Custom HTML HUD shell with QWebChannel bridge to py-xiaozhi.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtWebEngine
import QtWebChannel

import "../theme"
import "../components"

AppWindow {
    id: root

    width: 1280
    height: 800
    minimumWidth: 960
    minimumHeight: 600
    title: ""
    visible: false

    WebChannel {
        id: webChannel
        property bool bridgeRegistered: false

        Component.onCompleted: {
            if (typeof htmlUiBridge !== "undefined" && htmlUiBridge) {
                registerObject("htmlUiBridge", htmlUiBridge)
                bridgeRegistered = true
            }
        }
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        TitleBar {
            Layout.fillWidth: true
            showMaximize: true
            onMinimizeClicked: root.showMinimized()
            onMaximizeClicked: {
                if (root.visibility === Window.FullScreen || root.visibility === Window.Maximized) {
                    root.showNormal()
                } else {
                    root.showMaximized()
                }
            }
            onCloseClicked: {
                if (eventBridge) eventBridge.onQuitRequest()
            }
        }

        WebEngineView {
            id: htmlView
            objectName: "htmlView"
            Layout.fillWidth: true
            Layout.fillHeight: true
            webChannel: webChannel
            url: webChannel.bridgeRegistered && (typeof htmlUiUrl !== "undefined" && htmlUiUrl)
                 ? htmlUiUrl
                 : "about:blank"

            settings.localContentCanAccessRemoteUrls: true
            settings.localContentCanAccessFileUrls: true
            settings.javascriptEnabled: true

            onLoadingChanged: function(loadRequest) {
                if (loadRequest.status === WebEngineView.LoadFailedStatus) {
                    console.error("HtmlMainWindow: failed to load HTML UI:", loadRequest.url)
                }
            }
        }
    }
}
