'use strict';
(async () => {
  const version = '1.5.2';
  const parts = Array.from({length:9}, (_, index) => `app-core-${index + 1}.txt`);
  try {
    const responses = await Promise.all(parts.map((name) => fetch(`./${name}?v=${version}`, {cache:'no-store'})));
    for (const response of responses) {
      if (!response.ok) throw new Error(`앱 구성 파일 오류 HTTP ${response.status}`);
    }
    const combined = (await Promise.all(responses.map((response) => response.text()))).join('\n');
    const source = combined.replace(/^\s*boot\(\);\s*$/gm, '');
    (0, eval)(`${source}\nboot();`);
  } catch (error) {
    const panel = document.createElement('div');
    panel.style.cssText = 'font-family:sans-serif;padding:24px';
    const title = document.createElement('h1'); title.textContent = '앱 시작 실패';
    const detail = document.createElement('p'); detail.textContent = String(error.message || error);
    const retry = document.createElement('button'); retry.textContent = '다시 시도'; retry.onclick = () => location.reload();
    panel.append(title, detail, retry); document.body.replaceChildren(panel);
  }
})();
