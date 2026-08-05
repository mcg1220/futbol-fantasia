// ── Locker Room feed: rendering, infinite scroll, composers, interactions ──

let feedPosts = INITIAL_POSTS.slice();
let editingPostId = null;

function getMemeManagerId() {
  return document.getElementById('meme-as-manager').value;
}
function getMemeManagerName() {
  const id = getMemeManagerId();
  const m = MANAGERS.find(mg => String(mg.id) === String(id));
  return m ? m.name : '';
}
function requireMemeManager() {
  const id = getMemeManagerId();
  if (!id) {
    alert('Select who you\'re posting as first.');
    return null;
  }
  return id;
}
function onMemeManagerChange() {
  localStorage.setItem('memeAsManager', getMemeManagerId());
}
(function restoreMemeManager() {
  const saved = localStorage.getItem('memeAsManager');
  if (saved) {
    const sel = document.getElementById('meme-as-manager');
    if (Array.from(sel.options).some(o => o.value === saved)) sel.value = saved;
  }
})();

function closeModal(id) {
  document.getElementById(id).style.display = 'none';
}
function openModal(id) {
  document.getElementById(id).style.display = 'flex';
}

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str == null ? '' : str;
  return div.innerHTML;
}

function formatTimestamp(iso) {
  try {
    return new Date(iso).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
  } catch (e) { return iso; }
}

function renderReactions(post) {
  const myId = getMemeManagerId();
  const counts = {};
  const mine = new Set();
  (post.reactions || []).forEach(r => {
    counts[r.emoji] = (counts[r.emoji] || 0) + 1;
    if (String(r.manager_id) === String(myId)) mine.add(r.emoji);
  });
  return EMOJI_PALETTE.map(emoji => {
    const count = counts[emoji] || 0;
    const mineClass = mine.has(emoji) ? 'mine' : '';
    return `<button type="button" class="meme-reaction-btn ${mineClass}" onclick="toggleReaction(${post.id}, '${emoji}')">${emoji}${count ? ' ' + count : ''}</button>`;
  }).join('');
}

function renderComments(post) {
  const myId = getMemeManagerId();
  const rows = (post.comments || []).map(c => {
    const canDelete = String(c.manager_id) === String(myId);
    return `<div class="meme-comment-row" id="comment-${c.id}">
      <span class="meme-comment-body"><strong>${escapeHtml(c.manager_name)}:</strong> ${escapeHtml(c.body)}</span>
      ${canDelete ? `<span class="meme-comment-delete" onclick="deleteComment(${post.id}, ${c.id})">delete</span>` : ''}
    </div>`;
  }).join('');
  return `<div id="comments-section-${post.id}">
    <div class="meme-comments-list">${rows}</div>
    <div class="meme-comment-add">
      <input type="text" class="player-search-input" id="comment-input-${post.id}" placeholder="Add a comment..." onkeydown="if(event.key==='Enter') submitComment(${post.id})" />
      <button type="button" class="action-btn" onclick="submitComment(${post.id})">Post</button>
    </div>
  </div>`;
}

function renderPostBody(post) {
  if (post.post_type === 'image') {
    return `<img src="/static/${post.image_path}" class="meme-post-img" />`;
  }
  if (post.embed_html) {
    return post.embed_html;
  }
  return `<a href="${escapeHtml(post.link_url)}" target="_blank" rel="noopener">${escapeHtml(post.link_url)}</a>`;
}

function renderPostCard(post) {
  const myId = getMemeManagerId();
  const isMine = String(post.manager_id) === String(myId);
  const editedLine = post.edited
    ? `<div class="meme-post-meta">Edited ${formatTimestamp(post.updated_at)} by ${escapeHtml(post.manager_name)}</div>`
    : '';
  return `
    <div class="meme-post-card" id="meme-post-${post.id}">
      <div class="meme-post-header">
        <div>
          <div class="meme-post-author">${escapeHtml(post.manager_name)} <span style="font-weight:400;color:var(--text-muted)">— ${escapeHtml(post.team_name)}</span></div>
          <div class="meme-post-meta">${formatTimestamp(post.created_at)}</div>
          ${editedLine}
        </div>
        ${isMine ? `
        <div style="margin-left:auto;display:flex;gap:6px">
          <button type="button" class="action-btn" onclick="openEditPost(${post.id})">Edit</button>
          <button type="button" class="action-btn" onclick="deletePost(${post.id})">Delete</button>
        </div>` : ''}
      </div>
      ${post.caption ? `<div class="meme-post-caption">${escapeHtml(post.caption)}</div>` : ''}
      ${renderPostBody(post)}
      <div class="meme-post-actions" id="reactions-${post.id}">${renderReactions(post)}</div>
      ${renderComments(post)}
    </div>`;
}

