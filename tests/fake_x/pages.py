"""Server-rendered HTML and inline JS for the offline fake x.com.

The pages are deliberately tiny: no external scripts, no network. Everything the
driver needs to observe is either markup (selectors from
``docs/research/selectors.json``) or a toast produced by inline JS talking to the
fake's ``api.x.com`` route. ``dom="new"`` renders the observed ``x-web`` client;
``dom="legacy"`` renders the classic ``data-testid`` markup used as fallback.

WHY hand-written strings instead of a template engine: the fake must stay
readable and dependency-free, and keeping markup and its wiring together makes
it obvious which selector each test relies on. Because these are literal
strings embedded in Python, ``# ruff: noqa: E501`` would hide genuinely long
lines, so every line is kept under the 100-char limit instead.
"""

from __future__ import annotations

import html
import json
from typing import Any

_ACCEPT = "image/jpeg,image/png,image/webp,image/gif,video/mp4,video/quicktime"

ARTIFACT_CANARY = "csrf-canary-do-not-leak"

_AVATAR_SRC = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAE"
    "hQGAhKmMIQAAAABJRU5ErkJggg=="
)
"""Token-shaped canary embedded in every served page for artifact tests.

It appears in a ``csrf-token`` meta tag and a hidden authenticity input, so a
failure artifact must show ``[REDACTED]`` where the value used to be.
"""

_STYLE = """
:root { color-scheme: light; }
body { font-family: system-ui, sans-serif; margin: 0; padding: 24px;
  background: #fff; color: #0f1419; }
.fx-nav { display: flex; gap: 16px; align-items: center; margin-bottom: 24px; }
.fx-dialog { position: fixed; inset: 0; margin: auto; width: min(600px, 90vw);
  height: fit-content; max-height: 90vh; overflow: auto; background: #fff;
  border: 1px solid #cfd9de; border-radius: 16px; padding: 16px; z-index: 40;
  box-shadow: 0 8px 30px rgba(0,0,0,.2); }
.fx-dialog h2 { margin: 0 0 12px; font-size: 20px; }
textarea { width: 100%; min-height: 120px; font: inherit; padding: 8px;
  box-sizing: border-box; }
.fx-item { border-top: 1px solid #eff3f4; padding: 8px 0; }
.fx-previews { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 8px; }
[data-testid="attachments"]:empty { display: none; }
.fx-tile { position: relative; width: 96px; height: 96px; border-radius: 8px; overflow: visible;
  background: #eee; }
.fx-tile img { width: 100%; height: 100%; object-fit: cover; }
.fx-tile button { position: absolute; top: 2px; right: 2px; }
.fx-tools { display: flex; gap: 8px; align-items: center; margin-top: 8px; }
.fx-actions { display: flex; gap: 8px; justify-content: flex-end; margin-top: 12px; }
.fx-weight { color: #536471; font-size: 13px; }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
  overflow: hidden; clip: rect(0,0,0,0); white-space: nowrap; border: 0; }
[data-sonner-toaster] { position: fixed; bottom: 16px; left: 16px; display: flex;
  flex-direction: column; gap: 8px; z-index: 50; }
[data-sonner-toast] { background: #0f1419; color: #fff; padding: 12px;
  border-radius: 8px; display: flex; gap: 12px; align-items: center; }
[role="menu"] { position: relative; background: #fff; border: 1px solid #cfd9de;
  border-radius: 8px; padding: 4px; width: fit-content; }
[role="menuitem"] { display: block; width: 100%; text-align: left; padding: 8px;
  background: none; border: 0; cursor: pointer; }
iframe { width: 100%; height: 120px; border: 1px solid #ccc; }
[hidden] { display: none !important; }
"""

