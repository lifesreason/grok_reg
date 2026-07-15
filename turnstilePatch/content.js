// Turnstile Patch - toString 保护 + 原型级反指纹 + 帧隔离，不在注册早期页面补丁 turnstile API。
// 原因：在 OTP/邮箱路由阶段注入 pageHook 会干扰 xAI SPA 过渡，
// 表现为 verify-email 200 后长期停在 "An error occurred"。
// Turnstile 观测 hook 改由 Python 在资料页 fill_profile_and_submit 时 CDP 注入。
//
// 关键修复（与 Python stealth 脚本同步）：
// 0. Function.prototype.toString 保护——Cloudflare 通过 fn.toString() 检查函数是否为 native code
// 1. 所有 navigator 属性在 Navigator.prototype 上覆盖，避免 hasOwnProperty 检测
// 2. webdriver 返回 false 而非 undefined
// 3. chrome.runtime/csi/loadTimes 仅在顶层 frame 注入
// 4. 新增 chrome.app（真实 Chrome 有此属性）
// 5. 新增 userAgentData / maxTouchPoints / connection 覆盖
// 6. WebGL getSupportedExtensions 也替换

(function () {
    "use strict";

    // ====== 0. Function.prototype.toString 保护 ======
    var _origToString = Function.prototype.toString;
    var _nativeStrMap = new WeakMap();

    var _initToString = function () {
        var outStr = _nativeStrMap.has(this) ? _nativeStrMap.get(this) : _origToString.call(this);
        return outStr;
    };
    _nativeStrMap.set(_initToString, "function toString() { [native code] }");
    Function.prototype.toString = _initToString;

    function _hookGetter(obj, prop, getterFn, nativeStr) {
        try {
            _nativeStrMap.set(getterFn, nativeStr || ("function get " + prop + "() { [native code] }"));
            Object.defineProperty(obj, prop, { get: getterFn, configurable: true, enumerable: true });
        } catch (e) {}
    }
    function _hookValue(obj, prop, valueFn, nativeStr) {
        try {
            _nativeStrMap.set(valueFn, nativeStr || ("function " + prop + "() { [native code] }"));
            Object.defineProperty(obj, prop, { value: valueFn, configurable: true, writable: true });
        } catch (e) {}
    }

    var isTop = (window.top === window.self);

    // ====== 1. navigator.webdriver ======
    try {
        var wd = navigator.webdriver;
        if (wd === true || wd === undefined) {
            _hookGetter(Navigator.prototype, "webdriver", function () { return false; });
        }
    } catch (e) {}

    // ====== 2. chrome 对象 —— 仅顶层 frame ======
    try {
        if (isTop) {
            if (!window.chrome) window.chrome = {};
            if (!window.chrome.runtime) window.chrome.runtime = {};
            if (!window.chrome.app) {
                window.chrome.app = {
                    getDetails: function () { return null; },
                    getIsInstalled: function () { return false; },
                    runningState: function () { return "cannot_run"; },
                    installState: function () { return "disabled"; },
                    isInstalled: false,
                };
                _nativeStrMap.set(window.chrome.app.getDetails, "function getDetails() { [native code] }");
                _nativeStrMap.set(window.chrome.app.getIsInstalled, "function getIsInstalled() { [native code] }");
                _nativeStrMap.set(window.chrome.app.runningState, "function runningState() { [native code] }");
                _nativeStrMap.set(window.chrome.app.installState, "function installState() { [native code] }");
            }
            if (!window.chrome.csi) {
                var _csi = function () {
                    var _t = performance.timing || {};
                    return { startE: _t.navigationStart || Date.now() - 2000, onloadT: _t.loadEventEnd || Date.now() - 500, pageT: 2000, tran: 15 };
                };
                _nativeStrMap.set(_csi, "function csi() { [native code] }");
                window.chrome.csi = _csi;
            }
            if (!window.chrome.loadTimes) {
                var _lt = function () {
                    var _t = performance.timing || {};
                    var base = (_t.navigationStart || Date.now()) / 1000;
                    return {
                        commitLoadTime: base + 0.5, connectionInfo: "h2",
                        finishDocumentLoadTime: base + 1.5, finishLoadTime: base + 2,
                        firstPaintAfterLoadTime: 0, firstPaintTime: base + 1,
                        navigationType: "Other", npnNegotiatedProtocol: "h2",
                        requestTime: base - 0.5, startLoadTime: base,
                        wasAlternateProtocolAvailable: false, wasFetchedViaSPDY: true,
                        wasNpnNegotiated: true
                    };
                };
                _nativeStrMap.set(_lt, "function loadTimes() { [native code] }");
                window.chrome.loadTimes = _lt;
            }
        }
    } catch (e) {}

    // ====== 3. permissions.query ======
    try {
        if (window.navigator.permissions && window.navigator.permissions.query) {
            var origQuery = Permissions.prototype.query;
            _hookValue(Permissions.prototype, "query", function (parameters) {
                if (parameters && parameters.name === "notifications") {
                    return Promise.resolve({ state: Notification.permission });
                }
                return origQuery.call(this, parameters);
            }, "function query() { [native code] }");
        }
    } catch (e) {}

    // ====== 4. languages ======
    _hookGetter(Navigator.prototype, "languages", function () { return ["en-US", "en"]; });

    // ====== 5. platform + userAgent + appVersion ======
    try {
        var ua = navigator.userAgent || "";
        var p = "Linux x86_64";
        var fakeUa = ua;
        if (/Windows/.test(ua)) { p = "Win32"; }
        else if (/Macintosh/.test(ua)) { p = "MacIntel"; }
        else if (/Linux/.test(ua)) { p = "Win32"; fakeUa = ua.replace("X11; Linux x86_64", "Windows NT 10.0; Win64; x64"); }
        _hookGetter(Navigator.prototype, "platform", function () { return p; });
        if (fakeUa !== ua) {
            _hookGetter(Navigator.prototype, "userAgent", function () { return fakeUa; });
        }
        var effectiveUa = fakeUa !== ua ? fakeUa : ua;
        _hookGetter(Navigator.prototype, "appVersion", function () { return effectiveUa.replace("Mozilla/", ""); });
    } catch (e) {}

    // ====== 6. maxTouchPoints ======
    _hookGetter(Navigator.prototype, "maxTouchPoints", function () { return 0; });

    // ====== 7. navigator.connection ======
    try {
        if (!navigator.connection) {
            _hookGetter(Navigator.prototype, "connection", function () {
                return { effectiveType: "4g", rtt: 50, downlink: 10, saveData: false };
            });
        }
    } catch (e) {}

    // ====== 8. navigator.userAgentData ======
    try {
        if (navigator.userAgentData) {
            var ua2 = navigator.userAgent || "";
            var cm = ua2.match(/Chrome\/(\d+)/);
            var cv = cm ? cm[1] : "150";
            var isWin = /Windows/.test(ua2);
            var fakeUAD = {
                brands: [
                    { brand: "Google Chrome", version: cv },
                    { brand: "Chromium", version: cv },
                    { brand: "Not_A Brand", version: "24" },
                ],
                mobile: false,
                platform: isWin ? "Windows" : "macOS",
            };
            var _hev = function (hints) {
                return Promise.resolve({
                    brands: fakeUAD.brands, mobile: false,
                    platform: isWin ? "Windows" : "macOS",
                    platformVersion: isWin ? "10.0.0" : "13.6.0",
                    architecture: "x86", bitness: "64", model: "",
                    uaFullVersion: cv + ".0.0.0",
                    fullVersionList: [
                        { brand: "Google Chrome", version: cv + ".0.0.0" },
                        { brand: "Chromium", version: cv + ".0.0.0" },
                        { brand: "Not_A Brand", version: "24.0.0.0" },
                    ],
                });
            };
            _nativeStrMap.set(_hev, "function getHighEntropyValues() { [native code] }");
            fakeUAD.getHighEntropyValues = _hev;
            var _toJSON = function () { return { brands: fakeUAD.brands, mobile: false, platform: fakeUAD.platform }; };
            _nativeStrMap.set(_toJSON, "function toJSON() { [native code] }");
            fakeUAD.toJSON = _toJSON;
            _hookGetter(Navigator.prototype, "userAgentData", function () { return fakeUAD; });
        }
    } catch (e) {}

    // ====== 9. WebGL vendor/renderer/extensions ======
    try {
        var FAKE_WGL_VENDOR = "Google Inc. (Intel)";
        var FAKE_WGL_RENDERER = "ANGLE (Intel, Mesa Intel(R) UHD Graphics 630 (CFL GT2), OpenGL 4.6)";
        var SW_RE = /swiftshader|llvmpipe|softpipe|software[\s_-]*rasterizer|mesa[\s_-]*swrast/i;
        var FAKE_WGL1_EXTS = [
            "ANGLE_instanced_arrays","EXT_blend_minmax","EXT_color_buffer_half_float",
            "EXT_disjoint_timer_query","EXT_float_blend","EXT_frag_depth",
            "EXT_shader_texture_lod","EXT_texture_compression_bptc",
            "EXT_texture_compression_rgtc","EXT_texture_filter_anisotropic",
            "EXT_sRGB","OES_element_index_uint","OES_fbo_render_mipmap",
            "OES_standard_derivatives","OES_texture_float","OES_texture_float_linear",
            "OES_texture_half_float","OES_texture_half_float_linear","OES_vertex_array_object",
            "WEBGL_color_buffer_float","WEBGL_compressed_texture_s3tc",
            "WEBGL_compressed_texture_s3tc_srgb","WEBGL_debug_renderer_info",
            "WEBGL_debug_shaders","WEBGL_depth_texture","WEBGL_draw_buffers",
            "WEBGL_lose_context","WEBGL_multi_draw"
        ];
        var FAKE_WGL2_EXTS = [
            "EXT_color_buffer_float","EXT_color_buffer_half_float","EXT_disjoint_timer_query_webgl2",
            "EXT_float_blend","EXT_texture_compression_bptc","EXT_texture_compression_rgtc",
            "EXT_texture_filter_anisotropic","EXT_texture_norm16","KHR_parallel_shader_compile",
            "OES_draw_buffers_indexed","OES_texture_float_linear","OVR_multiview2",
            "WEBGL_compressed_texture_s3tc","WEBGL_compressed_texture_s3tc_srgb",
            "WEBGL_debug_renderer_info","WEBGL_debug_shaders","WEBGL_lose_context",
            "WEBGL_multi_draw","WEBGL_provoking_vertex"
        ];

        var hookWebGL = function (proto, fakeExts) {
            if (!proto || !proto.getParameter) return;
            var origGetParam = proto.getParameter;
            var origGetExts = proto.getSupportedExtensions;
            var isSW = function (gl) { try { return SW_RE.test(String(origGetParam.call(gl, 37446))); } catch (e) { return false; } };

            var _getParam = function (param) {
                var result = origGetParam.call(this, param);
                if (param === 37446 && SW_RE.test(String(result))) return FAKE_WGL_RENDERER;
                if (param === 37445 && isSW(this)) return FAKE_WGL_VENDOR;
                return result;
            };
            _nativeStrMap.set(_getParam, "function getParameter() { [native code] }");
            proto.getParameter = _getParam;

            if (origGetExts) {
                var _getExts = function () {
                    if (isSW(this)) return fakeExts;
                    return origGetExts.call(this);
                };
                _nativeStrMap.set(_getExts, "function getSupportedExtensions() { [native code] }");
                proto.getSupportedExtensions = _getExts;
            }
        };
        try { hookWebGL(WebGLRenderingContext.prototype, FAKE_WGL1_EXTS); } catch (e) {}
        try { hookWebGL(WebGL2RenderingContext.prototype, FAKE_WGL2_EXTS); } catch (e) {}
    } catch (e) {}

    // ====== 10. Canvas 指纹噪声 ======
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

        var _toDataURL = function () { _canvasInjectNoise(this); return origToDataURL.apply(this, arguments); };
        _nativeStrMap.set(_toDataURL, "function toDataURL() { [native code] }");
        HTMLCanvasElement.prototype.toDataURL = _toDataURL;
        if (origToBlob) {
            var _toBlob = function () { _canvasInjectNoise(this); return origToBlob.apply(this, arguments); };
            _nativeStrMap.set(_toBlob, "function toBlob() { [native code] }");
            HTMLCanvasElement.prototype.toBlob = _toBlob;
        }
    } catch (e) {}

    // ====== 11. AudioContext 指纹噪声 ======
    try {
        var origGetChannelData = AudioBuffer.prototype.getChannelData;
        var _gcd = function (channel) {
            var data = origGetChannelData.call(this, channel);
            if (data && data.length > 0) {
                var off = ((this.length || 0) * 3 + 1) % 7 / 100000;
                data[0] = data[0] + off;
            }
            return data;
        };
        _nativeStrMap.set(_gcd, "function getChannelData() { [native code] }");
        AudioBuffer.prototype.getChannelData = _gcd;
    } catch (e) {}

    // ====== 12. WebRTC + enumerateDevices ======
    try {
        if (window.RTCPeerConnection || window.webkitRTCPeerConnection) {
            var _RTC = window.RTCPeerConnection || window.webkitRTCPeerConnection;
            var _origSetConfig = _RTC.prototype.setConfiguration;
            if (_origSetConfig) {
                var _setConfig = function (config) {
                    if (config && config.iceTransportPolicy === undefined) { config.iceTransportPolicy = "relay"; }
                    return _origSetConfig.call(this, config);
                };
                _nativeStrMap.set(_setConfig, "function setConfiguration() { [native code] }");
                _RTC.prototype.setConfiguration = _setConfig;
            }
        }
        if (navigator.mediaDevices && navigator.mediaDevices.enumerateDevices) {
            var _origEnum = MediaDevices.prototype.enumerateDevices;
            var _enum = function () { return _origEnum.call(this).then(function (d) { return d.filter(function (x) { return x.kind !== "videoinput"; }); }); };
            _nativeStrMap.set(_enum, "function enumerateDevices() { [native code] }");
            MediaDevices.prototype.enumerateDevices = _enum;
        }
    } catch (e) {}

    // ====== 13. 资料页若出现可见 Turnstile iframe，尝试轻点 ======
    function autoClickTurnstile() {
        var checkCount = 0;
        var maxChecks = 80;
        var timer = setInterval(function () {
            checkCount++;
            if (checkCount > maxChecks) { clearInterval(timer); return; }
            try {
                var iframes = document.querySelectorAll(
                    'iframe[src*="challenges.cloudflare.com"], iframe[src*="turnstile"]'
                );
                if (!iframes.length) return;
                for (var i = 0; i < iframes.length; i++) {
                    try {
                        var body = iframes[i].contentDocument || iframes[i].contentWindow.document;
                        var checkbox = body.querySelector('input[type="checkbox"], .mark');
                        if (checkbox && !checkbox.checked) checkbox.click();
                    } catch (e) {}
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
