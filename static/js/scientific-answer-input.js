(function () {
    'use strict';

    class ScientificAnswerInput {
        constructor(root) {
            this.root = root;
            this.rows = Array.from(root.querySelectorAll('[data-answer-row]'));
            this.rows.forEach(row => {
                row.querySelectorAll('input:not([type="hidden"])').forEach(input => {
                    input.addEventListener(input.type === 'radio' ? 'change' : 'input', () => this.sync(row));
                });
                row.querySelector('[data-exponent-sign]').addEventListener('click', () => {
                    const input = row.querySelector('[data-exponent]');
                    const value = input.value.trim();
                    input.value = value.startsWith('-') ? value.slice(1) : '-' + value.replace(/^\+/, '');
                    this.sync(row);
                    input.focus();
                });
                this.sync(row);
            });
        }

        read(row) {
            const mantissa = row.querySelector('[data-mantissa]').value.trim();
            const exponent = row.querySelector('[data-exponent]').value.trim();
            const sign = row.querySelector('input[type="radio"]:checked').value;
            if (!mantissa) return {error: '请填写尾数', field: '[data-mantissa]', empty: true};
            if (!/^(?:\d+(?:\.\d*)?|\.\d+)$/.test(mantissa)) {
                return {error: '尾数请输入非负小数，正负号在左侧选择', field: '[data-mantissa]'};
            }
            if (!/^[+-]?\d+$/.test(exponent)) return {error: '指数请输入整数，可为负数', field: '[data-exponent]'};
            // Parse as one number to avoid intermediate rounding or power overflow.
            const value = Number(`${sign}${mantissa}e${exponent}`);
            if (!Number.isFinite(value) || (Number(mantissa) !== 0 && value === 0)) {
                return {error: '数值超出可计算范围，请检查尾数和指数', field: '[data-exponent]'};
            }
            return {value, mantissa, exponent, sign};
        }

        sync(row) {
            const result = this.read(row);
            row.querySelector('input[type="hidden"]').value = result.error ? '' : String(result.value);
            const preview = document.getElementById(`answer-preview${row.dataset.answerRow}`);
            preview.replaceChildren();
            if (result.error) {
                preview.textContent = result.empty ? '未填写' : '待完善';
                preview.classList.add('is-empty');
            } else {
                preview.classList.remove('is-empty');
                if (result.value === 0) {
                    preview.append(document.createTextNode('0'));
                } else {
                    preview.append(document.createTextNode(`${result.sign === '-' ? '−' : '+'} ${result.mantissa} × 10`));
                    const sup = document.createElement('sup');
                    sup.textContent = result.exponent;
                    preview.append(sup);
                }
                if (row.dataset.unit) preview.append(document.createTextNode(' ' + row.dataset.unit));
            }
            if (row.dataset.validated === 'true') this.showError(row, result);
            return result;
        }

        showError(row, result) {
            const error = row.querySelector('[data-answer-error]');
            error.textContent = result.error || '';
            error.hidden = !result.error;
            row.querySelectorAll('[data-mantissa], [data-exponent]').forEach(input => {
                input.setAttribute('aria-invalid', String(Boolean(result.error && input.matches(result.field))));
            });
        }

        values() {
            return this.rows.map(row => this.sync(row).value);
        }

        validate() {
            let firstError = null;
            this.rows.forEach(row => {
                row.dataset.validated = 'true';
                const result = this.sync(row);
                if (result.error && !firstError) firstError = {row, ...result};
            });
            if (firstError) {
                firstError.row.querySelector(firstError.field).focus();
                return `答案 ${firstError.row.dataset.answerRow}：${firstError.error}`;
            }
            return null;
        }

        clear() {
            this.rows.forEach(row => {
                delete row.dataset.validated;
                row.querySelector('[data-mantissa]').value = '';
                row.querySelector('[data-exponent]').value = '0';
                row.querySelector('input[value="+"]').checked = true;
                this.showError(row, {});
                this.sync(row);
            });
        }

        setDisabled(disabled) {
            this.root.querySelectorAll('input:not([type="hidden"]), button').forEach(input => {
                input.disabled = disabled;
            });
        }

        hasInput() {
            return this.rows.some(row => row.querySelector('[data-mantissa]').value.trim() !== '' ||
                !['', '0'].includes(row.querySelector('[data-exponent]').value.trim()) ||
                row.querySelector('input[value="-"]').checked);
        }
    }

    window.ScientificAnswerInput = ScientificAnswerInput;
})();