_COMMON_JS = r"""
const FX = window.__FAKE_X__;
window.__fxAltAddClicks = 0;
window.__fxAltIntro = { sure: 0, no: 0 };
window.__fxAltIntroShown = false;

function fxWeight(text) {
  const urls = (text.match(/https?:\/\/\S+/g) || []).length;
  const rest = text.replace(/https?:\/\/\S+/g, '');
  let total = urls * 23;
  for (const ch of rest) {
    total += ch.codePointAt(0) <= 0x24f ? 1 : 2;
  }
  return total;
}

function fxToast(message, isError, viewHref) {
  let toaster = document.querySelector('[data-sonner-toaster]');
  if (!toaster) {
    toaster = document.createElement('div');
    toaster.setAttribute('data-sonner-toaster', '');
    document.body.appendChild(toaster);
  }
  const toast = document.createElement('div');
  toast.setAttribute('data-sonner-toast', '');
  toast.setAttribute('role', 'status');
  if (FX.dom === 'legacy') {
    toast.setAttribute('data-testid', 'toast');
  }
  if (isError) {
    toast.setAttribute('data-error', '');
  }
  const label = document.createElement('span');
  label.textContent = message;
  toast.appendChild(label);
  if (viewHref) {
    const view = document.createElement('button');
    view.type = 'button';
    view.textContent = 'View';
    view.addEventListener('click', () => { location.href = viewHref; });
    toast.appendChild(view);
  }
  toaster.appendChild(toast);
  return toast;
}

function fxCloseDialog(dialog) {
  if (dialog) {
    dialog.remove();
  }
}

function fxDraftBlocks(input) {
  return Array.from(input.querySelectorAll('[data-block="true"]'));
}

function fxDraftShell() {
  return '<div data-contents="true"><div data-block="true">'
    + '<div><span><br data-text="true"></span></div></div></div>';
}

function fxDraftText(input) {
  const blocks = fxDraftBlocks(input);
  if (!blocks.length) {
    return null;
  }
  // Blocks joined by one newline: a blank line is an empty block and must not
  // contribute the extra newline innerText gives it for the placeholder br.
  return blocks.map((block) => block.textContent || '').join('\n');
}

function fxResetEditor(input) {
  if (!input) {
    return;
  }
  if (input.value !== undefined) {
    input.value = '';
    return;
  }
  if (fxDraftBlocks(input).length) {
    input.innerHTML = fxDraftShell();
    return;
  }
  input.innerText = '';
}

function fxNormaliseDraft(input) {
  // Keep the Draft.js shape: every line is one div[data-block], every run of
  // text sits in span[data-text]; an empty line is span > br[data-text].
  const blocks = fxDraftBlocks(input);
  if (!blocks.length) {
    return;
  }
  const selection = window.getSelection();
  const anchor = selection && selection.anchorNode;
  const anchorElement = anchor
    ? (anchor.nodeType === Node.ELEMENT_NODE ? anchor : anchor.parentElement)
    : null;
  const current = anchorElement && anchorElement.closest
    ? anchorElement.closest('[data-block="true"]') : null;
  blocks.forEach((block) => {
    const text = block.textContent || '';
    if (!text) {
      if (block.querySelector('br[data-text="true"]')
          && block.querySelectorAll('[data-text="true"]').length === 1) {
        return;
      }
      block.innerHTML = '<div><span><br data-text="true"></span></div>';
      return;
    }
    const spans = Array.from(block.querySelectorAll('span[data-text="true"]'));
    if (spans.length === 1 && (spans[0].textContent || '') === text) {
      return;
    }
    block.innerHTML = '<div><span data-text="true"></span></div>';
    const span = block.querySelector('span[data-text="true"]');
    if (span) {
      span.textContent = text;
    }
    if (block === current && selection && span) {
      const range = document.createRange();
      range.selectNodeContents(span);
      range.collapse(false);
      selection.removeAllRanges();
      selection.addRange(range);
    }
  });
}

function fxDraftBreak(input) {
  const contents = input.querySelector('[data-contents]') || input;
  const selection = window.getSelection();
  const anchor = selection && selection.anchorNode;
  const anchorElement = anchor
    ? (anchor.nodeType === Node.ELEMENT_NODE ? anchor : anchor.parentElement)
    : null;
  const current = anchorElement && anchorElement.closest
    ? anchorElement.closest('[data-block="true"]') : null;
  const block = document.createElement('div');
  block.setAttribute('data-block', 'true');
  block.innerHTML = '<div><span><br data-text="true"></span></div>';
  if (current) {
    current.after(block);
  } else {
    contents.appendChild(block);
  }
  const span = block.querySelector('span');
  if (selection && span) {
    const range = document.createRange();
    range.setStart(span, 0);
    range.collapse(true);
    selection.removeAllRanges();
    selection.addRange(range);
  }
}

function fxTypeaheadToken(input) {
  if (!fxDraftBlocks(input).length) {
    return null;
  }
  const text = (fxDraftText(input) || '').replace(/\r\n?/g, '\n');
  const line = text.split('\n').pop() || '';
  const match = /(?:@[A-Za-z0-9_]{1,15}|#[A-Za-z0-9_]+|\$[A-Za-z]+)$/.exec(line);
  return match ? match[0] : null;
}

function fxTypeaheadDropdown(input) {
  const item = input.closest('.fx-item') || input.parentElement;
  return item ? item.querySelector('[data-fx-typeahead]') : null;
}

function fxUpdateTypeahead(input) {
  const token = fxTypeaheadToken(input);
  const existing = fxTypeaheadDropdown(input);
  if (!token) {
    if (existing) {
      existing.remove();
    }
    return;
  }
  if (existing) {
    return;
  }
  const item = input.closest('.fx-item') || input.parentElement;
  if (!item) {
    return;
  }
  const dropdown = document.createElement('div');
  dropdown.setAttribute('role', 'listbox');
  dropdown.setAttribute('data-fx-typeahead', token);
  dropdown.style.cssText = 'background:#fff;border:1px solid #cfd9de;padding:2px 6px;'
    + 'font-size:11px;margin-top:2px';
  const option = document.createElement('div');
  option.setAttribute('role', 'option');
  option.textContent = token;
  dropdown.appendChild(option);
  item.appendChild(dropdown);
}

function fxSelectTypeahead(input) {
  // Choosing the highlighted suggestion keeps the token and adds one space.
  window.__fxTypeaheadSelections = (window.__fxTypeaheadSelections || 0) + 1;
  const dropdown = fxTypeaheadDropdown(input);
  if (dropdown) {
    dropdown.remove();
  }
  input.focus();
  const selection = window.getSelection();
  if (selection && selection.rangeCount) {
    document.execCommand('insertText', false, ' ');
  } else {
    const blocks = fxDraftBlocks(input);
    const last = blocks[blocks.length - 1];
    if (last) {
      last.textContent = (last.textContent || '') + ' ';
    }
  }
  fxNormaliseDraft(input);
  updateSubmit();
}

function fxComposerText(item) {
  const input = item.querySelector('[data-fx-input]');
  if (!input) {
    return '';
  }
  if (input.value !== undefined) {
    return input.value;
  }
  const draft = fxDraftText(input);
  return draft !== null ? draft : input.innerText;
}

function fxWireComposer(dialog) {
  if (!dialog || dialog.dataset.fxWired === '1') {
    return;
  }
  dialog.dataset.fxWired = '1';
  const itemsRoot = dialog.querySelector('[data-fx-items]');
  const form = dialog.querySelector('[data-fx-form]');
  const submit = dialog.querySelector('[data-fx-submit]');
  const addButton = dialog.querySelector('[data-fx-add]');
  const closeButton = dialog.querySelector('[data-fx-close]');
  const fileInput = dialog.querySelector('[data-fx-file]');
  const addMedia = dialog.querySelector('[data-fx-add-media]');
  const counter = dialog.querySelector('[data-fx-weight]');
  const blankItem = itemsRoot && itemsRoot.querySelector('.fx-item')
    ? itemsRoot.querySelector('.fx-item').cloneNode(true) : null;
  let activeItem = itemsRoot ? itemsRoot.querySelector('.fx-item') : null;
  let pendingUploads = 0;
  let uploadFailed = false;
  const altTexts = {};

  function items() {
    return itemsRoot ? Array.from(itemsRoot.querySelectorAll('.fx-item')) : [];
  }

  function updateSubmit() {
    if (!submit) {
      return;
    }
    const all = items();
    const empty = all.length === 0 || all.some((item) => !fxComposerText(item).trim());
    const over = all.some((item) => fxWeight(fxComposerText(item)) > FX.max_weight);
    submit.disabled = empty || over || pendingUploads > 0 || uploadFailed;
    if (addButton && FX.dom === 'legacy') {
      addButton.hidden = empty;
    }
    const many = all.length > 1;
    const reply = FX.mode === 'reply';
    submit.textContent = reply ? 'Reply' : (many ? 'Post all' : 'Post');
    if (counter) {
      counter.textContent = String(
        all.reduce((sum, item) => sum + fxWeight(fxComposerText(item)), 0),
      );
    }
  }

  function mediaFor(item) {
    try {
      return JSON.parse(item.dataset.mediaIds || '[]');
    } catch (err) {
      return [];
    }
  }

  function showSaveDialog() {
    const save = document.createElement('div');
    save.setAttribute('role', 'dialog');
    save.className = 'fx-dialog';
    save.innerHTML =
      '<h2>Save post?</h2>' +
      '<p>You can save this to send later from your drafts.</p>' +
      '<div class="fx-actions">' +
      '<button type="button" data-fx-discard>Discard</button>' +
      '<button type="button" data-fx-save>Save</button>' +
      '</div>';
    document.body.appendChild(save);
    if (FX.dom === 'legacy') {
      save.querySelector('[data-fx-save]')
        .setAttribute('data-testid', 'confirmationSheetConfirm');
      save.querySelector('[data-fx-discard]')
        .setAttribute('data-testid', 'confirmationSheetCancel');
    }
    const finish = () => { save.remove(); fxCloseDialog(dialog); };
    save.querySelector('[data-fx-discard]').addEventListener('click', finish);
    save.querySelector('[data-fx-save]').addEventListener('click', finish);
  }

  function reindex() {
    if (FX.dom !== 'legacy') {
      return;
    }
    items().forEach((item, index) => {
      const area = item.querySelector('[data-testid^="tweetTextarea_"]');
      if (area) {
        area.setAttribute('data-testid', 'tweetTextarea_' + index);
      }
    });
  }

  function wireItem(item) {
    const input = item.querySelector('[data-fx-input]');
    item.dataset.mediaIds = '[]';
    if (input) {
      input.addEventListener('input', () => {
        fxNormaliseDraft(input);
        fxUpdateTypeahead(input);
        updateSubmit();
      });
      input.addEventListener('focusin', () => { activeItem = item; });
      input.addEventListener('focus', () => { activeItem = item; });
      input.addEventListener('keydown', (event) => {
        if (fxDraftBlocks(input).length && fxTypeaheadDropdown(input)) {
          if (event.key === 'Escape') {
            event.preventDefault();
            fxCloseDialog(dialog);
            return;
          }
          if (event.key === 'Enter' || event.key === 'Tab') {
            event.preventDefault();
            fxSelectTypeahead(input);
            return;
          }
        }
        if (event.key !== 'Enter') {
          return;
        }
        if (event.metaKey || event.ctrlKey) {
          // The keyboard submit shortcut a person can use when the Post
          // button is covered; both web clients accept Meta/Control+Enter.
          event.preventDefault();
          if (form) {
            form.requestSubmit();
          }
          return;
        }
        if (event.shiftKey) {
          if (fxDraftBlocks(input).length) {
            event.preventDefault();
            fxDraftBreak(input);
            updateSubmit();
          }
          return;
        }
        // X submits on a bare Return. The fake ignores it entirely, so a
        // driver that types a line break without Shift loses the break
        // instead of silently submitting a half-written post.
        event.preventDefault();
      });
    }
    updateSubmit();
  }

  function addItem() {
    if (!blankItem || items().length >= 25) {
      return;
    }
    const clone = blankItem.cloneNode(true);
    clone.dataset.mediaIds = '[]';
    clone.querySelectorAll('.fx-tile').forEach((tile) => tile.remove());
    const area = clone.querySelector('[data-fx-input]');
    fxResetEditor(area);
    itemsRoot.appendChild(clone);
    reindex();
    wireItem(clone);
    activeItem = clone;
    if (area) {
      area.focus();
    }
    updateSubmit();
  }

  async function uploadFile(file, item, token, tile) {
    pendingUploads += 1;
    dialog.setAttribute('data-fx-uploading', '1');
    updateSubmit();
    let mediaId = null;
    let failed = false;
    try {
      const formData = new FormData();
      formData.append('command', 'INIT');
      formData.append('media_category', 'tweet_image');
      formData.append('total_bytes', String(file.size));
      formData.append('media_type', file.type || 'image/png');
      formData.append('fake_file_name', file.name);
      const response = await fetch('https://upload.x.com/i/media/upload.json', {
        method: 'POST',
        body: formData,
      });
      if (response.ok) {
        const payload = await response.json();
        mediaId = payload.media_id_string || null;
      } else {
        failed = true;
      }
    } catch (err) {
      failed = true;
    }
    if (mediaId) {
      const ids = mediaFor(item);
      const entry = { media_id: mediaId, token: token };
      if (altTexts[token]) {
        entry.alt_text = altTexts[token];
      }
      ids.push(entry);
      item.dataset.mediaIds = JSON.stringify(ids);
    } else {
      failed = true;
    }
    if (failed) {
      // X keeps Post disabled when an attachment failed; the user has to
      // remove or retry it, so the fake must not allow a silent post.
      uploadFailed = true;
      fxToast('Could not upload media. Try again.', true);
    }
    if (tile) {
      const progress = tile.querySelector('[role="progressbar"]');
      if (progress) {
        progress.remove();
      }
    }
    pendingUploads -= 1;
    if (pendingUploads === 0) {
      dialog.removeAttribute('data-fx-uploading');
    }
    updateSubmit();
  }

  async function handleFiles() {
    if (!fileInput || !fileInput.files) {
      return;
    }
    const target = activeItem || items()[0];
    const files = Array.from(fileInput.files);
    let accepted = target ? mediaFor(target).length : 0;
    for (const file of files) {
      if (!target || accepted >= 4) {
        break;
      }
      accepted += 1;
      const token = String(Date.now()) + '-' + String(Math.random()).slice(2);
      const preview = target.querySelector('[data-testid="attachments"]');
      if (preview) {
        const tile = document.createElement('div');
        tile.className = 'fx-tile';
        const group = document.createElement('div');
        group.setAttribute('role', 'group');
        group.setAttribute('aria-label', 'Media');
        group.style.cssText = 'position:relative;width:100%;height:100%';
        const wrapper = document.createElement('div');
        wrapper.style.cssText = 'position:absolute;inset:0;overflow:visible';
        const image = document.createElement('img');
        image.src = URL.createObjectURL(file);
        image.alt = file.name;
        image.style.pointerEvents = 'none';
        const edit = document.createElement('button');
        edit.type = 'button';
        edit.setAttribute('aria-label', 'Edit media');
        edit.textContent = 'Edit';
        edit.style.cssText = 'top:auto;right:auto;bottom:2px;left:2px';
        edit.addEventListener('click', () => { openMediaEditor(); });
        const remove = document.createElement('button');
        remove.type = 'button';
        remove.setAttribute('aria-label', 'Remove media');
        remove.textContent = '\u2715';
        remove.addEventListener('click', () => {
          const kept = mediaFor(target).filter((entry) => entry.token !== token);
          target.dataset.mediaIds = JSON.stringify(kept);
          tile.remove();
          updateSubmit();
        });
        const tagPeople = document.createElement('a');
        tagPeople.href = '#';
        tagPeople.setAttribute('role', 'link');
        tagPeople.textContent = 'Tag people';
        tagPeople.style.cssText = 'position:absolute;bottom:40px;left:2px;z-index:3;'
          + 'font-size:9px;color:#fff;background:rgba(0,0,0,.5);padding:1px 2px';
        tagPeople.addEventListener('click', (event) => event.preventDefault());
        const addDescription = document.createElement('a');
        addDescription.href = '#';
        addDescription.setAttribute('role', 'link');
        addDescription.setAttribute('aria-label', 'Add description');
        addDescription.setAttribute('data-testid', 'altTextWrapper');
        addDescription.textContent = 'Add description';
        addDescription.style.cssText = 'position:absolute;bottom:24px;left:2px;z-index:3;'
          + 'font-size:9px;color:#fff;background:rgba(0,0,0,.5);padding:1px 2px';
        addDescription.addEventListener('click', (event) => {
          event.preventDefault();
          openAltDescription(target, tile, token);
        });
        const progress = document.createElement('div');
        progress.setAttribute('role', 'progressbar');
        progress.setAttribute('aria-label', 'Uploading media');
        progress.textContent = '…';
        progress.style.cssText = 'position:absolute;bottom:2px;right:2px;font-size:10px';
        edit.style.zIndex = '2';
        remove.style.zIndex = '2';
        wrapper.style.zIndex = '1';
        group.appendChild(edit);
        group.appendChild(remove);
        wrapper.appendChild(image);
        group.appendChild(wrapper);
        group.appendChild(tagPeople);
        group.appendChild(addDescription);
        tile.appendChild(group);
        tile.appendChild(progress);
        preview.appendChild(tile);
        await uploadFile(file, target, token, tile);
      } else {
        await uploadFile(file, target, token, null);
      }
    }
    fileInput.value = '';
  }

  function challengeDialog() {
    const overlay = document.createElement('div');
    overlay.setAttribute('role', 'dialog');
    overlay.className = 'fx-dialog';
    overlay.innerHTML =
      '<h2>Verify you are a human</h2>' +
      '<p>Verifying you are a human\u2026</p>' +
      '<iframe title="challenge" src="https://x.com/i/arkose/challenge"></iframe>';
    document.body.appendChild(overlay);
  }

  function challengeIframe() {
    // No text in the top document: the only signal is the Arkose iframe,
    // exactly the challenge that body-text detection used to miss.
    const overlay = document.createElement('div');
    overlay.setAttribute('data-fx-challenge', 'iframe');
    const frame = document.createElement('iframe');
    frame.title = 'challenge';
    frame.src = 'https://x.com/i/arkose/challenge';
    overlay.appendChild(frame);
    document.body.appendChild(overlay);
  }

  function openMediaEditor() {
    // "Edit media" opens the image viewer, not the description editor.
    const overlay = document.createElement('div');
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-modal', 'true');
    overlay.className = 'fx-dialog';
    overlay.innerHTML =
      '<h2>Image description</h2>' +
      '<p>Edit image</p>' +
      '<div class="fx-actions"><button type="button">Back</button></div>';
    document.body.appendChild(overlay);
    overlay.querySelector('button').addEventListener('click', () => overlay.remove());
  }

  function openAltDescription(item, tile, token) {
    window.__fxAltAddClicks += 1;
    if (FX.scenario === 'alt_ui_missing') {
      // A deliberately broken client: no education sheet and no editor.
      return;
    }
    if (FX.scenario === 'alt_intro' && !window.__fxAltIntroShown) {
      window.__fxAltIntroShown = true;
      const sheet = document.createElement('div');
      sheet.setAttribute('data-testid', 'sheetDialog');
      sheet.setAttribute('role', 'dialog');
      sheet.className = 'fx-dialog';
      sheet.innerHTML =
        '<h2>Add descriptions</h2>' +
        '<p>You can add descriptions to your images.</p>' +
        '<div class="fx-actions">' +
        '<button type="button" data-fx-intro-sure>Sure</button>' +
        '<button type="button" data-fx-intro-no>No, thanks</button>' +
        '</div>';
      document.body.appendChild(sheet);
      const close = () => sheet.remove();
      sheet.querySelector('[data-fx-intro-sure]').addEventListener('click', () => {
        window.__fxAltIntro.sure += 1;
        close();
        showAltEditor(item, tile, token);
      });
      sheet.querySelector('[data-fx-intro-no]').addEventListener('click', () => {
        window.__fxAltIntro.no += 1;
        close();
        showAltEditor(item, tile, token);
      });
      return;
    }
    showAltEditor(item, tile, token);
  }

  function showAltEditor(item, tile, token) {
    const editor = document.createElement('div');
    editor.setAttribute('role', 'dialog');
    editor.setAttribute('aria-modal', 'true');
    editor.className = 'fx-dialog';
    editor.innerHTML =
      '<button type="button" aria-label="Back" data-fx-alt-back>\u2190</button>' +
      '<h2>Image description</h2>' +
      '<div>Edit image description</div>' +
      '<textarea aria-label="Edit image description"></textarea>' +
      '<div class="fx-actions">' +
      '<button type="button" data-fx-alt-save>Save</button>' +
      '</div>';
    document.body.appendChild(editor);
    const area = editor.querySelector('textarea');
    area.value = altTexts[token] || tile.dataset.alt || '';
    editor.querySelector('[data-fx-alt-back]')
      .addEventListener('click', () => editor.remove());
    editor.querySelector('[data-fx-alt-save]').addEventListener('click', () => {
      const value = area.value;
      altTexts[token] = value;
      tile.dataset.alt = value;
      const ids = mediaFor(item);
      const existing = ids.find((candidate) => candidate.token === token);
      if (existing) {
        existing.alt_text = value;
        item.dataset.mediaIds = JSON.stringify(ids);
      }
      editor.remove();
    });
  }

  async function send() {
    const all = items().map((item) => ({
      text: fxComposerText(item),
      media: mediaFor(item).map((entry) => {
        const media = { media_id: entry.media_id };
        if (entry.alt_text) {
          media.alt_text = entry.alt_text;
        }
        return media;
      }),
    }));
    if (all.length === 0 || all.some((entry) => !entry.text.trim())) {
      return;
    }
    if (FX.scenario === 'challenge') {
      challengeDialog();
      return;
    }
    if (FX.scenario === 'challenge_iframe') {
      challengeIframe();
      return;
    }
    if (submit) {
      submit.disabled = true;
    }
    let lastId = null;
    for (let index = 0; index < all.length; index += 1) {
      const variables = {
        tweet_text: all[index].text,
        reply: {
          exclude_reply_user_ids: [],
          in_reply_to_tweet_id: index === 0 ? (FX.reply_to || null) : lastId,
        },
        media: { media_entities: all[index].media, possibly_sensitive: false },
        batch_compose: index === 0 ? 'BatchFirst' : 'BatchSubsequent',
        semantic_annotation_ids: [],
      };
      let response;
      try {
        response = await fetch(
          'https://api.x.com/graphql/8TlLvl0sHLceb7rlGZZ6bg/CreateTweet',
          {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify({
              queryId: '8TlLvl0sHLceb7rlGZZ6bg',
              variables,
            }),
          },
        );
      } catch (err) {
        fxToast('Something went wrong. Try again.', true);
        updateSubmit();
        return;
      }
      if (!response.ok) {
        fxToast('Something went wrong. Try again.', true);
        updateSubmit();
        return;
      }
      const payload = await response.json();
      if (payload.errors && payload.errors.length) {
        fxToast(payload.errors[0].message || 'Something went wrong.', true);
        updateSubmit();
        return;
      }
      const data = payload.data || {};
      const results = (data.create_tweet || data.notetweet_create || {})
        .tweet_results || {};
      const result = results.result || {};
      const id = results.rest_id || result.rest_id || (result.tweet || {}).rest_id;
      if (!id) {
        fxToast('Tweet created but no ID returned', true);
        updateSubmit();
        return;
      }
      lastId = id;
    }
    fxCloseDialog(dialog);
    const href = '/' + FX.handle + '/status/' + lastId;
    const message = all.length > 1 ? 'Your posts were sent.' : 'Your post was sent.';
    fxToast(message, false, href);
  }

  if (itemsRoot) {
    items().forEach(wireItem);
  }
  if (addButton) {
    addButton.addEventListener('click', addItem);
  }
  if (addMedia && fileInput) {
    addMedia.addEventListener('click', () => fileInput.click());
  }
  if (fileInput) {
    fileInput.addEventListener('change', handleFiles);
  }
  if (closeButton) {
    closeButton.addEventListener('click', () => {
      const dirty = items().some((item) => fxComposerText(item).trim() !== '');
      if (dirty) {
        showSaveDialog();
      } else {
        fxCloseDialog(dialog);
      }
    });
  }
  if (form) {
    form.addEventListener('submit', (event) => {
      event.preventDefault();
      if (!submit || submit.disabled) {
        return;
      }
      send();
    });
  }
  updateSubmit();
}
"""

