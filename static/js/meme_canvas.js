// ── In-app meme generator: Fabric.js canvas editor ──────────────────────────

let fabricCanvas = null;
let canvasEditPostId = null;
let cropTargetImage = null;
let cropRect = null;

function getFabricCanvas() {
  if (!fabricCanvas) {
    fabricCanvas = new fabric.Canvas('meme-fabric-canvas', { backgroundColor: '#ffffff' });
    fabricCanvas.on('selection:created', syncObjectPanelToSelection);
    fabricCanvas.on('selection:updated', syncObjectPanelToSelection);
    fabricCanvas.on('object:rotating', syncObjectPanelToSelection);
  }
  return fabricCanvas;
}

// Keeps the side-panel controls (fill, stroke, rotation, font) reflecting
// whichever object is currently selected, since values can also change via
// direct canvas interaction (dragging the rotate handle, etc), not just
// through these inputs.
function syncObjectPanelToSelection() {
  const canvas = getFabricCanvas();
  const obj = canvas.getActiveObject();
  if (!obj) return;
  document.getElementById('canvas-obj-rotation').value = Math.round(obj.angle || 0);
  document.getElementById('canvas-obj-fill').value = normalizeColorForInput(obj.fill) || '#ffffff';
  document.getElementById('canvas-text-stroke').value = normalizeColorForInput(obj.stroke) || '#000000';
  document.getElementById('canvas-text-stroke-width').value = obj.strokeWidth || 0;

  const isText = obj.type === 'i-text';
  document.getElementById('canvas-text-only-controls').style.display = isText ? 'flex' : 'none';
  if (isText) {
    document.getElementById('canvas-text-size').value = obj.fontSize || 32;
    document.getElementById('canvas-text-font').value = obj.fontFamily || 'Impact';
  }
}

function normalizeColorForInput(color) {
  if (!color || typeof color !== 'string' || !color.startsWith('#')) return null;
  return color.length === 4
    ? '#' + color.slice(1).split('').map(c => c + c).join('')
    : color;
}

function openMemeCanvas(existingPost) {
  if (!requireMemeManager()) return;

  // Fabric.js measures its container's size at construction time, so the
  // modal must be visible (not display:none) before the canvas is first
  // created — otherwise Fabric locks in a 0x0 size and nothing ever paints.
  openModal('meme-canvas-modal');

  const canvas = getFabricCanvas();
  canvas.setDimensions({ width: 640, height: 480 });
  canvas.calcOffset();
  canvas.clear();
  canvas.backgroundColor = '#ffffff';
  cropTargetImage = null;
  cropRect = null;
  document.getElementById('canvas-apply-crop-btn').style.display = 'none';
  document.getElementById('canvas-error').style.display = 'none';

  if (existingPost && existingPost.post_type === 'image') {
    canvasEditPostId = existingPost.id;
    document.getElementById('canvas-caption-input').value = existingPost.caption || '';
    fabric.Image.fromURL(`/static/${existingPost.image_path}`, (img) => {
      const scale = Math.min(canvas.width / img.width, canvas.height / img.height, 1);
      img.set({ left: 0, top: 0, scaleX: scale, scaleY: scale, selectable: true });
      canvas.add(img);
      canvas.renderAll();
    }, { crossOrigin: 'anonymous' });
  } else {
    canvasEditPostId = null;
    document.getElementById('canvas-caption-input').value = '';
  }

  canvas.renderAll();
}

function canvasAddImage(event) {
  const file = event.target.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = (e) => {
    fabric.Image.fromURL(e.target.result, (img) => {
      const canvas = getFabricCanvas();
      const scale = Math.min(canvas.width / img.width, canvas.height / img.height, 1) * 0.8;
      img.set({ left: 20, top: 20, scaleX: scale, scaleY: scale });
      canvas.add(img);
      canvas.setActiveObject(img);
      canvas.renderAll();
    });
  };
  reader.readAsDataURL(file);
  event.target.value = '';
}

