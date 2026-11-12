/* 参考消费端；同源认证与授权归 W13，所有状态来自真实响应，不内置样例数据。 */
(function (root) {
  'use strict';
  const displayUnknown = value => value === null || value === undefined ? '未知' : String(value);
  const identifier = () => globalThis.crypto.randomUUID().replaceAll('-', '');
  class BusinessError extends Error {
    constructor(message, status, body) { super(message); this.status = status; this.body = body; }
  }
  const RECEIPT_FIELDS = ['request_id', 'operation_id', 'state', 'data', 'error', 'allowed_actions', 'warnings', 'effect_outcome'];
  const CONFIRMED_STATES = {200: 'completed', 202: 'accepted'};
  const isRecord = value => value !== null && typeof value === 'object' && !Array.isArray(value);
  // 冻结回执封套是闭合对象且没有 ok 字段；缺字段、多字段或类型不符都不是可采信的回执。
  function receiptEnvelope(body) {
    if (!isRecord(body) || Object.keys(body).length !== RECEIPT_FIELDS.length
        || !RECEIPT_FIELDS.every(key => Object.hasOwn(body, key))) return null;
    const valid = typeof body.request_id === 'string' && body.request_id !== ''
      && (body.operation_id === null || typeof body.operation_id === 'string')
      && ['completed', 'accepted', 'pending', 'blocked', 'failed', 'unknown'].includes(body.state) && isRecord(body.data)
      && (body.error === null || (isRecord(body.error) && typeof body.error.code === 'string'))
      && Array.isArray(body.allowed_actions) && Array.isArray(body.warnings)
      && ['none', 'not_started', 'committed', 'running', 'unknown'].includes(body.effect_outcome);
    return valid ? body : null;
  }
  class BusinessClient {
    constructor(fetcher = globalThis.fetch.bind(globalThis), csrf = () => '') {
      this.fetcher = fetcher; this.csrf = csrf; this.pending = new Map();
    }
    headers(json = true) {
      const token = this.csrf();
      return { ...(json ? {'Content-Type': 'application/json'} : {}), ...(token ? {'X-CSRF-Token': token} : {}) };
    }
    prepare(action, params, options = {}) {
      const expectedRev = options.expected_revision !== undefined ? options.expected_revision : options.expectedRevision;
      const signature = options.mutating ? JSON.stringify([action, params, expectedRev, options.idempotency_key]) : null;
      if (signature) {
        const existing = [...this.pending.values()].find(call => call.signature === signature);
        if (existing) return existing;
      }
      const payload = {action, params, request_id: identifier()};
      if (options.mutating) payload.idempotency_key = options.idempotency_key || identifier();
      if (expectedRev !== undefined) payload.expected_revision = expectedRev;
      if (options.authorization_ticket) payload.authorization_ticket = options.authorization_ticket;
      return Object.freeze({body: JSON.stringify(payload), request_id: payload.request_id, signature});
    }
    async send(prepared, signal) {
      // 未知传输结果保留同一准备对象供人工核对，不换 ID 自动重传。
      this.pending.set(prepared.request_id, prepared);
      let response;
      try {
        response = await this.fetcher('/client/business', {method: 'POST', credentials: 'same-origin',
          headers: this.headers(), body: prepared.body, signal});
      } catch (error) { throw new BusinessError('未确认；请核对原 request_id，勿新建重发', 0, {request_id: prepared.request_id}); }
      let body = null;
      try { body = await response.json(); } catch (error) { body = null; }
      const envelope = receiptEnvelope(body);
      // 只有 200/completed 或 202/accepted 且 error=null 才算成功；accepted 仅证明已受理，不等于已完成。
      if (envelope && envelope.error === null && CONFIRMED_STATES[response.status] === envelope.state) {
        this.pending.delete(prepared.request_id);
        return envelope;
      }
      if (!response.ok && (!envelope || ['none', 'not_started'].includes(envelope.effect_outcome)))
        throw new BusinessError('业务请求被拒绝或尚未连接', response.status, body);
      // 结果未知、已提交但不可见、未完成或回执不可采信：保留原准备对象，不换 ID 重发。
      throw new BusinessError('业务回执未确认；请核对原 request_id，勿新建重发', response.status, body);
    }
    call(action, params, options = {}) { return this.send(this.prepare(action, params, options), options.signal); }
    async uploadArtifact(file, spaceId, idempotencyKey, taskId) {
      const body = new FormData();
      body.append('file', file); body.append('space_id', spaceId); body.append('idempotency_key', idempotencyKey);
      if (taskId) body.append('task_id', taskId);
      const response = await this.fetcher('/client/business/artifacts', {method: 'POST', credentials: 'same-origin',
        headers: this.headers(false), body});
      if (!response.ok) throw new BusinessError('上传未确认；保留原幂等键核对', response.status);
      return response.json();
    }
    artifactURL(artifactId, versionRef) {
      return `/client/business/artifacts/${encodeURIComponent(artifactId)}/content?version_ref=${encodeURIComponent(versionRef)}`;
    }
    async readArtifact(artifactId, versionRef, range = '') {
      const headers = range ? {Range: range} : {};
      const response = await this.fetcher(this.artifactURL(artifactId, versionRef), {method: 'GET',
        credentials: 'same-origin', headers});
      if (!response.ok) throw new BusinessError('产物版本读取被拒绝', response.status);
      return response.blob();
    }
  }
  class NDJSONDecoder {
    constructor(onItem, maxLineChars = 1048576) {
      this.decoder = new TextDecoder('utf-8', {fatal: true}); this.buffer = ''; this.onItem = onItem;
      this.maxLineChars = maxLineChars;
    }
    feed(bytes) {
      this.buffer += this.decoder.decode(bytes, {stream: true});
      let index;
      while ((index = this.buffer.indexOf('\n')) >= 0) {
        if (index > this.maxLineChars) throw new BusinessError('观察行过长，覆盖未知', 0);
        const line = this.buffer.slice(0, index).trim(); this.buffer = this.buffer.slice(index + 1);
        if (line) this.onItem(JSON.parse(line));
      }
      if (this.buffer.length > this.maxLineChars) throw new BusinessError('观察缓存超限，覆盖未知', 0);
    }
    finish() {
      this.buffer += this.decoder.decode();
      if (this.buffer.trim()) throw new BusinessError('NDJSON 末行不完整，需要补拉', 0);
    }
  }
  class BusinessObservation {
    constructor(client, onUpdate, filter = {}) {
      this.client = client; this.onUpdate = onUpdate; this.filter = JSON.parse(JSON.stringify(filter));
      this.cursor = null; this.coverage = 'unknown'; this.seen = new Set(); this.maxCatchUpPages = 10;
    }
    async recover(expired = false) {
      if (expired) { this.cursor = null; this.coverage = 'expired'; this.seen.clear(); }
      else this.coverage = 'partial';
      // 持续事件源到当前末尾仍给非空续读游标，且游标串随过期时间变化：只靠有界页数收口；
      // 每页都保存服务端扫描续点（含过滤后为空的页），游标保持不透明，不据其内容做授权判断。
      for (let page = 0; ; page += 1) {
        if (page >= this.maxCatchUpPages) { this.coverage = 'partial'; break; }
        const params = {filter: this.filter}; if (this.cursor) params.cursor = this.cursor;
        const result = await this.client.call('events.list', params);
        const data = result.data;
        if (!data || !Array.isArray(data.events) || typeof data.reset_required !== 'boolean'
            || (data.next_cursor !== null && typeof data.next_cursor !== 'string')) throw new BusinessError('补拉协议不完整', 0, result);
        if (data.reset_required) { this.cursor = null; this.coverage = 'expired'; this.seen.clear(); break; }
        for (const event of data.events) await this.accept(event);
        this.cursor = data.next_cursor;
        this.coverage = data.coverage || 'unknown';
        if (data.next_cursor === null) break;
      }
      this.onUpdate({stream_item: 'recovered', coverage: this.coverage, cursor: this.cursor});
    }
    async accept(item) {
      const kind = item.stream_item;
      if (kind === 'gap' || kind === 'cursor_expired') {
        this.onUpdate(item); await this.recover(kind === 'cursor_expired'); return;
      }
      if (!['event', 'snapshot', 'heartbeat'].includes(kind)) throw new BusinessError('未知观察协议', 0);
      if (kind === 'event') {
        if (!item.event_id) throw new BusinessError('事件缺少稳定身份', 0);
        if (this.seen.has(item.event_id)) return;
        this.seen.add(item.event_id);
        // 只作有界去重缓存；不是持久事实或权限来源。
        if (this.seen.size > 1000) this.seen.delete(this.seen.values().next().value);
      }
      if (item.cursor) this.cursor = item.cursor;
      this.coverage = item.coverage || 'unknown'; this.onUpdate(item);
    }
    async run(signal) {
      const request = {filter: this.filter, request_id: identifier()}; if (this.cursor) request.cursor = this.cursor;
      let reader;
      try {
        const response = await this.client.fetcher('/client/business/stream', {method: 'POST', credentials: 'same-origin',
          headers: {...this.client.headers(), Accept: 'application/x-ndjson'}, body: JSON.stringify(request), signal});
        if (!response.ok || !response.body) throw new BusinessError('观察未连接', response.status);
        reader = response.body.getReader();
        const queue = [];
        const decoder = new NDJSONDecoder(item => { if (queue.length >= 256) throw new BusinessError('观察缓存已满', 0); queue.push(item); });
        while (!signal?.aborted) {
          const {done, value} = await reader.read();
          if (done) { decoder.finish(); break; }
          decoder.feed(value);
          while (queue.length) await this.accept(queue.shift());
        }
        if (!signal?.aborted) await this.recover();
      } catch (error) {
        if (!signal?.aborted) { this.coverage = 'unknown'; this.onUpdate({stream_item: 'disconnected', coverage: 'unknown'}); await this.recover(); }
      } finally {
        if (reader) {
          // 已中止的真实网络流在cancel时仍可能拒绝；不让清理阶段覆盖已经完成的补拉。
          try { await reader.cancel(); }
          catch (error) { if (error.name !== 'AbortError') throw error; }
          finally { reader.releaseLock(); }
        }
      }
    }
  }

  const _agentRevision = async (client, agentId) => {
    const res = await client.call('background_agents.get', {agent_id: agentId});
    return res.data?.agent?.identity_revision ?? res.data?.agent?.revision ?? 0;
  };

  function createReferenceSurface(options = {}) {
    const fetcher = options.fetcher || (typeof globalThis !== 'undefined' && globalThis.fetch ? globalThis.fetch.bind(globalThis) : null);
    const csrf = options.csrf || (() => (typeof document !== 'undefined' ? (document.querySelector('meta[name="my-agent-csrf"]')?.content || document.querySelector('meta[name="csrf-token"]')?.content || '') : ''));
    const client = options.client || new BusinessClient(fetcher, csrf);
    return {
      client,
      BusinessClient,
      BusinessObservation,
      NDJSONDecoder,
      BusinessError,
      displayUnknown,
      controller: {
        getController: (params = {}) => client.call('controllers.get', params),
        submitTask: (spaceId, instruction, extra = {}) => client.call('tasks.submit', {space_id: spaceId, instruction, ...extra}, {mutating: true}),
        assignTask: (agentId, spaceId, instruction, extra = {}) => client.call('tasks.assign', {agent_id: agentId, space_id: spaceId, instruction, ...extra}, {mutating: true}),
        resolveInterface: (bindingId, selectedSpaceId) => client.call('interfaces.resolve', {binding_id: bindingId, ...(selectedSpaceId ? {selected_space_id: selectedSpaceId} : {})}),
      },
      materials: {
        listAssets: (spaceId, options = {}) => client.call('assets.list', {space_id: spaceId, ...options}),
        getAsset: (spaceId, assetId) => client.call('assets.get', {space_id: spaceId, asset_id: assetId}),
        registerAsset: (spaceId, name, kind, artifactRef, extra = {}) => client.call('assets.register', {space_id: spaceId, name, kind, artifact_ref: artifactRef, ...extra}, {mutating: true}),
        listVersions: (spaceId, assetId, options = {}) => client.call('assets.versions.list', {space_id: spaceId, asset_id: assetId, ...options}),
        uploadArtifact: (file, spaceId, key, taskId) => client.uploadArtifact(file, spaceId, key, taskId),
        readArtifact: (artifactId, versionRef, range) => client.readArtifact(artifactId, versionRef, range),
      },
      packages: {
        catalogPackages: (options = {}) => client.call('spaces.packages.catalog', options),
        listPackages: (spaceId, options = {}) => client.call('spaces.packages.list', {space_id: spaceId, ...options}),
        previewPackages: (spaceId, mounts) => client.call('spaces.packages.preview', {space_id: spaceId, mounts}),
        listWorkflows: (options = {}) => client.call('workflows.list', options),
        getWorkflow: (workflowRef) => client.call('workflows.get', {workflow_ref: workflowRef}),
        runWorkflow: (workflowRef, spaceId, inputs = {}) => client.call('workflows.run', {workflow_ref: workflowRef, space_id: spaceId, inputs}, {mutating: true}),
      },
      team: {
        listAgents: (options = {}) => client.call('background_agents.list', options),
        getAgent: (agentId) => client.call('background_agents.get', {agent_id: agentId}),
        suspendAgent: async (agentId, expectedRevision) => {
          const rev = expectedRevision !== undefined ? expectedRevision : await _agentRevision(client, agentId);
          return client.call('background_agents.suspend', {agent_id: agentId}, {mutating: true, expectedRevision: rev});
        },
        resumeAgent: async (agentId, expectedRevision) => {
          const rev = expectedRevision !== undefined ? expectedRevision : await _agentRevision(client, agentId);
          return client.call('background_agents.resume', {agent_id: agentId}, {mutating: true, expectedRevision: rev});
        },
        retireAgent: async (agentId, disposition = 'drain', handoffAgentId, expectedRevision) => {
          const rev = expectedRevision !== undefined ? expectedRevision : await _agentRevision(client, agentId);
          return client.call('background_agents.retire', {agent_id: agentId, disposition, ...(handoffAgentId ? {handoff_agent_id: handoffAgentId} : {})}, {mutating: true, expectedRevision: rev});
        },
        sendMessage: (agentId, text, kind = 'message') => client.call('background_agents.messages.send', {agent_id: agentId, text, input_kind: kind}, {mutating: true}),
        listMessages: (target, options = {}) => client.call('messages.list', {target, ...options}),
        getAgentTree: (spaceId, rootAgentId) => client.call('agent_tree.get', {space_id: spaceId, root_agent_id: rootAgentId}),
      },
      executions: {
        listExecutions: (filter = {}, options = {}) => client.call('executions.list', {filter, ...options}),
        listToolCalls: (filter = {}, options = {}) => client.call('tool_calls.list', {filter, ...options}),
        getToolCall: (operationId) => client.call('tool_calls.get', {operation_id: operationId}),
        readLogs: (sourceRef, options = {}) => client.call('logs.read', {source_ref: sourceRef, ...options}),
        listEvents: (filter = {}, options = {}) => client.call('events.list', {filter, ...options}),
        createObservation: (onUpdate, filter = {}) => new BusinessObservation(client, onUpdate, filter),
      },
      delivery: {
        listArtifacts: (filter = {}, options = {}) => client.call('artifacts.list', {filter, ...options}),
        getChanges: (taskId, candidateRef) => client.call('changes.get', {task_id: taskId, ...(candidateRef ? {candidate_ref: candidateRef} : {})}),
        listApprovals: (filter = {}, options = {}) => client.call('approvals.list', {filter, ...options}),
        decideApproval: (approvalId, decision, responseTicket) => client.call('approvals.decide', {approval_id: approvalId, decision, response_ticket: responseTicket}, {mutating: true}),
        getPolicies: (spaceId) => client.call('policies.get', {space_id: spaceId}),
        getPermissions: (target, taskId) => client.call('permissions.get', {target, ...(taskId ? {task_id: taskId} : {})}),
      },
    };
  }

  const exported = {BusinessClient, BusinessObservation, NDJSONDecoder, BusinessError, displayUnknown, createReferenceSurface};
  if (typeof module !== 'undefined' && module.exports) module.exports = exported;
  root.BusinessUI = exported;
  root.createReferenceSurface = createReferenceSurface;
  if (typeof document === 'undefined') return;

  // DOM 内容只走 textContent，不把模型/日志/文件名拼成 HTML 或宿主路径。
  const $ = id => document.getElementById(id);
  const api = new BusinessClient(globalThis.fetch.bind(globalThis), () => document.querySelector('meta[name="my-agent-csrf"]')?.content || document.querySelector('meta[name="csrf-token"]')?.content || '');
  let supported = new Set(), observation = null, streamStop = null;
  let workflowNumbers = new Map(), workflowVisible = new Set();
  function notice(text) { const el = $('connection'); if (el) el.textContent = text; }
  function show(id, value) { const el = $(id); if (el) el.textContent = JSON.stringify(value, null, 2); }
  function space() { const value = $('space').value; if (!value) throw new Error('请先选择真实空间'); return value; }
  function renderRows(id, data, keys) {
    const target = $(id); if (!target) return; target.replaceChildren();
    const coverage = document.createElement('p'); coverage.className = 'muted';
    coverage.textContent = `覆盖：${data?.coverage || 'unknown'}${data?.next_cursor ? ' · 尚有下一页' : ''}`; target.append(coverage);
    if (!Array.isArray(data?.items)) return;
    for (const row of data.items) {
      const detail = document.createElement('details'), summary = document.createElement('summary'), body = document.createElement('pre');
      summary.textContent = keys.map(key => displayUnknown(row[key])).join(' · ');
      body.textContent = JSON.stringify(row, null, 2); detail.append(summary, body); target.append(detail);
    }
  }
  async function call(action, params, options = {}) {
    if (supported.size > 0 && !supported.has(action)) throw new Error(`接口未接通：${action}`);
    return api.call(action, params, options);
  }
  async function perform(fn) {
    try { await fn(); } catch (error) { notice(error.status === 0 ? `结果未知；${error.message}` : `${error.message}（${displayUnknown(error.status)}）`); }
  }
  function wire(id, fn) { const el = $(id); if (el) el.addEventListener('click', () => perform(fn)); }
  async function refresh() {
    const id = space(), filter = {space_id: id};
    const reads = [
      ['assets.list', {space_id: id}, 'materials-data', ['asset_ref', 'name']],
      ['spaces.packages.list', {space_id: id}, 'packages-data', ['package_ref', 'enabled']],
      ['background_agents.list', {space_id: id}, 'team-data', ['name', 'space_id', 'purpose', 'model_profile_id']],
      ['executions.list', {filter}, 'executions-data', ['run_id', 'status']],
      ['artifacts.list', {filter}, 'artifacts-data', ['artifact_id', 'version_ref']],
      ['approvals.list', {filter}, 'approvals-data', ['approval_id', 'status']],
    ];
    await Promise.all(reads.map(async ([action, params, target, keys]) => {
      try { const result = await call(action, params); renderRows(target, result.data, keys); }
      catch (error) { const el = $(target); if (el) el.textContent = `未知 / 未连接：${error.message}`; }
    }));
    if (supported.size === 0 || supported.has('workflows.list')) {
      try {
        const result = await call('workflows.list', {space_id: id});
        renderWorkflows(result.data);
      } catch (error) {
        const el = $('workflows-data'); if (el) el.replaceChildren();
      }
    }
    if (supported.size === 0 || supported.has('usage.get')) {
      try {
        const result = await call('usage.get', {filter});
        show('usage-data', result.data);
      } catch {}
    }
  }
  function renderWorkflows(data) {
    const target = $('workflows-data'); if (!target) return;
    target.replaceChildren(); workflowVisible = new Set();
    for (const row of data?.items || []) {
      const ref = row.workflow_ref; workflowVisible.add(ref);
      if (!workflowNumbers.has(ref)) workflowNumbers.set(ref, workflowNumbers.size + 1);
      const tr = document.createElement('tr');
      for (const text of [`${workflowNumbers.get(ref)} ${row.name}`, row.scope?.scope_mode === 'spaces' ? row.scope.space_ids.join('、') : ({owner_global: '所有空间', unassigned: '未分配'}[row.scope?.scope_mode] || '未知')]) {
        const td = document.createElement('td'); td.textContent = text; tr.append(td);
      }
      tr.addEventListener('click', () => perform(async () => show('workflow-detail', (await call('workflows.get', {workflow_ref: ref})).data)));
      target.append(tr);
    }
    const cov = $('workflow-coverage');
    if (cov) cov.textContent = `覆盖：${data?.coverage || 'unknown'}${data?.next_cursor ? ' · 尚有下一页' : ''}`;
  }
  wire('connect', async () => {
    const result = await api.call('system.capabilities', {});
    if (!Array.isArray(result.data?.supported_actions)) throw new Error('接口目录不完整');
    supported = new Set(result.data.supported_actions);
    const spaces = await call('spaces.list', {}); $('space').replaceChildren();
    for (const row of spaces.data?.items || []) {
      const option = document.createElement('option'); option.value = row.space_id; option.textContent = row.name; $('space').append(option);
    }
    notice(`已连接认证 API · 空间覆盖 ${spaces.data?.coverage || 'unknown'} · 费用见原始用量；未覆盖仍未知`);
    if ($('space').value) await refresh();
  });
  wire('refresh', refresh);
  const spaceSelect = $('space');
  if (spaceSelect) spaceSelect.addEventListener('change', () => { streamStop?.abort(); observation = null; perform(refresh); });

  // 1. Controller & task input
  wire('controller-get', async () => show('controller-info', (await call('controllers.get', {})).data));
  wire('submit', async () => {
    const request = api.prepare('tasks.submit', {space_id: space(), instruction: $('instruction').value}, {mutating: true});
    if (supported.size > 0 && !supported.has('tasks.submit')) throw new Error('任务提交未接通');
    show('controller-result', await api.send(request));
  });
  wire('task-assign', async () => {
    const agentId = $('assign-agent-id').value.trim();
    const instruction = $('assign-instruction').value;
    if (!agentId || !instruction) throw new Error('请提供助手引用与指派指令');
    show('assign-result', await call('tasks.assign', {agent_id: agentId, space_id: space(), instruction}, {mutating: true}));
  });
  wire('interface-resolve', async () => {
    const bindingId = $('interface-binding-id').value.trim();
    if (!bindingId) throw new Error('请提供绑定引用');
    show('interface-result', (await call('interfaces.resolve', {binding_id: bindingId, selected_space_id: space()})).data);
  });
  wire('observe', async () => {
    streamStop?.abort(); streamStop = new AbortController();
    observation ||= new BusinessObservation(api, item => {
      const list = $('events');
      if (list) {
        const line = document.createElement('li'); line.textContent = JSON.stringify(item);
        list.append(line); while (list.children.length > 100) list.firstChild.remove();
      }
      const obsState = $('observation-state');
      if (obsState) obsState.textContent = `覆盖：${observation.coverage} · 观察不代表执行/发布成功`;
    }, {space_id: space()});
    await observation.run(streamStop.signal);
  });
  wire('stop-observe', async () => { streamStop?.abort(); const el = $('observation-state'); if (el) el.textContent = '观察已断开；后台工作未取消'; });

  // 2. Business material
  wire('asset-get', async () => {
    const assetId = $('asset-id').value.trim();
    if (!assetId) throw new Error('请提供资料引用 (asset_id)');
    show('asset-detail', (await call('assets.get', {space_id: space(), asset_id: assetId})).data);
  });
  wire('asset-versions', async () => {
    const assetId = $('asset-id').value.trim();
    if (!assetId) throw new Error('请提供资料引用 (asset_id)');
    show('asset-detail', (await call('assets.versions.list', {space_id: space(), asset_id: assetId})).data);
  });
  wire('asset-register', async () => {
    const name = $('asset-reg-name').value.trim();
    const kind = $('asset-reg-kind').value.trim() || 'document';
    const artifactRef = $('asset-reg-artifact-ref').value.trim();
    if (!name || !artifactRef) throw new Error('请提供资料名称与产物引用');
    show('asset-reg-result', await call('assets.register', {space_id: space(), name, kind, artifact_ref: artifactRef}, {mutating: true}));
  });
  let uploadKey = null;
  const mediaInput = $('media-file');
  if (mediaInput) mediaInput.addEventListener('change', () => { uploadKey = null; });
  wire('upload', async () => {
    const file = $('media-file').files[0]; if (!file) throw new Error('请选择文件');
    uploadKey ||= identifier(); show('upload-result', await api.uploadArtifact(file, space(), uploadKey));
  });
  wire('preview-artifact', async () => {
    const id = $('artifact-id').value, version = $('version-ref').value;
    if (!id || !version) throw new Error('请提供准确产物与版本引用');
    const blob = await api.readArtifact(id, version);
    const url = URL.createObjectURL(blob), viewer = document.createElement(blob.type.startsWith('video/') ? 'video' : blob.type.startsWith('audio/') ? 'audio' : blob.type.startsWith('image/') ? 'img' : 'a');
    if (viewer.tagName === 'A') { viewer.href = url; viewer.textContent = '下载当前认证版本'; viewer.download = id; }
    else { viewer.src = blob.type.startsWith('video/') || blob.type.startsWith('audio/') ? api.artifactURL(id, version) : url; viewer.controls = true; viewer.alt = '已认证产物'; }
    const prev = $('media-preview');
    if (prev) {
      const old = prev.dataset.objectUrl; if (old) URL.revokeObjectURL(old);
      prev.dataset.objectUrl = url; prev.replaceChildren(viewer);
    }
  });

  // 3. Packages & workflows
  wire('packages-catalog', async () => show('packages-catalog-result', (await call('spaces.packages.catalog', {})).data));
  wire('packages-preview', async () => {
    let mounts = [];
    try { mounts = JSON.parse($('packages-preview-mounts').value || '[]'); } catch { throw new Error('挂载参数非合法JSON'); }
    show('packages-preview-result', (await call('spaces.packages.preview', {space_id: space(), mounts})).data);
  });
  wire('workflow-run', async () => {
    const number = Number($('workflow-number').value);
    const ref = [...workflowNumbers].find(([ref, n]) => n === number && workflowVisible.has(ref))?.[0];
    if (!ref) throw new Error('编号已失效，请重新查看流程列表');
    show('workflow-detail', await call('workflows.run', {workflow_ref: ref, space_id: space(), inputs: JSON.parse($('workflow-inputs').value)}, {mutating: true}));
  });

  // 4. Background team
  wire('team-get', async () => {
    const agentId = $('agent-id').value.trim();
    if (!agentId) throw new Error('请提供准确 Agent 引用');
    const result = await call('background_agents.get', {agent_id: agentId});
    show('team-result', result.data);
    if (result.data?.scheduling?.reason_code) {
      notice(`Agent 调度状态：${result.data.scheduling.reason_code}`);
    }
  });
  wire('team-suspend', async () => {
    const agentId = $('agent-id').value.trim();
    if (!agentId) throw new Error('请提供准确 Agent 引用');
    const expectedRevision = await _agentRevision(api, agentId);
    show('team-result', await call('background_agents.suspend', {agent_id: agentId}, {mutating: true, expectedRevision}));
  });
  wire('team-resume', async () => {
    const agentId = $('agent-id').value.trim();
    if (!agentId) throw new Error('请提供准确 Agent 引用');
    const expectedRevision = await _agentRevision(api, agentId);
    show('team-result', await call('background_agents.resume', {agent_id: agentId}, {mutating: true, expectedRevision}));
  });
  wire('team-retire', async () => {
    const agentId = $('agent-id').value.trim();
    const disposition = $('retire-disposition').value;
    if (!agentId) throw new Error('请提供准确 Agent 引用');
    const expectedRevision = await _agentRevision(api, agentId);
    show('team-result', await call('background_agents.retire', {agent_id: agentId, disposition}, {mutating: true, expectedRevision}));
  });
  wire('team-send', async () => show('team-result', await call('background_agents.messages.send', {
    agent_id: $('agent-id').value, text: $('team-message').value, input_kind: 'message'}, {mutating: true})));
  wire('team-messages', async () => {
    const agentId = $('agent-id').value.trim();
    if (!agentId) throw new Error('请提供准确 Agent 引用');
    show('team-result', (await call('messages.list', {target: {kind: 'background_main', id: agentId}})).data);
  });
  wire('team-tree', async () => show('team-result', (await call('agent_tree.get', {space_id: space(), root_agent_id: $('agent-id').value})).data));

  // 5. Execution records
  wire('tool-calls-list', async () => show('execution-detail', (await call('tool_calls.list', {filter: {space_id: space()}})).data));
  wire('tool-call-get', async () => {
    const opId = $('operation-id').value.trim();
    if (!opId) throw new Error('请提供操作ID (operation_id)');
    show('execution-detail', (await call('tool_calls.get', {operation_id: opId})).data);
  });
  wire('events-list', async () => show('execution-detail', (await call('events.list', {filter: {space_id: space()}})).data));
  wire('read-log', async () => show('execution-detail', (await call('logs.read', {source_ref: $('log-ref').value})).data));

  // 6. Artifacts, review & release outcomes
  wire('changes-get', async () => {
    const taskId = $('changes-task-id').value.trim();
    if (!taskId) throw new Error('请提供任务ID (task_id)');
    show('changes-result', (await call('changes.get', {task_id: taskId})).data);
  });
  wire('policies-get', async () => show('delivery-info', (await call('policies.get', {space_id: space()})).data));
  wire('permissions-get', async () => {
    const target = $('permission-target').value.trim();
    if (!target) throw new Error('请提供权限目标引用');
    const kind = $('permission-target-kind')?.value || 'background_main';
    show('delivery-info', (await call('permissions.get', {target: {kind, id: target}})).data);
  });
  wire('approval-decide', async () => show('approval-result', await call('approvals.decide', {
    approval_id: $('approval-id').value, decision: $('decision').value, response_ticket: $('response-ticket').value}, {mutating: true})));

  root.addEventListener('pagehide', () => streamStop?.abort());
})(typeof window === 'undefined' ? globalThis : window);