_COMPOSER_JS = r"""
fxWireComposer(document.querySelector('[data-fx-composer]'));
(function () {
  const wrapper = document.querySelector('[data-fx-modal-wrapper]');
  if (!wrapper) {
    return;
  }
  // The live modal layer is torn down together with the composer. Removing it
  // here keeps the dimming mask from lingering after a close/discard and makes
  // "is the composer closed?" checks truthful.
  const observer = new MutationObserver(() => {
    if (!document.querySelector('[data-fx-composer]')) {
      observer.disconnect();
      wrapper.remove();
    }
  });
  observer.observe(document.body, { childList: true, subtree: true });
})();
"""

_STATUS_JS = r"""
(function () {
  const article = document.querySelector('[data-fx-tweet]');
  if (!article) {
    return;
  }
  const tweetId = article.getAttribute('data-fx-tweet-id');
  const more = article.querySelector('[data-fx-more]');
  const menu = article.querySelector('[data-fx-menu]');
  const reply = article.querySelector('[data-fx-reply]');
  const template = document.querySelector('#fx-reply-template');
  const slot = document.querySelector('#fx-reply-slot');

  if (more && menu) {
    more.addEventListener('click', () => { menu.hidden = !menu.hidden; });
  }
  if (menu) {
    const remove = menu.querySelector('[data-fx-delete]');
    if (remove) {
      remove.addEventListener('click', () => {
        menu.hidden = true;
        const confirm = document.createElement('div');
        confirm.setAttribute('role', 'dialog');
        confirm.className = 'fx-dialog';
        confirm.innerHTML =
          '<h2>Delete post?</h2>' +
          '<p>This can\'t be undone and it will be removed from your profile, ' +
          'the timeline of any accounts that follow you, and from search results.</p>' +
          '<div class="fx-actions">' +
          '<button type="button" data-fx-cancel>Cancel</button>' +
          '<button type="button" data-fx-confirm>Delete</button>' +
          '</div>';
        document.body.appendChild(confirm);
        if (FX.dom === 'legacy') {
          confirm.querySelector('[data-fx-confirm]')
            .setAttribute('data-testid', 'confirmationSheetConfirm');
        }
        confirm.querySelector('[data-fx-cancel]').addEventListener('click', () => {
          confirm.remove();
        });
        confirm.querySelector('[data-fx-confirm]').addEventListener('click', async () => {
          try {
            await fetch(
              'https://api.x.com/graphql/8TlLvl0sHLceb7rlGZZ6bg/DeleteTweet',
              {
                method: 'POST',
                headers: { 'content-type': 'application/json' },
                body: JSON.stringify({
                  queryId: '8TlLvl0sHLceb7rlGZZ6bg',
                  variables: { tweet_id: tweetId, dark_request: false },
                }),
              },
            );
          } catch (err) {
            fxToast('Something went wrong. Try again.', true);
            confirm.remove();
            return;
          }
          confirm.remove();
          article.remove();
          fxToast('Your post was deleted.', false);
        });
      });
    }
  }
  if (reply && template && slot) {
    reply.addEventListener('click', () => {
      slot.innerHTML = '';
      slot.appendChild(template.content.cloneNode(true));
      fxWireComposer(slot.querySelector('[data-fx-composer]'));
    });
  }
})();
"""


