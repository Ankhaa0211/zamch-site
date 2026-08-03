function escapeHtml(value) {
    return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function safeUrl(url) {
    const value = String(url || '');
    if (value.startsWith('/photos/') || value.startsWith('/static/')) return value;
    return '/static/placeholder.jpg';
}

function money(n) {
    return Number(n || 0).toLocaleString() + ' ₮';
}

function setPageTitle(parts) {
    const bits = (Array.isArray(parts) ? parts : [parts])
        .map((p) => String(p || '').trim())
        .filter(Boolean);
    document.title = bits.length ? bits.join(' - ') + ' - ЗАМЧ' : 'ЗАМЧ';
}

function emptyState(message, actionsHtml = '') {
    return `<div class="empty-state">
        <p class="empty-state-msg">${message}</p>
        ${actionsHtml ? `<div class="empty-state-actions">${actionsHtml}</div>` : ''}
    </div>`;
}

function productCard(p) {
    const images = Array.isArray(p.images) ? p.images : [];
    const img = escapeHtml(safeUrl(images[0] || '/static/placeholder.jpg'));
    const kind = (p.listing_kind || p.category_slug || '').toLowerCase();
    const kindLabel = kind === 'combo'
        ? 'Иж бүрдэл'
        : kind === 'obud'
            ? 'Обуд'
            : kind === 'dugui'
                ? 'Дугуй'
                : '';
    const size = (p.width && p.ratio && p.diameter)
        ? `${p.width}/${p.ratio} R${p.diameter}`
        : (p.diameter ? `R${p.diameter}` : '');
    const bolt = (p.bolt_pattern || '').trim();
    const title = String(p.title || '').trim();
    const compact = (s) => String(s || '').replace(/[\s\/·\-]/g, '').toLowerCase();
    const titleC = compact(title);
    const showSize = size && !titleC.includes(compact(size));
    const showBolt = bolt && !titleC.includes(compact(bolt));
    const metaBits = [];
    if (showSize) metaBits.push(size);
    if (showBolt) metaBits.push(bolt);
    const stock = Number(p.stock || 0);
    return `
      <a class="product-tile" href="/product/${Number(p.id)}">
        <div class="product-tile-media">
          <img src="${img}" alt="">
          ${kindLabel ? `<span class="product-tile-badge">${escapeHtml(kindLabel)}</span>` : ''}
          ${stock < 1 ? '<span class="product-tile-soldout">Дууссан</span>' : ''}
        </div>
        <div class="product-tile-body">
          <div class="product-tile-store">${escapeHtml(p.store_name || '')}</div>
          <h3 class="product-tile-title">${escapeHtml(title)}</h3>
          ${metaBits.length ? `<div class="product-tile-meta">${escapeHtml(metaBits.join(' · '))}</div>` : ''}
          <div class="product-tile-price">${money(p.price)}</div>
        </div>
      </a>`;
}

let cartCache = { items: [], total: 0 };

function cartDropdownHtml(data) {
    const items = data.items || [];
    const total = data.total || 0;
    if (!items.length) {
        return `
          <div class="cart-dd-empty">Сагс хоосон</div>
          <a class="cart-dd-checkout" href="/">Бараа хайх</a>`;
    }
    const rows = items.map(item => {
        const img = escapeHtml(safeUrl((item.images || [])[0] || '/static/placeholder.jpg'));
        return `
          <div class="cart-dd-row">
            <img src="${img}" alt="">
            <div class="cart-dd-info">
              <div class="cart-dd-title">${escapeHtml(item.title)}</div>
              <div class="cart-dd-meta">${Number(item.quantity)} × ${money(item.price)}</div>
            </div>
            <button type="button" class="cart-dd-remove" data-remove-cart="${Number(item.id)}" aria-label="Хасах">×</button>
          </div>`;
    }).join('');
    return `
      <div class="cart-dd-list">${rows}</div>
      <div class="cart-dd-total"><span>Нийт</span><strong>${money(total)}</strong></div>
      <a class="cart-dd-checkout" href="/cart">Захиалах</a>`;
}

async function fetchCart() {
    const res = await fetch('/api/cart');
    const data = await res.json();
    cartCache = { items: data.items || [], total: data.total || 0 };
    return cartCache;
}

function updateCartBadge(count) {
    document.querySelectorAll('[data-cart-count]').forEach(el => {
        el.textContent = count;
        el.style.display = count > 0 ? 'flex' : 'none';
    });
}

async function refreshCartBadge() {
    try {
        const data = await fetchCart();
        const count = data.items.reduce((s, i) => s + Number(i.quantity || 0), 0);
        updateCartBadge(count);
        const body = document.getElementById('cartDropdownBody');
        if (body && document.getElementById('cartDropdown')?.classList.contains('is-open')) {
            body.innerHTML = cartDropdownHtml(data);
            bindCartDropdownActions();
        }
    } catch (_) {}
}

function bindCartDropdownActions() {
    document.querySelectorAll('[data-remove-cart]').forEach(btn => {
        btn.addEventListener('click', async (e) => {
            e.preventDefault();
            e.stopPropagation();
            const id = btn.getAttribute('data-remove-cart');
            const fd = new FormData();
            fd.append('item_id', id);
            await fetch('/api/cart/remove', { method: 'POST', body: fd });
            await refreshCartBadge();
            const body = document.getElementById('cartDropdownBody');
            if (body) {
                body.innerHTML = cartDropdownHtml(cartCache);
                bindCartDropdownActions();
            }
        });
    });
}

function closeCartDropdown() {
    const dd = document.getElementById('cartDropdown');
    const btn = document.getElementById('cartToggle');
    if (dd) dd.classList.remove('is-open');
    if (btn) btn.setAttribute('aria-expanded', 'false');
}

async function openCartDropdown() {
    const dd = document.getElementById('cartDropdown');
    const body = document.getElementById('cartDropdownBody');
    const btn = document.getElementById('cartToggle');
    if (!dd || !body) return;
    try {
        const data = await fetchCart();
        const count = data.items.reduce((s, i) => s + Number(i.quantity || 0), 0);
        updateCartBadge(count);
        body.innerHTML = cartDropdownHtml(data);
        bindCartDropdownActions();
    } catch (_) {
        body.innerHTML = '<div class="cart-dd-empty">Сагс ачаалж чадсангүй</div>';
    }
    dd.classList.add('is-open');
    if (btn) btn.setAttribute('aria-expanded', 'true');
}

function initCartMenu() {
    const mount = document.getElementById('cartMenuMount');
    if (!mount || mount.dataset.ready) return;
    mount.dataset.ready = '1';
    mount.innerHTML = `
      <div class="cart-menu">
        <button type="button" class="cart-icon-btn" id="cartToggle" aria-label="Сагс" aria-expanded="false" aria-haspopup="true">
          <i class="bi bi-bag"></i>
          <span class="cart-badge" data-cart-count style="display:none">0</span>
        </button>
        <div class="cart-dropdown" id="cartDropdown" role="dialog" aria-label="Сагс">
          <div class="cart-dd-head">Сагс</div>
          <div id="cartDropdownBody"></div>
        </div>
      </div>`;

    const toggle = document.getElementById('cartToggle');
    toggle?.addEventListener('click', async (e) => {
        e.stopPropagation();
        const dd = document.getElementById('cartDropdown');
        if (dd?.classList.contains('is-open')) {
            closeCartDropdown();
        } else {
            await openCartDropdown();
        }
    });

    document.addEventListener('click', (e) => {
        const menu = document.querySelector('.cart-menu');
        if (menu && !menu.contains(e.target)) closeCartDropdown();
    });
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') closeCartDropdown();
    });
}

