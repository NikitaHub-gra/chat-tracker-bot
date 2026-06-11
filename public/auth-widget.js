// auth-widget.js — shared "logged in as ..." badge + logout for hub pages.
// Include with <script src="/auth-widget.js"></script>. If the session is
// invalid, the server's auth gate already redirects page loads to
// /login.html, so this mainly renders the badge for already-authed pages.
//
// Preferred placement: appended as a normal flex child into the page's
// header right-side container (`.header-right`, or the last direct-child
// <div> of <header>). Falls back to a fixed-position badge (wrapped so it
// never overlaps header buttons) if no header container is found.
(function () {
  fetch('/api/dashboard/auth/me').then(r => r.json()).then(r => {
    if (!r.user) { location.href = '/login.html'; return; }
    const u = r.user;

    const el = document.createElement('div');
    el.id = 'hub-auth-widget';

    const name = document.createElement('span');
    name.textContent = u.name + (u.role === 'admin' ? ' · 👑' : '');
    el.appendChild(name);

    const logout = document.createElement('a');
    logout.href = '#';
    logout.textContent = 'Выйти';
    logout.style.cssText = 'color:#FF5252;text-decoration:none;font-weight:600;';
    logout.onclick = async (e) => {
      e.preventDefault();
      await fetch('/api/dashboard/auth/logout', { method: 'POST' });
      location.href = '/login.html';
    };
    el.appendChild(logout);

    // Try to find a header container to integrate into.
    const header = document.querySelector('header');
    let host = header ? header.querySelector('.header-right') : null;
    if (!host && header) {
      // Fall back to the last direct-child <div> of <header>, but only if
      // there are at least two — a single div is the left-side group and
      // the widget must not be appended into it (it would land next to the
      // title instead of on the right edge).
      const divs = Array.from(header.children).filter(c => c.tagName === 'DIV');
      if (divs.length > 1) {
        host = divs[divs.length - 1];
      } else {
        // Create a dedicated right-side container pinned to the header's end.
        host = document.createElement('div');
        host.style.cssText = 'display:flex;align-items:center;margin-left:auto;';
        header.appendChild(host);
      }
    }

    if (host) {
      el.style.cssText = `
        display: flex; align-items: center; gap: 10px;
        background: #1a1d2e; border-radius: 20px; padding: 6px 14px;
        font-size: 12px; color: #94a3b8;
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
        flex-shrink: 0; white-space: nowrap;
      `;
      host.style.flexWrap = 'wrap';
      host.appendChild(el);
    } else {
      // No header found at all — fixed badge that wraps and stays clear of content.
      el.style.cssText = `
        position: fixed; top: 10px; right: 14px; z-index: 9000;
        display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
        max-width: calc(100vw - 28px);
        background: #1a1d2e; border-radius: 20px; padding: 6px 14px;
        font-size: 12px; color: #94a3b8;
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
        box-shadow: 0 4px 16px rgba(0,0,0,0.35);
      `;
      document.body.appendChild(el);
    }
  }).catch(() => {});
})();
