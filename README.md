# statusline

[中文](./README.zh-CN.md)

Minimal, color-coded terminal statuslines for **Antigravity CLI (`agy`)** and **pi coding agent (`pi`)**.

## Antigravity CLI (`agy`)

Single-line 256-color statusline for light themes (`Light` / `Solarized Light`), zero third-party dependencies.

![agy statusline preview](./agy/assets/preview.png)

- **Type × value colors**: distinct color families for `cwd`, `5h`, `week`, `ctx`, `cache`, `t/s`, and `model` (+ thinking tier).
- **Accurate speed**: pauses timing during `tool_use` and uses a token-weighted window.
- **Orchestrator dual-rate**: tracks underlying models via `transcript_path` (e.g. `N~85/H~150t/s`).

**One-line install:**

```bash
mkdir -p ~/.gemini/antigravity-cli && curl -fsSL https://raw.githubusercontent.com/Hiaa1/statusline/main/agy/statusline.py -o ~/.gemini/antigravity-cli/statusline.py && chmod +x ~/.gemini/antigravity-cli/statusline.py && python3 -c 'import json,pathlib as P; d=P.Path.home()/".gemini/antigravity-cli"; p=d/"settings.json"; s=json.loads(p.read_text()) if p.exists() else {}; s["statusLine"]={"type":"command","command":f"/usr/bin/python3 {d}/statusline.py","enabled":True}; p.write_text(json.dumps(s,indent=2)+"\n")'
```

> Or tell your agent: `Install the agy statusline from https://github.com/Hiaa1/statusline`

---

## pi coding agent (`pi`)

Two-line custom footer extension (`pi/extensions/cny-footer.ts`) with responsive width degradation.

![pi statusline preview](./pi/assets/preview.png)

- **Two-line layout**: `cwd (branch) • session` + `context │ speed │ hit │ cost` and right-aligned `model • thinking`.
- **CNY peak/off-peak billing**: official DeepSeek Beijing-time pricing (fallback USD × `PI_CNY_RATE`, default `7.2`).
- **Speed & commands**: TTFT + decode `avg(current)` persisted across `/reload`; built-in `/speed [all]` and `/bar [auto|full|short|nano]`.

**One-line install:**

```bash
mkdir -p ~/.pi/agent/extensions && curl -fsSL https://raw.githubusercontent.com/Hiaa1/statusline/main/pi/extensions/cny-footer.ts -o ~/.pi/agent/extensions/cny-footer.ts
```

> Restart `pi` or run `/reload`. Or tell your agent: `Install the pi statusline from https://github.com/Hiaa1/statusline`
