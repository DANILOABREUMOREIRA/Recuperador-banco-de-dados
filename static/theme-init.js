// Escolhe o tema (claro ou escuro) ANTES de a página aparecer, para não piscar o tema errado.
// Vale o que o usuário gravou; sem escolha, o do sistema.
(function () {
  var theme = null;
  try { theme = localStorage.getItem('theme'); } catch (e) { /* sem armazenamento */ }
  if (theme !== 'light' && theme !== 'dark') {
    theme = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }
  document.documentElement.setAttribute('data-theme', theme);
})();