def weighted_length(text: str) -> int:
    """Return the fake's simplified weighted length.

    WHY this exists in Python too: tests and the driver need to predict whether
    the composer's Post button is enabled. The inline JS implements the identical
    rule: every ``http(s)://`` token weighs 23, Latin characters weigh 1, and all
    other code points weigh 2. It is intentionally not X's real (script-aware)
    weighting; it only needs to be deterministic and shared.
    """
    import re

    url_pattern = re.compile(r"https?://\S+")
    urls = url_pattern.findall(text)
    rest = url_pattern.sub("", text)
    return 23 * len(urls) + sum(1 if ord(char) <= 0x24F else 2 for char in rest)


def _e(value: object) -> str:
    """HTML-escape a value; centralised so no template forgets it."""
    return html.escape(str(value), quote=True)


def _page(
    title: str,
    body: str,
    config: dict[str, Any],
    *,
    script: str = "",
) -> str:
    """Wrap a page body with the shared styles, config and JS helpers."""
    payload = json.dumps(config, separators=(",", ":")).replace("</", "<\\/")
    parts = [
        '<!doctype html><html lang="en"><head><meta charset="utf-8">',
        f'<meta name="csrf-token" content="{ARTIFACT_CANARY}">',
        f"<title>{_e(title)}</title>",
        f"<style>{_STYLE}</style></head><body>",
        f'<input type="hidden" name="authenticity_token" value="{ARTIFACT_CANARY}">',
        body,
        f"<script>window.__FAKE_X__ = {payload};</script>",
        f"<script>{_COMMON_JS}</script>",
    ]
    if script:
        parts.append(f"<script>{script}</script>")
    parts.append("</body></html>")
    return "".join(parts)


