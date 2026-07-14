// Turnstile Patch - 全面 stealth + WebGL/Canvas/Audio 反指纹，不在注册早期页面补丁 turnstile API。
// 原因：在 OTP/邮箱路由阶段注入 pageHook 会干扰 xAI SPA 过渡，
// 表现为 verify-email 200 后长期停在 "An error occurred"。
// Turnstile 观测 hook 改由 Python 在资料页 fill_profile_and_submit 时 CDP 注入。

(function () {
    "use strict";

    // 1. 隐藏 navigator.webdriver
    try {
        Object.defineProperty(navigator, "webdriver", {
            get: function () { return undefined; },
            configurable: true,
        });
    } catch (e) {}

    // 2. chrome.runtime —— Turnstile 检查 window.chrome.runtime
    try {
        if (!window.chrome) window.chrome = {};
        if (!window.chrome.runtime) window.chrome.runtime = {};
    } catch (e) {}

    // 3. permissions.query —— notifications 异常路径
    try {
        var origQuery = navigator.permissions.query.bind(navigator.permissions);
        navigator.permissions.query = function (params) {
            if (params && params.name === "notifications") {
                return Promise.resolve({ state: Notification.permission });
            }
            return origQuery(params);
        };
    } catch (e) {}

    // 4. languages
    try {
        Object.defineProperty(navigator, "languages", {
            get: function () { return ["en-US", "en"]; },
            configurable: true,
        });
    } catch (e) {}

    // 5. platform + userAgent —— 根据 UA 动态推导，Linux UA 替换为 Windows 画像
    try {
        var ua = navigator.userAgent || "";
        var p = "Linux x86_64";
        var fakeUa = ua;
        if (/Windows/.test(ua)) {
            p = "Win32";
        } else if (/Macintosh/.test(ua)) {
            p = "MacIntel";
        } else if (/Linux/.test(ua)) {
            p = "Win32";
            fakeUa = ua.replace("X11; Linux x86_64", "Windows NT 10.0; Win64; x64");
        }
        Object.defineProperty(navigator, "platform", { get: function () { return p; }, configurable: true });
        if (fakeUa !== ua) {
            Object.defineProperty(navigator, "userAgent", { get: function () { return fakeUa; }, configurable: true });
        }
    } catch (e) {}

    // 6. WebGL vendor/renderer —— 始终 hook getParameter，调用时判断是否需要伪装
    try {
        var FAKE_WGL_VENDOR = "Google Inc. (Intel)";
        var FAKE_WGL_RENDERER = "ANGLE (Intel, Mesa Intel(R) UHD Graphics 630 (CFL GT2), OpenGL 4.6)";
        var SW_RE = /swiftshader|llvmpipe|softpipe|software[\s_-]*rasterizer|mesa[\s_-]*swrast/i;

        var hookGetParam = function (proto) {
            if (!proto || !proto.getParameter) return;
            var orig = proto.getParameter;
            proto.getParameter = function (param) {
                var result = orig.call(this, param);
                if (param === 37445 && SW_RE.test(String(result))) return FAKE_WGL_VENDOR;
                if (param === 37446 && SW_RE.test(String(result))) return FAKE_WGL_RENDERER;
                return result;
            };
        };
        try { hookGetParam(WebGLRenderingContext.prototype); } catch (e) {}
        try { hookGetParam(WebGL2RenderingContext.prototype); } catch (e) {}
    } catch (e) {}

    // 7. Canvas 指纹噪声
    try {
        var origToDataURL = HTMLCanvasElement.prototype.toDataURL;
        var origToBlob = HTMLCanvasElement.prototype.toBlob;
        var _noiseOff = ((window.location.hostname || "").length * 7 + 3) % 5 + 1;

        function _canvasInjectNoise(canvas) {
            try {
                var ctx = canvas.getContext("2d");
                if (!ctx || canvas.width < 1 || canvas.height < 1) return;
                var img = ctx.getImageData(0, 0, 1, 1);
                img.data[3] = (img.data[3] + _noiseOff) & 0xFF;
                ctx.putImageData(img, 0, 0);
            } catch (e) {}
        }

        HTMLCanvasElement.prototype.toDataURL = function () {
            _canvasInjectNoise(this);
            return origToDataURL.apply(this, arguments);
        };
        if (origToBlob) {
            HTMLCanvasElement.prototype.toBlob = function () {
                _canvasInjectNoise(this);
                return origToBlob.apply(this, arguments);
            };
        }
    } catch (e) {}

    // 8. AudioContext 指纹噪声
    try {
        var origGetChannelData = AudioBuffer.prototype.getChannelData;
        AudioBuffer.prototype.getChannelData = function (channel) {
            var data = origGetChannelData.call(this, channel);
            if (data && data.length > 0) {
                var off = ((this.length || 0) * 3 + 1) % 7 / 100000;
                data[0] = data[0] + off;
            }
            return data;
        };
    } catch (e) {}

    // 9. navigator.plugins 伪装 —— headless Chrome 的 plugins 为空数组是已知检测项
    //    必须先缓存原始值，否则 getter 内访问 navigator.plugins 会无限递归
    try {
        var _origPlugins = navigator.plugins;
        if (!_origPlugins || _origPlugins.length === 0) {
            Object.defineProperty(navigator, "plugins", {
                get: function () { return _origPlugins; },
                configurable: true,
            });
        }
    } catch (e) {}

    // 10. WebRTC 屏蔽 —— 容器内 WebRTC 暴露内网 IP 是检测项
    try {
        if (window.RTCPeerConnection || window.webkitRTCPeerConnection) {
            var _RTC = window.RTCPeerConnection || window.webkitRTCPeerConnection;
            var _origSetConfig = _RTC.prototype.setConfiguration;
            if (_origSetConfig) {
                _RTC.prototype.setConfiguration = function (config) {
                    if (config && config.iceTransportPolicy === undefined) {
                        config.iceTransportPolicy = "relay";
                    }
                    return _origSetConfig.call(this, config);
                };
            }
        }
        if (navigator.mediaDevices && navigator.mediaDevices.enumerateDevices) {
            var origEnum = navigator.mediaDevices.enumerateDevices.bind(navigator.mediaDevices);
            navigator.mediaDevices.enumerateDevices = function () {
                return origEnum().then(function (d) { return d.filter(function (x) { return x.kind !== "videoinput"; }); });
            };
        }
    } catch (e) {}

    // 11. chrome.csi / chrome.loadTimes —— 完整 window.chrome 对象
    try {
        if (!window.chrome.csi) window.chrome.csi = function () { return {}; };
        if (!window.chrome.loadTimes) window.chrome.loadTimes = function () { return { commitLoadTime: Date.now()/1000, startLoadTime: Date.now()/1000 }; };
    } catch (e) {}

    // 12. 资料页若出现可见 Turnstile iframe，尝试轻点（跨域失败则忽略）
    function autoClickTurnstile() {
        var checkCount = 0;
        var maxChecks = 80;
        var timer = setInterval(function () {
            checkCount++;
            if (checkCount > maxChecks) {
                clearInterval(timer);
                return;
            }
            try {
                var iframes = document.querySelectorAll(
                    'iframe[src*="challenges.cloudflare.com"], iframe[src*="turnstile"]'
                );
                if (!iframes.length) return;
                for (var i = 0; i < iframes.length; i++) {
                    var iframe = iframes[i];
                    try {
                        var body = iframe.contentDocument || iframe.contentWindow.document;
                        var checkbox = body.querySelector('input[type="checkbox"], .mark');
                        if (checkbox && !checkbox.checked) checkbox.click();
                    } catch (e) {
                        // 跨域，无法直接点
                    }
                }
            } catch (e) {}
        }, 500);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", autoClickTurnstile);
    } else {
        autoClickTurnstile();
    }
})();
