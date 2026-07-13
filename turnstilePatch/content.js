// Turnstile Patch - 仅做轻量 stealth，不在注册早期页面补丁 turnstile API。
// 原因：在 OTP/邮箱路由阶段注入 pageHook 会干扰 xAI SPA 过渡，
// 表现为 verify-email 200 后长期停在 "An error occurred"。
// Turnstile 观测 hook 改由 Python 在资料页 fill_profile_and_submit 时 CDP 注入。

(function () {
    "use strict";

    // 1. 隐藏 navigator.webdriver
    try {
        Object.defineProperty(navigator, "webdriver", {
            get: function () {
                return false;
            },
            configurable: true,
        });
    } catch (e) {}

    // 2. 覆盖 permissions.query 的 notifications 异常路径
    try {
        var origQuery = navigator.permissions.query.bind(navigator.permissions);
        navigator.permissions.query = function (params) {
            if (params && params.name === "notifications") {
                return Promise.resolve({ state: Notification.permission });
            }
            return origQuery(params);
        };
    } catch (e) {}

    // 3. languages 保持常见桌面画像
    try {
        Object.defineProperty(navigator, "languages", {
            get: function () {
                return ["en-US", "en"];
            },
            configurable: true,
        });
    } catch (e) {}

    // 4. 资料页若出现可见 Turnstile iframe，尝试轻点（跨域失败则忽略）
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