def _nav(cfg: dict[str, Any]) -> str:
    """Render the signed-in shell navigation for the requested client flavour."""
    handle = _e(cfg["handle"])
    if cfg["dom"] == "legacy":
        return (
            '<nav class="fx-nav">'
            '<a href="/home">Home</a>'
            '<a data-testid="SideNav_NewTweet_Button" href="/compose/post">Post</a>'
            f'<a data-testid="AppTabBar_Profile_Link" href="/{handle}">Profile</a>'
            '<button type="button" data-testid="SideNav_AccountSwitcher_Button">'
            "Account</button>"
            "</nav>"
        )
    return (
        '<nav class="fx-nav">'
        '<a href="/home">Home</a>'
        '<a href="/compose/post">Post</a>'
        f'<a href="/{handle}">@{handle}</a>'
        '<button type="button" aria-label="Account menu">Account</button>'
        "</nav>"
    )


def _main(cfg: dict[str, Any], inner: str) -> str:
    """Wrap content in ``main`` (legacy also tags it ``primaryColumn``)."""
    if cfg["dom"] == "legacy":
        return f'<main data-testid="primaryColumn">{inner}</main>'
    return f"<main>{inner}</main>"


def home(cfg: dict[str, Any]) -> str:
    """The signed-in home shell: nav plus an empty timeline."""
    handle = _e(cfg["handle"])
    body = _nav(cfg) + _main(
        cfg,
        f"<h1>Home</h1><p>Signed in as @{handle}.</p>",
    )
    return _page("Home / X", body, cfg)


