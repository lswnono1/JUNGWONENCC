'use strict';
(async () => {
  const parts = [
    'app-core-1.txt',
    'app-core-2.txt',
    'app-core-3.txt',
    'app-core-4.txt',
    'app-core-5.txt',
    'app-core-6.txt',
    'app-core-7.txt'
  ];
  try {
    const responses = await Promise.all(parts.map((name) => fetch(`./${name}?v=1.5.0`, {cache:'no-store'})));
    for (const response of responses) {
      if (!response.ok) throw new Error(`앱 구성 파일 오류 HTTP ${response.status}`);
    }
    const combined = (await Promise.all(responses.map((response) => response.text()))).join('\n');
    const source = combined.replace(/^\s*boot\(\);\s*$/gm, '');
    (0, eval)(`${source}\nboot();`);
  } catch (error) {
    document.body.innerHTML = `<div style="font-family:sans-serif;padding:24px"><h1>앱 시작 실패</h1><p>${String(error.message || error)}</p><button onclick="location.reload()">다시 시도</button></div>`;
  }
})();