async function canvasAddImageFromUrl() {
  const input = document.getElementById('canvas-image-url-input');
  const errEl = document.getElementById('canvas-url-error');
  errEl.style.display = 'none';
  const url = input.value.trim();
  if (!url) return;

  const res = await fetch('/memes/canvas/fetch-image', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url })
  });
  const data = await res.json();
  if (!res.ok) {
    errEl.textContent = data.error || 'Could not add that image.';
    errEl.style.display = 'block';
    return;
  }

  fabric.Image.fromURL(`/static/${data.path}`, (img) => {
    const canvas = getFabricCanvas();
    const scale = Math.min(canvas.width / img.width, canvas.height / img.height, 1) * 0.8;
    img.set({ left: 20, top: 20, scaleX: scale, scaleY: scale });
    canvas.add(img);
    canvas.setActiveObject(img);
    canvas.renderAll();
    syncObjectPanelToSelection();
  });
  input.value = '';
}

function canvasAddText() {
  const canvas = getFabricCanvas();
  const text = new fabric.IText('Tap to edit', {
    left: 60, top: 60,
    fontFamily: document.getElementById('canvas-text-font').value,
    fontSize: parseInt(document.getElementById('canvas-text-size').value) || 32,
    fill: document.getElementById('canvas-obj-fill').value,
    stroke: document.getElementById('canvas-text-stroke').value,
    strokeWidth: parseInt(document.getElementById('canvas-text-stroke-width').value) || 0,
  });
  canvas.add(text);
  canvas.setActiveObject(text);
  canvas.renderAll();
  syncObjectPanelToSelection();
}

function canvasAddShape(kind) {
  const canvas = getFabricCanvas();
  const fill = document.getElementById('canvas-obj-fill').value;
  const stroke = document.getElementById('canvas-text-stroke').value;
  const strokeWidth = parseInt(document.getElementById('canvas-text-stroke-width').value) || 0;
  const common = { left: 80, top: 80, fill, stroke, strokeWidth };
  const shape = kind === 'circle'
    ? new fabric.Circle({ ...common, radius: 80 })
    : new fabric.Rect({ ...common, width: 200, height: 140 });
  canvas.add(shape);
  canvas.setActiveObject(shape);
  canvas.renderAll();
  syncObjectPanelToSelection();
}

// Font/size only apply to text objects.
function canvasUpdateTextStyle() {
  const canvas = getFabricCanvas();
  const obj = canvas.getActiveObject();
  if (!obj || obj.type !== 'i-text') return;
  obj.set({
    fontSize: parseInt(document.getElementById('canvas-text-size').value) || 32,
    fontFamily: document.getElementById('canvas-text-font').value,
  });
  canvas.renderAll();
}

// Fill applies to any object — text color and shape fill are the same
// underlying Fabric property, so one control covers both.
function canvasUpdateObjectFill() {
  const canvas = getFabricCanvas();
  const obj = canvas.getActiveObject();
  if (!obj) return;
  obj.set({ fill: document.getElementById('canvas-obj-fill').value });
  canvas.renderAll();
}

function canvasUpdateObjectStroke() {
  const canvas = getFabricCanvas();
  const obj = canvas.getActiveObject();
  if (!obj) return;
  obj.set({
    stroke: document.getElementById('canvas-text-stroke').value,
    strokeWidth: parseInt(document.getElementById('canvas-text-stroke-width').value) || 0,
  });
  canvas.renderAll();
}

function canvasUpdateObjectTransform() {
  const canvas = getFabricCanvas();
  const obj = canvas.getActiveObject();
  if (!obj) return;
  const deg = parseFloat(document.getElementById('canvas-obj-rotation').value) || 0;
  obj.rotate(((deg % 360) + 360) % 360);
  canvas.renderAll();
}

