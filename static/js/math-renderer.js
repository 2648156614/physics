// Configure MathJax before loading it; never replace its initialized runtime.
(function () {
    let resolveReady;
    let rejectReady;
    const ready = new Promise((resolve, reject) => {
        resolveReady = resolve;
        rejectReady = reject;
    });
    let queue = Promise.resolve();

    function waitForFonts() {
        return document.fonts ? document.fonts.ready : Promise.resolve();
    }

    window.physicsMath = {
        ready: ready,
        fail: function (error) {
            rejectReady(error || new Error('公式引擎加载失败'));
        },
        // Serialize DOM replacement and typesetting, including initial startup.
        typeset: function (elements, updateContent) {
            const result = queue.then(() => ready).then(async () => {
                if (updateContent) {
                    window.MathJax.typesetClear(elements);
                    updateContent();
                }
                await window.MathJax.typesetPromise(elements);
                await waitForFonts();
            });
            // A failed request must not poison later requests in the queue.
            queue = result.catch(() => {});
            return result;
        }
    };
    ready.catch(error => console.error('公式引擎初始化失败:', error));

    window.MathJax = {
        tex: {
            inlineMath: [['$', '$'], ['\\(', '\\)']],
            displayMath: [['$$', '$$'], ['\\[', '\\]']]
        },
        startup: {
            pageReady: function () {
                return window.MathJax.startup.defaultPageReady()
                    .then(waitForFonts)
                    .then(resolveReady)
                    .catch(error => {
                        rejectReady(error);
                        throw error;
                    });
            }
        }
    };
})();
