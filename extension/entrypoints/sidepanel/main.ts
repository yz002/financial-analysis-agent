document
  .querySelector<HTMLButtonElement>('#signin-google')
  ?.addEventListener('click', () => {
    console.log('[sidepanel] Sign in with Google clicked (placeholder — OAuth wired in session 2)');
  });

document
  .querySelector<HTMLButtonElement>('#signin-microsoft')
  ?.addEventListener('click', () => {
    console.log('[sidepanel] Sign in with Microsoft clicked (placeholder — OAuth wired in session 2)');
  });