// Scripts inserted via innerHTML never execute (standard DOM behavior) —
// oEmbed HTML from Twitter/Reddit/TikTok relies on a <script> to hydrate
// the placeholder markup into a real rich embed. Re-creating each script
// tag makes the browser actually run it.
function activateEmbedScripts(container) {
  container.querySelectorAll('script').forEach(oldScript => {
    const newScript = document.createElement('script');
    Array.from(oldScript.attributes).forEach(attr => newScript.setAttribute(attr.name, attr.value));
    newScript.textContent = oldScript.textContent;
    oldScript.replaceWith(newScript);
  });
}

function renderFeed() {
  const container = document.getElementById('meme-feed');
  container.innerHTML = feedPosts.map(renderPostCard).join('');
  activateEmbedScripts(container);
}

// Targeted updates for reactions/comments so we don't tear down and
// rebuild hydrated embeds (Twitter/Reddit/TikTok iframes) on every
// interaction elsewhere on the page.
function updateReactionsUI(postId) {
  const post = findPost(postId);
  const el = document.getElementById(`reactions-${postId}`);
  if (post && el) el.innerHTML = renderReactions(post);
}
function updateCommentsUI(postId) {
  const post = findPost(postId);
  const el = document.getElementById(`comments-section-${postId}`);
  if (post && el) el.outerHTML = renderComments(post);
}

function findPost(postId) {
  return feedPosts.find(p => p.id === postId);
}

function moveOrInsertAtTop(post) {
  feedPosts = feedPosts.filter(p => p.id !== post.id);
  feedPosts.unshift(post);
  renderFeed();
}

renderFeed();

// ── Infinite scroll ─────────────────────────────────────────────────────
let feedLoading = false;
const sentinel = document.getElementById('meme-feed-sentinel');
const observer = new IntersectionObserver((entries) => {
  if (entries[0].isIntersecting) loadMoreFeed();
}, { rootMargin: '200px' });
observer.observe(sentinel);

async function loadMoreFeed() {
  if (feedLoading || !HAS_MORE || feedPosts.length === 0) return;
  feedLoading = true;
  document.getElementById('meme-feed-loading').style.display = 'block';
  const last = feedPosts[feedPosts.length - 1];
  const res = await fetch(`/memes/feed?before=${encodeURIComponent(last.updated_at)}&before_id=${last.id}`);
  const data = await res.json();
  feedPosts = feedPosts.concat(data.posts);
  HAS_MORE = data.has_more;
  renderFeed();
  document.getElementById('meme-feed-loading').style.display = 'none';
  if (!HAS_MORE) document.getElementById('meme-feed-end').style.display = 'block';
  feedLoading = false;
}

// ── Reactions ────────────────────────────────────────────────────────────
async function toggleReaction(postId, emoji) {
  const managerId = requireMemeManager();
  if (!managerId) return;
  const res = await fetch(`/memes/${postId}/react`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ manager_id: managerId, emoji })
  });
  const data = await res.json();
  if (!res.ok) { alert(data.error || 'Reaction failed'); return; }
  const post = findPost(postId);
  if (!post) return;
  post.reactions = post.reactions || [];
  if (data.on) {
    post.reactions.push({ emoji, manager_id: parseInt(managerId), manager_name: getMemeManagerName() });
  } else {
    const idx = post.reactions.findIndex(r => r.emoji === emoji && String(r.manager_id) === String(managerId));
    if (idx >= 0) post.reactions.splice(idx, 1);
  }
  updateReactionsUI(postId);
}

// ── Comments ─────────────────────────────────────────────────────────────
async function submitComment(postId) {
  const managerId = requireMemeManager();
  if (!managerId) return;
  const input = document.getElementById(`comment-input-${postId}`);
  const body = input.value.trim();
  if (!body) return;
  const res = await fetch(`/memes/${postId}/comment`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ manager_id: managerId, body })
  });
  const data = await res.json();
  if (!res.ok) { alert(data.error || 'Comment failed'); return; }
  const post = findPost(postId);
  if (!post) return;
  post.comments = post.comments || [];
  post.comments.push({ id: data.id, manager_id: parseInt(managerId), manager_name: data.manager_name, body });
  updateCommentsUI(postId);
}

async function deleteComment(postId, commentId) {
  const managerId = requireMemeManager();
  if (!managerId) return;
  if (!confirm('Delete this comment?')) return;
  const res = await fetch(`/memes/comment/${commentId}/delete`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ manager_id: managerId })
  });
  const data = await res.json();
  if (!res.ok) { alert(data.error || 'Delete failed'); return; }
  const post = findPost(postId);
  if (post) post.comments = (post.comments || []).filter(c => c.id !== commentId);
  updateCommentsUI(postId);
}

