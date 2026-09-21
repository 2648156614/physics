(function () {
    'use strict';

    document.addEventListener('DOMContentLoaded', function () {
        const importTextarea = document.getElementById('template_import');
        const parseButton = document.getElementById('parse_template_btn');
        const clearButton = document.getElementById('clear_template_btn');
        const statusBox = document.getElementById('template_import_status');
        const imageHint = document.getElementById('detected_image_hint');

        if (!importTextarea || !parseButton || !clearButton || !statusBox) {
            return;
        }

        const fieldDefinitions = [
            {keys: ['name', 'template_name'], elementId: 'template_name'},
            {keys: ['knowledge_point'], elementId: 'knowledge_point'},
            {keys: ['text', 'problem_text'], elementId: 'problem_text'},
            {keys: ['variables'], elementId: 'variables'},
            {keys: ['formula', 'solution_formula'], elementId: 'solution_formula'},
            {keys: ['answer_units'], elementId: 'answer_units'},
            {keys: ['difficulty'], elementId: 'difficulty'}
        ];

        function setStatus(message, type) {
            statusBox.textContent = message;
            statusBox.className = 'small mt-2 text-' + type;
            statusBox.classList.remove('d-none');
        }

        function hideStatus() {
            statusBox.classList.add('d-none');
            statusBox.textContent = '';
        }

        function skipWhitespace(source, index) {
            while (index < source.length && /\s/.test(source[index])) {
                index += 1;
            }
            return index;
        }

        function readQuotedValue(source, index) {
            index = skipWhitespace(source, index);

            let prefix = '';
            while (index < source.length && /[rRuUbBfF]/.test(source[index])) {
                prefix += source[index];
                index += 1;
            }

            const quote = source[index];
            if (quote !== '"' && quote !== "'") {
                return null;
            }

            const isRaw = /r/i.test(prefix);
            const tripleQuote = source.slice(index, index + 3) === quote.repeat(3);
            const contentStart = index + (tripleQuote ? 3 : 1);
            const terminator = tripleQuote ? quote.repeat(3) : quote;
            let value = '';
            let escaped = false;

            for (let i = contentStart; i < source.length; i += 1) {
                if (tripleQuote && source.slice(i, i + 3) === terminator) {
                    return value;
                }

                const char = source[i];
                if (!tripleQuote && !escaped && char === quote) {
                    return value;
                }

                if (isRaw) {
                    value += char;
                    continue;
                }

                if (escaped) {
                    const escapes = {n: '\n', r: '\r', t: '\t', '\\': '\\', '"': '"', "'": "'"};
                    value += Object.prototype.hasOwnProperty.call(escapes, char) ? escapes[char] : '\\' + char;
                    escaped = false;
                } else if (char === '\\') {
                    escaped = true;
                } else {
                    value += char;
                }
            }
            return null;
        }

        function readNumberValue(source, index) {
            const match = source.slice(index).match(/^\s*([+-]?\d+(?:\.\d+)?)/);
            return match ? match[1] : null;
        }

        function readBalancedValue(source, index) {
            index = skipWhitespace(source, index);
            const opener = source[index];
            const closer = opener === '{' ? '}' : opener === '[' ? ']' : null;
            if (!closer) {
                return null;
            }

            let depth = 0;
            let quote = null;
            let escaped = false;
            for (let i = index; i < source.length; i += 1) {
                const char = source[i];
                if (quote) {
                    if (escaped) {
                        escaped = false;
                    } else if (char === '\\') {
                        escaped = true;
                    } else if (char === quote) {
                        quote = null;
                    }
                    continue;
                }

                if (char === '"' || char === "'") {
                    quote = char;
                } else if (char === opener) {
                    depth += 1;
                } else if (char === closer) {
                    depth -= 1;
                    if (depth === 0) {
                        return source.slice(index, i + 1);
                    }
                }
            }
            return null;
        }

        function normalizeObjectLiteral(value) {
            if (!value) {
                return value;
            }

            const normalized = value
                .replace(/\bTrue\b/g, 'true')
                .replace(/\bFalse\b/g, 'false')
                .replace(/\bNone\b/g, 'null')
                .replace(/'/g, '"')
                .replace(/,\s*([}\]])/g, '$1');

            try {
                return JSON.stringify(JSON.parse(normalized), null, 2);
            } catch (error) {
                return value;
            }
        }

        function extractTemplateValue(source, key, valueType) {
            const keyPattern = new RegExp('[\\\'\"]' + key + '[\\\'\"]\\s*:', 'm');
            const match = keyPattern.exec(source);
            if (!match) {
                return null;
            }

            const valueIndex = match.index + match[0].length;
            if (valueType === 'number') {
                return readNumberValue(source, valueIndex);
            }
            if (valueType === 'object') {
                return readBalancedValue(source, valueIndex);
            }
            return readQuotedValue(source, valueIndex);
        }

        function extractFirstValue(source, keys, valueType) {
            for (const key of keys) {
                const value = extractTemplateValue(source, key, valueType);
                if (value !== null) {
                    return value;
                }
            }
            return null;
        }

        function parseTemplate(source) {
            const parsed = {};
            fieldDefinitions.forEach(function (definition) {
                const value = extractFirstValue(source, definition.keys, 'string');
                if (value !== null) {
                    parsed[definition.elementId] = value;
                }
            });

            const answerCount = extractTemplateValue(source, 'answer_count', 'number');
            if (answerCount !== null) {
                parsed.answer_count = answerCount;
            }

            const generationStrategy = extractTemplateValue(source, 'generation_strategy', 'object');
            if (generationStrategy !== null) {
                parsed.generation_strategy = normalizeObjectLiteral(generationStrategy);
            } else {
                const strategyString = extractTemplateValue(source, 'generation_strategy', 'string');
                if (strategyString !== null) {
                    parsed.generation_strategy = normalizeObjectLiteral(strategyString);
                }
            }

            const imageFilename = extractTemplateValue(source, 'image_filename', 'string');
            if (imageFilename !== null) {
                parsed.image_filename = imageFilename;
            }
            return parsed;
        }

        function setElementValue(elementId, value) {
            const element = document.getElementById(elementId);
            if (!element) {
                return false;
            }

            if (elementId === 'answer_count' && !['1', '2', '3'].includes(String(value))) {
                return false;
            }
            if (elementId === 'difficulty' && !['easy', 'medium', 'hard'].includes(String(value))) {
                return false;
            }

            element.value = value;
            element.dispatchEvent(new Event('input', {bubbles: true}));
            element.dispatchEvent(new Event('change', {bubbles: true}));
            return true;
        }

        function fillForm(parsed) {
            let filledCount = 0;
            Object.entries(parsed).forEach(function ([elementId, value]) {
                if (elementId === 'image_filename') {
                    return;
                }
                if (setElementValue(elementId, value)) {
                    filledCount += 1;
                }
            });

            if (imageHint && parsed.image_filename) {
                imageHint.textContent = '模板中识别到图片：' + parsed.image_filename + '。浏览器不能自动选择本地文件；如需替换当前图片，请在上传框中选择原图。';
                imageHint.classList.remove('d-none');
            }
            return filledCount;
        }

        function parseAndFill() {
            const source = importTextarea.value.trim();
            if (!source) {
                setStatus('请先粘贴题目模板。', 'warning');
                return;
            }

            const parsed = parseTemplate(source);
            const filledCount = fillForm(parsed);
            if (filledCount > 0) {
                setStatus('已替换 ' + filledCount + ' 个字段；模板中未提供的内容保持不变，请检查后保存。', 'success');
            } else {
                setStatus('没有识别到可替换的字段，请确认模板包含 name、text、variables、formula 等键。', 'danger');
            }
        }

        parseButton.addEventListener('click', parseAndFill);
        clearButton.addEventListener('click', function () {
            importTextarea.value = '';
            hideStatus();
        });
        importTextarea.addEventListener('paste', function () {
            window.setTimeout(parseAndFill, 0);
        });
    });
}());
