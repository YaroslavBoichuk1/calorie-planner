// Галочки «куплено» без перезавантаження сторінки. Без JavaScript ті самі форми працюють звичайним способом.
document.addEventListener('submit', async (event) => {
    const form = event.target.closest('form.check-form');
    if (!form) return;
    event.preventDefault();

    const data = new FormData(form);
    try {
        const response = await fetch(form.action, {
            method: 'POST', body: data, headers: {'X-Requested-With': 'fetch'}, credentials: 'same-origin',
        });
        if (!response.ok) throw new Error(response.statusText);
    } catch (error) {
        form.submit();   // не вийшло — відправляємо форму звичайно
        return;
    }

    const bought = data.get('bought') === '1';
    form.closest('li').classList.toggle('is-bought', bought);
    form.querySelector('input[name="bought"]').value = bought ? '0' : '1';
    form.querySelector('button').setAttribute('aria-pressed', bought ? 'true' : 'false');

    const list = form.closest('.shopping-list');
    const progress = list.previousElementSibling;
    if (progress && progress.matches('[data-bought-progress]')) {
        const count = list.querySelectorAll('li.is-bought').length;
        const total = Number(progress.dataset.total) || 1;
        progress.querySelector('[data-bought-count]').textContent = count;
        progress.querySelector('[data-bought-bar]').style.width = `${Math.round(count / total * 100)}%`;
    }
});
