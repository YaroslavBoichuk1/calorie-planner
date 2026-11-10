// Панель «Ціль»: поки людина вводить числа, одразу показуємо грами БЖВ і суму відсотків.
// Без JavaScript ці підказки оновлюються після натискання «Скласти раціон».
document.addEventListener('input', (event) => {
    const form = event.target.closest('.goal-form');
    if (!form) return;

    const calories = Number(form.querySelector('[data-goal-calories]').value) || 0;
    let sum = 0;
    form.querySelectorAll('input[data-kcal]').forEach((input) => {
        const pct = Number(input.value) || 0;
        sum += pct;
        const grams = Math.round(calories * pct / 100 / Number(input.dataset.kcal));
        input.closest('.macro-input').querySelector('[data-grams]').textContent = `≈ ${grams} г на день`;
    });

    const sumLine = form.querySelector('[data-goal-sum]');
    sumLine.textContent = sum === 100 ? 'Разом 100%' : `Разом ${sum}% — має бути 100%`;
    sumLine.classList.toggle('is-wrong', sum !== 100);
});
