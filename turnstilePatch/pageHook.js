(function () {
    "use strict";

    if (window.__grokTurnstileHookInstalled) return;
    window.__grokTurnstileHookInstalled = true;
    window.__grokTurnstile = window.__grokTurnstile || {
        widgets: [],
        lastToken: "",
        callbackCount: 0,
        renderCount: 0,
        executeCount: 0,
        errors: [],
    };

    function recordError(step, error) {
        try {
            window.__grokTurnstile.errors.push({
                step: step,
                message: String((error && error.message) || error || "").slice(0, 180),
            });
        } catch (e) {}
    }

    function patch(api) {
        if (!api || api.__grokPatched) return api;
        try {
            var originalRender = typeof api.render === "function" ? api.render.bind(api) : null;
            var originalExecute = typeof api.execute === "function" ? api.execute.bind(api) : null;
            var originalReset = typeof api.reset === "function" ? api.reset.bind(api) : null;
            if (originalRender) {
                api.render = function (container, options) {
                    var opts = options || {};
                    var originalCallback = opts.callback;
                    var wrappedOptions = Object.assign({}, opts);
                    wrappedOptions.callback = function (token) {
                        try {
                            window.__grokTurnstile.lastToken = String(token || "");
                            window.__grokTurnstile.callbackCount += 1;
                            var input = document.querySelector('input[name="cf-turnstile-response"]');
                            if (input && token) {
                                input.value = token;
                                input.dispatchEvent(new Event("input", { bubbles: true }));
                                input.dispatchEvent(new Event("change", { bubbles: true }));
                            }
                        } catch (e) {
                            recordError("callback", e);
                        }
                        if (typeof originalCallback === "function") {
                            return originalCallback.apply(this, arguments);
                        }
                    };
                    var id = originalRender(container, wrappedOptions);
                    try {
                        window.__grokTurnstile.renderCount += 1;
                        window.__grokTurnstile.widgets.push({
                            id: id,
                            sitekey: String(wrappedOptions.sitekey || ""),
                            action: String(wrappedOptions.action || ""),
                            cData: String(wrappedOptions.cData || ""),
                            size: String(wrappedOptions.size || ""),
                            theme: String(wrappedOptions.theme || ""),
                        });
                    } catch (e) {
                        recordError("render-record", e);
                    }
                    return id;
                };
            }
            if (originalExecute) {
                api.execute = function () {
                    try {
                        window.__grokTurnstile.executeCount += 1;
                        window.__grokTurnstile.lastExecuteArgs = Array.from(arguments).map(function (item) {
                            if (typeof item === "string") return item;
                            if (item && item.nodeType === 1) return item.tagName + "#" + (item.id || "");
                            if (item && typeof item === "object") return Object.keys(item).join(",");
                            return String(item);
                        });
                    } catch (e) {}
                    return originalExecute.apply(api, arguments);
                };
            }
            if (originalReset) {
                api.reset = function () {
                    return originalReset.apply(api, arguments);
                };
            }
            Object.defineProperty(api, "__grokPatched", { value: true, configurable: true });
        } catch (e) {
            recordError("patch", e);
        }
        return api;
    }

    var current = window.turnstile;
    try {
        Object.defineProperty(window, "turnstile", {
            configurable: true,
            get: function () {
                return current;
            },
            set: function (value) {
                current = patch(value);
            },
        });
        if (current) current = patch(current);
    } catch (e) {
        recordError("defineProperty", e);
    }

    var attempts = 0;
    var timer = setInterval(function () {
        attempts += 1;
        if (window.turnstile) patch(window.turnstile);
        if ((window.turnstile && window.turnstile.__grokPatched) || attempts > 120) {
            clearInterval(timer);
        }
    }, 250);
})();
