// «Замінити страву»: поле пошуку ховає страви, у назві чи складі яких нема введених слів.
// Без JavaScript поля не видно, а весь список гортається як раніше.
document.querySelectorAll('.swap-search').forEach((input) => { input.hidden = false; });

// Українські слова змінюються в кінці: «курка» → «куряче», «картопля» → «картоплею», «риба» → «рибна».
// Тому з довгого слова відкидаємо дві останні літери, а з короткого — кінцеву голосну, і шукаємо початок.
function stem(word) {
    if (word.length >= 5) return word.slice(0, -2);
    if (word.length === 4 && /[аяоеєиіїую]$/.test(word)) return word.slice(0, -1);
    return word;
}

function normalize(text) {
    return text.toLocaleLowerCase('uk').replace(/[’ʼ]/g, "'");
}

document.addEventListener('input', (event) => {
    const input = event.target.closest('.swap-search');
    if (!input) return;

    const body = input.closest('.swap-body');
    const words = normalize(input.value).split(/\s+/).filter(Boolean).map(stem);
    let shown = 0;
    body.querySelectorAll('.swap-list > li').forEach((item) => {
        // шукаємо з початку слова: «риба» має знайти «рибна», але не «грибами»
        const itemWords = item.dataset.search.split(/[^\p{L}\d']+/u);
        const fits = words.every((word) => itemWords.some((itemWord) => itemWord.startsWith(word)));
        item.hidden = !fits;
        if (fits) shown += 1;
    });

    const empty = body.querySelector('.swap-empty');
    empty.hidden = shown > 0;
    empty.textContent = shown > 0 ? '' : `Нічого не знайшли за «${input.value.trim()}». Спробуй коротше слово, наприклад «кур» чи «сир».`;
    body.querySelector('.swap-list').scrollTop = 0;
});

// Esc очищає поле й повертає весь список.
document.addEventListener('keydown', (event) => {
    const input = event.target.closest('.swap-search');
    if (!input || event.key !== 'Escape' || !input.value) return;
    event.preventDefault();
    input.value = '';
    input.dispatchEvent(new Event('input', { bubbles: true }));
});
