'use strict';
const statusText = document.getElementById('status');
const dot = document.getElementById('dot');
const typing = document.getElementById('input');
const stopButton = document.getElementById('stop');
function setStatus(text, live) {
  statusText.textContent = text;
  dot.style.background = live ? '#76c89a' : '#dbb178';
}
async function post(path, data) {
  const response = await fetch(path, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(data),
  });
  if (!response.ok) throw new Error('Request rejected');
  return response;
}
async function start() {
  const token = new URLSearchParams(location.hash.slice(1)).get('token');
  // Fragments are not sent in HTTP requests. Clear it before opening links.
  history.replaceState(null, '', location.pathname);
  if (token) await post('/session', {token});
  const health = await fetch('/health');
  if (!health.ok) {
    setStatus('Open the private access link', false);
    typing.disabled = true;
    return;
  }
  const term = new Terminal({
    cursorBlink: true, fontSize: 14,
    fontFamily: '"SFMono-Regular", Menlo, Consolas, monospace', lineHeight: 1.2,
    scrollback: 10000,
    theme: {background:'#111418',foreground:'#dee3ea',cursor:'#b7e6cb',
      selectionBackground:'#364a5c',black:'#29313b',red:'#f07d81',green:'#96d6aa',
      yellow:'#e6c384',blue:'#8ab7f2',magenta:'#c6a1e5',cyan:'#8dd3d2',white:'#e6eaf0'},
  });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.loadAddon(new WebLinksAddon.WebLinksAddon());
  term.open(document.getElementById('terminal'));
  let queue = Promise.resolve();
  let stopped = false;
  term.onData(data => {
    if (typing.checked && !stopped) {
      queue = queue.then(() => post('/input', {data})).catch(() => setStatus('Input disconnected', false));
    }
  });
  let resizeFrame;
  function resize() {
    cancelAnimationFrame(resizeFrame);
    resizeFrame = requestAnimationFrame(() => {
      if (stopped) return;
      fit.fit();
      post('/resize', {cols:term.cols, rows:term.rows}).catch(() => {});
      document.getElementById('dimensions').textContent = `${term.cols} × ${term.rows} · local to this machine`;
    });
  }
  const observer = new ResizeObserver(resize);
  observer.observe(document.getElementById('terminal'));
  resize();
  const stream = new EventSource('/events');
  stream.onopen = () => setStatus('Live', true);
  stream.onerror = () => {if (!stopped) setStatus('Reconnecting', false);};
  stream.onmessage = event => {
    const message = JSON.parse(event.data);
    const bytes = Uint8Array.from(atob(message.data), char => char.charCodeAt(0));
    if (message.reset) term.reset();
    term.write(bytes);
  };
  stream.addEventListener('exit', () => {
    stopped = true;
    stream.close();
    typing.disabled = true;
    observer.disconnect();
    setStatus('Shell exited', false);
  });
  stopButton.disabled = false;
  stopButton.addEventListener('click', async () => {
    stopButton.disabled = true;
    try {
      await post('/stop', {});
      stopped = true;
      stream.close();
      observer.disconnect();
      typing.disabled = true;
      term.reset();
      setStatus('Session ended', false);
    } catch {
      stopButton.disabled = false;
      setStatus('Could not end session', false);
    }
  });
  term.focus();
}
start().catch(() => {
  typing.disabled = true;
  setStatus('Session unavailable — reopen its private link', false);
});