def _item_markup(cfg: dict[str, Any], *, index: int, reply: bool) -> str:
    """One composer text entry plus its preview area (media tooling is shared)."""
    label = "Post your reply" if reply else "Post text"
    placeholder = "Post your reply" if reply else "What is happening?!"
    if cfg["dom"] == "legacy":
        input_markup = (
            f'<div data-fx-input contenteditable="true" role="textbox" '
            f'aria-multiline="true" data-testid="tweetTextarea_{index}" '
            f'aria-label="{label}">'
            '<div data-contents="true"><div data-block="true">'
            '<div><span><br data-text="true"></span></div></div></div>'
            "</div>"
        )
    else:
        aria = f' aria-label="{label}"' if reply else ""
        input_markup = (
            f'<textarea data-fx-input{aria} aria-multiline="true" '
            f'placeholder="{placeholder}"></textarea>'
        )
    return (
        f'<div class="fx-item">{input_markup}'
        '<div data-testid="attachments" class="fx-previews"></div></div>'
    )


def composer_markup(cfg: dict[str, Any], *, mode: str = "compose") -> str:
    """Build the modal composer (also reused inside the reply template).

    The media input is a single hidden ``input[type=file]`` for the whole
    dialog, matching the observed client and keeping ``[role=dialog]
    input[type=file]`` a unique locator. Files attach to the item that last had
    focus.
    """
    reply = mode == "reply"
    heading = "Reply" if reply else "Create post"
    title = "Post your reply" if reply else "Create post"
    button_label = "Reply" if reply else "Post"
    items = _item_markup(cfg, index=0, reply=reply)
    if reply or cfg.get("ui_drift"):
        add_markup = ""
    elif cfg["dom"] == "legacy":
        add_markup = (
            '<button type="button" data-fx-add data-testid="addButton" hidden>'
            "Add another post</button>"
        )
    else:
        add_markup = (
            '<button type="button" data-fx-add '
            'aria-label="Add another post">Add another post</button>'
        )
    if cfg["dom"] == "legacy":
        submit_testid = "tweetButtonInline" if reply else "tweetButton"
        submit_markup = (
            f'<button type="submit" data-fx-submit '
            f'data-testid="{submit_testid}" disabled>{button_label}</button>'
        )
        file_testid = ' data-testid="fileInput"'
    else:
        submit_markup = f'<button type="submit" data-fx-submit disabled>{button_label}</button>'
        file_testid = ""
    close_testid = ' data-testid="app-bar-close"' if cfg["dom"] == "legacy" and not reply else ""
    inner = (
        f"<h2>{heading}</h2>"
        f'<img alt="Display Name" src="{_AVATAR_SRC}" '
        'style="width:40px;height:40px;border-radius:50%">'
        f'<button type="button" data-fx-close{close_testid} aria-label="Close">\u2715</button>'
        "<form data-fx-form>"
        f"<div data-fx-items>{items}</div>"
        '<div class="fx-tools" data-testid="toolBar">'
        '<button type="button" data-fx-add-media aria-label="Add media">'
        "Add media</button>"
        f'<input data-fx-file{file_testid} class="sr-only" type="file" '
        f'multiple aria-hidden="true" tabindex="-1" accept="{_ACCEPT}">'
        '<span class="fx-weight" data-fx-weight>0</span>'
        "</div>"
        f"{add_markup}"
        '<div class="fx-actions">'
        f"{submit_markup}"
        "</div>"
        "</form>"
    )
    if cfg["dom"] == "legacy" and not reply:
        # Live classic structure: an outer role=dialog wrapper (kept zero-size
        # so Playwright does not treat it as visible) containing the dimming
        # mask and the real role=dialog[aria-modal=true] composer.
        return (
            '<div role="dialog" data-fx-modal-wrapper style="position:fixed;top:0;'
            'left:0;width:0;height:0;overflow:visible;z-index:20">'
            '<div data-testid="mask" style="position:fixed;inset:0;'
            'background:rgba(180,180,182,0.5);z-index:30"></div>'
            f'<div role="dialog" aria-modal="true" aria-label="{title}" data-fx-composer '
            'style="position:fixed;top:5vh;left:50%;transform:translateX(-50%);'
            "width:min(600px,90vw);max-height:90vh;overflow:auto;background:#fff;"
            'border:1px solid #cfd9de;border-radius:16px;padding:16px;z-index:31">'
            f"{inner}</div></div>"
        )
    return (
        f'<div role="dialog" aria-modal="true" aria-label="{title}" '
        f"data-fx-composer><div data-modal-slot-surface>{inner}</div></div>"
    )


