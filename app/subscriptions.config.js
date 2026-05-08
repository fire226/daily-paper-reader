// Local subscription config module.
// Reads/writes config.yaml through the local server API used by serve.py.

window.SubscriptionsConfig = (function () {
  const getYaml = () => window.jsyaml || window.jsYaml || window.jsYAML;

  const parseConfig = (content) => {
    const yaml = getYaml();
    if (!yaml || typeof yaml.load !== 'function') {
      throw new Error('前端缺少 YAML 解析库（js-yaml），无法解析 config.yaml。');
    }
    return yaml.load(content || '') || {};
  };

  const dumpConfig = (configObject) => {
    const yaml = getYaml();
    if (!yaml || typeof yaml.dump !== 'function') {
      throw new Error('前端缺少 YAML 序列化库（js-yaml），无法写入 config.yaml。');
    }
    return yaml.dump(configObject || {}, { lineWidth: 120 });
  };

  const loadConfig = async () => {
    const res = await fetch('/api/config', { cache: 'no-store' });
    if (!res.ok) {
      throw new Error(`本地读取 config.yaml 失败（HTTP ${res.status}）`);
    }
    const { content } = await res.json();
    return { config: parseConfig(content || '') };
  };

  const saveConfig = async (configObject) => {
    const content = dumpConfig(configObject || {});
    const res = await fetch('/api/config', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content }),
    });
    if (!res.ok) {
      const text = await res.text().catch(() => '');
      throw new Error(`本地写入 config.yaml 失败：${res.status} - ${text}`);
    }
    return await res.json();
  };

  const updateConfig = async (updater) => {
    const { config } = await loadConfig();
    const next = typeof updater === 'function' ? updater({ ...(config || {}) }) || config : config;
    return saveConfig(next);
  };

  return {
    loadConfig,
    saveConfig,
    updateConfig,
  };
})();