function markActiveNav() {
    const path = location.pathname.replace(/\/$/, '') || '/';
    document.querySelectorAll('.zamch-nav .nav-link').forEach((a) => {
        const href = (a.getAttribute('href') || '').replace(/\/$/, '') || '/';
        const active = href === '/' ? path === '/' : path === href || path.startsWith(href + '/');
        a.classList.toggle('active', active);
    });
}

async function loadAuthNav() {
    try {
        const res = await fetch('/api/auth/me');
        const data = await res.json();
        const user = data.user;
        const box = document.getElementById('authNav');
        if (!box) return;
        if (!user) {
            box.innerHTML = `
              <a href="/login" class="nav-auth-link">Нэвтрэх</a>
              <a href="/register" class="nav-auth-cta">Бүртгүүлэх</a>`;
            return;
        }
        const adminLink = user.role === 'admin'
            ? `<a href="/admin" class="nav-auth-link">Админ</a>`
            : '';
        box.innerHTML = `
          ${adminLink}
          <a href="/my-orders" class="nav-auth-link">Захиалга</a>
          <a href="/account" class="nav-auth-link">Профайл</a>
          <button type="button" class="nav-auth-link nav-auth-btn" id="logoutBtn">Гарах</button>`;
        document.getElementById('logoutBtn')?.addEventListener('click', async () => {
            await fetch('/api/auth/logout', { method: 'POST' });
            location.href = '/';
        });
    } catch (_) {}
}

document.addEventListener('DOMContentLoaded', () => {
    markActiveNav();
    initCartMenu();
    refreshCartBadge();
    loadAuthNav();
});