def _legacy_inline_composer() -> str:
    """The home-timeline inline composer that sits under the classic modal.

    It deliberately shares ``tweetTextarea_0``, ``toolBar``, ``fileInput`` and
    ``tweetButtonInline`` with the modal (as the live client does), but it is
    inert: no JS is wired and its Post button stays disabled. The modal's mask
    covers it so a click cannot reach it.
    """
    return (
        '<div data-fx-inline-composer style="border:1px solid #cfd9de;'
        'border-radius:16px;padding:12px;margin-bottom:24px">'
        '<div contenteditable="true" role="textbox" aria-multiline="true" '
        'aria-label="Post text" data-testid="tweetTextarea_0" '
        'style="min-height:60px;outline:none"></div>'
        '<div class="fx-tools" data-testid="toolBar">'
        f'<input data-testid="fileInput" class="sr-only" type="file" multiple '
        f'aria-hidden="true" tabindex="-1" accept="{_ACCEPT}">'
        '<button type="button" data-testid="tweetButtonInline" disabled>Post</button>'
        "</div></div>"
    )


def composer(cfg: dict[str, Any]) -> str:
    """The ``/compose/post`` modal route."""
    if cfg["dom"] == "legacy":
        background = _legacy_inline_composer() + '<div style="height:3000px"></div>'
        body = _nav(cfg) + _main(cfg, background) + composer_markup(cfg, mode="compose")
    else:
        body = _nav(cfg) + composer_markup(cfg, mode="compose")
    return _page("Compose post / X", body, cfg, script=_COMPOSER_JS)