function canvasBringToFront() {
  const canvas = getFabricCanvas();
  const obj = canvas.getActiveObject();
  if (obj) { canvas.bringToFront(obj); canvas.renderAll(); }
}
function canvasSendToBack() {
  const canvas = getFabricCanvas();
  const obj = canvas.getActiveObject();
  if (obj) { canvas.sendToBack(obj); canvas.renderAll(); }
}
function canvasDeleteSelected() {
  const canvas = getFabricCanvas();
  const obj = canvas.getActiveObject();
  if (obj) { canvas.remove(obj); canvas.renderAll(); }
}

// ── Cropping ──────────────────────────────────────────────────────────────
function canvasStartCrop() {
  const canvas = getFabricCanvas();
  const obj = canvas.getActiveObject();
  if (!obj || obj.type !== 'image') {
    alert('Select an image on the canvas first.');
    return;
  }
  if (obj.angle) {
    alert('Straighten the image (angle 0) before cropping.');
    return;
  }
  cropTargetImage = obj;
  cropRect = new fabric.Rect({
    left: obj.left, top: obj.top,
    width: obj.getScaledWidth(), height: obj.getScaledHeight(),
    fill: 'rgba(233,0,82,0.15)', stroke: '#E90052', strokeDashArray: [6, 4],
    strokeWidth: 2,
  });
  canvas.add(cropRect);
  canvas.setActiveObject(cropRect);
  canvas.renderAll();
  document.getElementById('canvas-apply-crop-btn').style.display = 'inline-block';
}

function canvasApplyCrop() {
  const canvas = getFabricCanvas();
  if (!cropTargetImage || !cropRect) return;
  const img = cropTargetImage;

  const relLeft = (cropRect.left - img.left) / img.getScaledWidth();
  const relTop = (cropRect.top - img.top) / img.getScaledHeight();
  const relWidth = cropRect.getScaledWidth() / img.getScaledWidth();
  const relHeight = cropRect.getScaledHeight() / img.getScaledHeight();

  const existingCropX = img.cropX || 0;
  const existingCropY = img.cropY || 0;
  const newCropX = existingCropX + relLeft * img.width;
  const newCropY = existingCropY + relTop * img.height;
  const newWidth = Math.max(1, relWidth * img.width);
  const newHeight = Math.max(1, relHeight * img.height);

  const newScaleX = cropRect.getScaledWidth() / newWidth;
  const newScaleY = cropRect.getScaledHeight() / newHeight;

  img.set({
    cropX: newCropX, cropY: newCropY,
    width: newWidth, height: newHeight,
    left: cropRect.left, top: cropRect.top,
    scaleX: newScaleX, scaleY: newScaleY,
  });

  canvas.remove(cropRect);
  cropRect = null;
  cropTargetImage = null;
  canvas.setActiveObject(img);
  canvas.renderAll();
  document.getElementById('canvas-apply-crop-btn').style.display = 'none';
}

// ── Save ──────────────────────────────────────────────────────────────────
async function canvasSave() {
  const managerId = requireMemeManager();
  if (!managerId) return;
  const canvas = getFabricCanvas();
  const errEl = document.getElementById('canvas-error');
  errEl.style.display = 'none';

  if (canvas.getObjects().length === 0) {
    errEl.textContent = 'Add at least one image or text element first.';
    errEl.style.display = 'block';
    return;
  }

  canvas.discardActiveObject();
  canvas.renderAll();
  const dataUrl = canvas.toDataURL({ format: 'png' });

  const blob = await (await fetch(dataUrl)).blob();
  const file = new File([blob], `meme-${Date.now()}.png`, { type: 'image/png' });

  const form = new FormData();
  form.append('manager_id', managerId);
  form.append('caption', document.getElementById('canvas-caption-input').value.trim());
  form.append('image', file);

  const url = canvasEditPostId ? `/memes/${canvasEditPostId}/edit` : '/memes/new';
  const res = await fetch(url, { method: 'POST', body: form });
  const data = await res.json();
  if (!res.ok) { errEl.textContent = data.error || 'Save failed'; errEl.style.display = 'block'; return; }
  closeModal('meme-canvas-modal');
  window.location.reload();
}
