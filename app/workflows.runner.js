// 工作流面板：本地部署模式下触发区间抓取，并展示运行进度

window.DPRWorkflowRunner = (function () {
  // Date range fetch replaces the old quick-fetch presets

  let overlay = null;
  let panel = null;
  let statusEl = null;
  let runsEl = null;

  let _localPollTimer = null;

  const escapeHtml = (str) => {
    if (!str) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39');
  };

  const setStatus = (text, color, options = {}) => {
    if (!statusEl) return;
    statusEl.textContent = text || '';
    statusEl.style.color = color || '#666';
    statusEl.classList.toggle('is-waiting', !!(options && options.waiting));
  };

  const ensureOverlay = () => {
    if (overlay && panel) return;
    overlay = document.getElementById('dpr-workflow-overlay');
    if (overlay) {
      panel = document.getElementById('dpr-workflow-panel');
      statusEl = document.getElementById('dpr-workflow-status');
      runsEl = document.getElementById('dpr-workflow-runs');
      return;
    }

    overlay = document.createElement('div');
    overlay.id = 'dpr-workflow-overlay';
    overlay.innerHTML = `
      <div id="dpr-workflow-panel">
        <div id="dpr-workflow-header">
          <div style="font-weight:600;">本地工作流</div>
          <div style="display:flex; gap:8px; align-items:center;">
            <button id="dpr-workflow-close-btn" class="arxiv-tool-btn" style="padding:2px 6px;">关闭</button>
          </div>
        </div>
        <div id="dpr-workflow-body">
          <div id="dpr-workflow-status" style="font-size:12px; color:#666; margin-bottom:10px;">准备就绪。</div>
          <div style="font-size:12px; color:#666; margin-bottom:10px;">本地部署模式，工作流由 <code>python src/main.py</code> 直接运行，不经过 GitHub Actions。</div>
          <div style="font-weight:600; font-size:13px; margin-bottom:6px;">执行过程</div>
          <div id="dpr-workflow-runs" style="font-size:12px; color:#333; border:1px solid #eee; border-radius:8px; background:#fff; padding:10px; min-height:120px;">
            <div style="color:#999;">尚未触发工作流。</div>
          </div>
        </div>
      </div>
    `;
    document.body.appendChild(overlay);

    panel = document.getElementById('dpr-workflow-panel');
    statusEl = document.getElementById('dpr-workflow-status');
    runsEl = document.getElementById('dpr-workflow-runs');

    const closeBtn = document.getElementById('dpr-workflow-close-btn');
    if (closeBtn) {
      closeBtn.addEventListener('click', close);
    }
    overlay.addEventListener('mousedown', (e) => {
      if (e.target === overlay) close();
    });
  };

  const open = () => {
    ensureOverlay();
    if (!overlay) return;
    overlay.style.display = 'flex';
    requestAnimationFrame(() => overlay.classList.add('show'));
    return true;
  };

  const _setStatus = (text, color, options) => {
    setStatus(text, color, options);
  };

  const _setRuns = (html) => {
    if (runsEl) runsEl.innerHTML = html;
  };

  const _escapeHtml = (str) => escapeHtml(str);

  const close = () => {
    if (!overlay) return;
    overlay.classList.remove('show');
    setTimeout(() => {
      overlay.style.display = 'none';
    }, 160);
    if (_localPollTimer) {
      clearInterval(_localPollTimer);
      _localPollTimer = null;
    }
  };

  const _isLocalHost = (() => {
    const h = String((window.location && window.location.hostname) || '').toLowerCase();
    return h === 'localhost' || h === '127.0.0.1' || h === '[::1]';
  })();

  const _localRangeFetch = async (startDate, endDate, skipExisting) => {
    const body = {
      start_date: String(startDate || '').trim(),
      end_date: String(endDate || '').trim(),
    };
    if (skipExisting) body.force_existing = true;

    const res = await fetch('/api/range-fetch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(data.error || `HTTP ${res.status}`);
    }
    return data;
  };

  const runRangeFetch = async (startDateOrDays, endDateOrOptions, optionsArg) => {
    let startDate, endDate, opts;
    if (endDateOrOptions && typeof endDateOrOptions === 'object' && !optionsArg) {
      const days = parseInt(startDateOrDays, 10) || 10;
      const end = new Date();
      const start = new Date(end);
      start.setDate(start.getDate() - days + 1);
      startDate = start.toISOString().slice(0, 10).replace(/-/g, '');
      endDate = end.toISOString().slice(0, 10).replace(/-/g, '');
      opts = endDateOrOptions;
    } else {
      startDate = String(startDateOrDays || '').trim();
      endDate = String(endDateOrOptions || '').trim();
      opts = optionsArg || {};
    }
    const skipExisting = !!opts.skipExisting;
    if (_isLocalHost) {
      try {
        const data = await _localRangeFetch(startDate, endDate, skipExisting);
        setStatus(data.message || `已启动区间抓取 (${startDate} ~ ${endDate})`, '#080');
        _startLocalStatusPolling();
        return data;
      } catch (e) {
        setStatus(`区间抓取启动失败：${e.message || e}`, '#c00');
        return null;
      }
    }
    setStatus('区间抓取仅支持本地模式。', '#c00');
    return null;
  };

  const _startLocalStatusPolling = () => {
    if (_localPollTimer) clearInterval(_localPollTimer);
    ensureOverlay();
    if (overlay) {
      overlay.style.display = 'flex';
      requestAnimationFrame(() => overlay.classList.add('show'));
    }
    const poll = async () => {
      try {
        const res = await fetch('/api/range-fetch/status');
        const data = await res.json().catch(() => ({}));
        const status = data.status || 'unknown';
        const logTail = data.log_tail || data.log || '';
        if (status === 'running') {
          setStatus('区间抓取运行中...', '#1565c0', { waiting: true });
          if (runsEl) {
            runsEl.innerHTML = `<div style="font-size:11px; white-space:pre-wrap; max-height:300px; overflow:auto; color:#333; font-family:monospace;">${escapeHtml(logTail.slice(-2000))}</div>`;
          }
          const chatLogWrap = document.getElementById('chat-range-log');
          const chatLogContent = document.getElementById('chat-range-log-content');
          if (chatLogWrap) chatLogWrap.style.display = 'block';
          if (chatLogContent) chatLogContent.textContent = logTail.slice(-4000);
        } else if (status === 'success') {
          setStatus('区间抓取完成', '#080');
          if (runsEl) {
            runsEl.innerHTML = `<div style="color:#080; margin-bottom:6px;">区间抓取成功完成</div><div style="font-size:11px; white-space:pre-wrap; max-height:300px; overflow:auto; color:#333; font-family:monospace;">${escapeHtml(logTail.slice(-2000))}</div>`;
          }
          const chatLogContent = document.getElementById('chat-range-log-content');
          if (chatLogContent) chatLogContent.textContent = logTail.slice(-4000);
          if (typeof window.__dprRefreshLastRun === 'function') window.__dprRefreshLastRun();
          clearInterval(_localPollTimer);
          _localPollTimer = null;
        } else if (status === 'failure') {
          setStatus(`区间抓取失败 (exit=${data.exit_code})`, '#c00');
          if (runsEl) {
            runsEl.innerHTML = `<div style="color:#c00; margin-bottom:6px;">区间抓取失败 (exit=${data.exit_code})</div><div style="font-size:11px; white-space:pre-wrap; max-height:300px; overflow:auto; color:#333; font-family:monospace;">${escapeHtml(logTail.slice(-2000))}</div>`;
          }
          const chatLogContent = document.getElementById('chat-range-log-content');
          if (chatLogContent) chatLogContent.textContent = logTail.slice(-4000);
          if (typeof window.__dprRefreshLastRun === 'function') window.__dprRefreshLastRun();
          clearInterval(_localPollTimer);
          _localPollTimer = null;
        }
      } catch (e) {
        // ignore poll errors
      }
    };
    poll();
    _localPollTimer = setInterval(poll, 3000);
  };

  return {
    open,
    runRangeFetch,
    _setStatus,
    _setRuns,
    _escapeHtml,
  };
})();
