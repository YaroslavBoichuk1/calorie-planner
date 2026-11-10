// Перед незворотною дією (видалення) питаємо підтвердження. Без JavaScript форма спрацьовує одразу.
document.addEventListener('submit', (event) => {
    const form = event.target.closest('form[data-confirm]');
    if (form && !window.confirm(form.dataset.confirm)) {
        event.preventDefault();
    }
});