def _profile_article(cfg: dict[str, Any], tweet: Any) -> str:
    """Render one tweet for the profile page."""
    handle = _e(cfg["handle"])
    text = _e(tweet.text)
    link = f"/{handle}/status/{_e(tweet.id)}"
    reply_note = ""
    if tweet.in_reply_to:
        reply_note = (
            f'<p class="fx-reply">Replying to '
            f'<a href="/{handle}/status/{_e(tweet.in_reply_to)}">this post</a></p>'
        )
    if cfg["dom"] == "legacy":
        return (
            '<article data-testid="tweet">'
            f'<a href="/{handle}">@{handle}</a> '
            f'<a href="{link}">permalink</a>'
            f'{reply_note}<p class="fx-text">{text}</p>'
            f'<a href="{link}">View post</a></article>'
        )
    return (
        '<article itemprop="SocialMediaPosting" itemscope '
        'itemtype="https://schema.org/SocialMediaPosting" class="group/post">'
        f'<a href="/{handle}">@{handle}</a> '
        f'<a href="{link}">permalink</a>'
        f'{reply_note}<p class="fx-text">{text}</p>'
        f'<a href="{link}">View post</a></article>'
    )


def profile(cfg: dict[str, Any], tweets: list[Any]) -> str:
    """The profile page: every visible tweet, newest as inserted (thread order)."""
    handle = _e(cfg["handle"])
    articles = "".join(_profile_article(cfg, tweet) for tweet in tweets)
    if not articles:
        articles = "<p>No posts yet.</p>"
    body = _nav(cfg) + _main(
        cfg,
        f"<h1>@{handle}</h1>{articles}",
    )
    return _page(f"@{handle} / X", body, cfg)


def _status_article(cfg: dict[str, Any], tweet: Any) -> str:
    """The focal tweet with reply, more/delete and an inline reply composer slot."""
    handle = _e(cfg["handle"])
    text = _e(tweet.text)
    if cfg["dom"] == "legacy":
        opening = f'<article data-fx-tweet data-fx-tweet-id="{_e(tweet.id)}" data-testid="tweet">'
        reply_button = (
            '<button type="button" data-fx-reply data-testid="reply" '
            'aria-label="Reply">Reply</button>'
        )
        more_button = (
            '<button type="button" data-fx-more data-testid="caret" aria-label="More">More</button>'
        )
    else:
        opening = (
            "<article data-fx-tweet "
            f'data-fx-tweet-id="{_e(tweet.id)}" '
            'itemprop="SocialMediaPosting" itemscope '
            'itemtype="https://schema.org/SocialMediaPosting" class="group/post">'
        )
        reply_button = '<button type="button" data-fx-reply aria-label="Reply">Reply</button>'
        more_button = '<button type="button" data-fx-more aria-label="More">More</button>'
    menu = (
        '<div role="menu" data-fx-menu hidden>'
        '<button type="button" role="menuitem" data-fx-delete>Delete</button>'
        "</div>"
    )
    return (
        f'{opening}<a href="/{handle}">@{handle}</a>'
        f'<p class="fx-text">{text}</p>'
        f"{reply_button}{more_button}{menu}</article>"
    )


def status(cfg: dict[str, Any], tweet: Any) -> str:
    """A permalink page with a live reply composer and a delete flow."""
    reply_cfg = {**cfg, "mode": "reply", "reply_to": tweet.id}
    template = (
        f'<template id="fx-reply-template">{composer_markup(reply_cfg, mode="reply")}</template>'
    )
    body = _nav(reply_cfg) + _main(
        reply_cfg,
        _status_article(reply_cfg, tweet) + '<div id="fx-reply-slot"></div>' + template,
    )
    return _page("Post / X", body, reply_cfg, script=_STATUS_JS)


def login(cfg: dict[str, Any]) -> str:
    """The logged-out login landing page from ``selectors.json``."""
    body = _main(
        cfg,
        "<h1>Sign in to X</h1>"
        '<a href="/i/jf/onboarding/web?mode=login&amp;redirect_after_login=%2F">'
        "Log in with username or email</a>"
        '<form><label>Username <input autocomplete="username" name="text" '
        'type="text"></label><label>Password '
        '<input autocomplete="current-password" name="password" '
        'type="password"></label>'
        '<button type="submit">Log in</button></form>'
        '<a href="/i/jf/onboarding/web?mode=signup">Sign up</a>',
    )
    return _page("Sign in to X", body, cfg)


def locked(cfg: dict[str, Any]) -> str:
    """The bounce page for an account that needs manual unlocking."""
    body = _main(
        cfg,
        "<h1>Your account has been locked</h1><p>Verify your identity to unlock your account.</p>",
    )
    return _page("Account locked / X", body, cfg)


def suspended(cfg: dict[str, Any]) -> str:
    """The bounce page for a suspended account."""
    body = _main(
        cfg,
        "<h1>Sorry, this account has been suspended.</h1>",
    )
    return _page("Account suspended / X", body, cfg)


def logout(cfg: dict[str, Any]) -> str:
    """A minimal logout confirmation page."""
    body = _main(cfg, "<h1>You are logged out.</h1>")
    return _page("Logged out / X", body, cfg)


def redirect(cfg: dict[str, Any], target: str) -> str:
    """Emulate a 302 to ``target`` with a script navigation.

    WHY not a real 3xx: ``Route.fulfill`` hands the status straight to Chromium,
    which does not follow a fulfilled redirect and shows its network error page
    instead. A same-origin ``location.replace`` produces the same observable
    outcome (URL change plus a second document visit) entirely offline.
    """
    script = f"location.replace({json.dumps(target)});"
    body = "<main><p>Redirecting\u2026</p></main>"
    return _page("Redirecting / X", body, cfg, script=script)


def not_found(cfg: dict[str, Any]) -> str:
    """The standard missing-tweet page (unknown or deleted ids land here)."""
    body = _main(
        cfg,
        "<h1>Hmm...this page doesn't exist</h1><p>Try searching for something else.</p>",
    )
    return _page("Page not found / X", body, cfg)


def challenge_frame(cfg: dict[str, Any]) -> str:
    """Placeholder inside the Arkose iframe; keeps every request on x.com."""
    body = (
        "<main><h1>Arkose challenge placeholder</h1>"
        "<p>The fake never loads third-party challenge scripts.</p></main>"
    )
    return _page("Challenge / X", body, cfg)
