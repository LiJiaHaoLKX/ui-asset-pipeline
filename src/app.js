document.querySelectorAll('button').forEach((button) => {
  button.addEventListener('click', () => {
    if (button.closest('.bottom-nav')) {
      document.querySelectorAll('.bottom-nav button').forEach((item) => item.classList.remove('active'));
      button.classList.add('active');
    }
  });
});