// ── Delete post ──────────────────────────────────────────────────────────
async function deletePost(postId) {
  const managerId = requireMemeManager();
  if (!managerId) return;
  if (!confirm('Delete this post?')) return;
  const res = await fetch(`/memes/${postId}/delete`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ manager_id: managerId })
  });
  const data = await res.json();
  if (!res.ok) { alert(data.error || 'Delete failed'); return; }
  feedPosts = feedPosts.filter(p => p.id !== postId);
  renderFeed();
}

// ── Upload composer ──────────────────────────────────────────────────────
function openUploadComposer() {
  if (!requireMemeManager()) return;
  document.getElementById('upload-file-input').value = '';
  document.getElementById('upload-caption-input').value = '';
  document.getElementById('upload-error').style.display = 'none';
  openModal('upload-composer-modal');
}

async function submitUploadComposer() {
  const managerId = requireMemeManager();
  if (!managerId) return;
  const fileInput = document.getElementById('upload-file-input');
  const errEl = document.getElementById('upload-error');
  errEl.style.display = 'none';
  if (!fileInput.files.length) {
    errEl.textContent = 'Choose a file first.';
    errEl.style.display = 'block';
    return;
  }
  const form = new FormData();
  form.append('manager_id', managerId);
  form.append('caption', document.getElementById('upload-caption-input').value.trim());
  form.append('image', fileInput.files[0]);

  const res = await fetch('/memes/new', { method: 'POST', body: form });
  const data = await res.json();
  if (!res.ok) { errEl.textContent = data.error || 'Post failed'; errEl.style.display = 'block'; return; }
  closeModal('upload-composer-modal');
  window.location.reload();
}

// ── Link composer ────────────────────────────────────────────────────────
function openLinkComposer() {
  if (!requireMemeManager()) return;
  document.getElementById('link-url-input').value = '';
  document.getElementById('link-caption-input').value = '';
  document.getElementById('link-error').style.display = 'none';
  openModal('link-composer-modal');
}

async function submitLinkComposer() {
  const managerId = requireMemeManager();
  if (!managerId) return;
  const url = document.getElementById('link-url-input').value.trim();
  const errEl = document.getElementById('link-error');
  errEl.style.display = 'none';
  if (!url) {
    errEl.textContent = 'Paste a URL first.';
    errEl.style.display = 'block';
    return;
  }
  const form = new FormData();
  form.append('manager_id', managerId);
  form.append('caption', document.getElementById('link-caption-input').value.trim());
  form.append('link_url', url);

  const res = await fetch('/memes/new', { method: 'POST', body: form });
  const data = await res.json();
  if (!res.ok) { errEl.textContent = data.error || 'Post failed'; errEl.style.display = 'block'; return; }
  closeModal('link-composer-modal');
  window.location.reload();
}

// ── Edit post ─────────────────────────────────────────────────────────────
function openEditPost(postId) {
  const managerId = requireMemeManager();
  if (!managerId) return;
  const post = findPost(postId);
  if (!post) return;
  editingPostId = postId;
  document.getElementById('edit-error').style.display = 'none';
  document.getElementById('edit-caption-input').value = post.caption || '';
  document.getElementById('edit-image-field').style.display = post.post_type === 'image' ? 'block' : 'none';
  document.getElementById('edit-link-field').style.display = post.post_type === 'link' ? 'block' : 'none';
  document.getElementById('edit-file-input').value = '';
  document.getElementById('edit-link-input').value = post.link_url || '';
  openModal('edit-post-modal');
}

function openMemeCanvasForEdit() {
  const post = findPost(editingPostId);
  closeModal('edit-post-modal');
  openMemeCanvas(post);
}

async function submitEditPost() {
  const managerId = requireMemeManager();
  if (!managerId || !editingPostId) return;
  const errEl = document.getElementById('edit-error');
  errEl.style.display = 'none';

  const form = new FormData();
  form.append('manager_id', managerId);
  form.append('caption', document.getElementById('edit-caption-input').value.trim());
  const post = findPost(editingPostId);
  if (post.post_type === 'image') {
    const fileInput = document.getElementById('edit-file-input');
    if (fileInput.files.length) form.append('image', fileInput.files[0]);
  } else {
    form.append('link_url', document.getElementById('edit-link-input').value.trim());
  }

  const res = await fetch(`/memes/${editingPostId}/edit`, { method: 'POST', body: form });
  const data = await res.json();
  if (!res.ok) { errEl.textContent = data.error || 'Edit failed'; errEl.style.display = 'block'; return; }
  closeModal('edit-post-modal');
  window.location.reload();
}
