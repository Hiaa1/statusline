#!/usr/bin/env python3
# agy statusLine 渲染脚本（单行，浅色主题友好）
# 输入：agy 通过 stdin 传入的 JSON payload
# 输出：一行带 ANSI 颜色的状态栏；任何异常都降级为纯文本，绝不崩
import json
import os
import re
import sys
import time

STATE_DIR = "/tmp"
BUSY_STATES = {"working", "thinking", "tool", "streaming", "generating"}
RATE_TTL = 300.0  # 空闲后 ~N 秒仍显示上一轮速度，超过就隐藏


# ---------- 格式化 ----------
def fmt_tokens(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "--"
    if n <= 0:
        return "0"
    if n < 1000:
        return f"{n:.0f}"
    if n < 1_000_000:
        return f"{n / 1000:.0f}k"
    return f"{n / 1_000_000:.1f}M"


def fmt_cwd(path):
    home = os.path.expanduser("~")
    if not path:
        return "?"
    if path == home:
        return "~"
    if path.startswith(home + os.sep):
        rel = path[len(home) + 1:]
        parts = [p for p in rel.split(os.sep) if p]
        if len(parts) > 2:
            return "~/…/" + "/".join(parts[-2:])
        return "~/" + rel
    parts = [p for p in path.split(os.sep) if p]
    return "/…/" + "/".join(parts[-2:]) if len(parts) > 2 else path


def shorten_model(model):
    name = (model or {}).get("display_name") or (model or {}).get("id") or "?"
    return re.sub(r"^Gemini \d+(?:\.\d+)?\s+", "", name).strip()


def quota_group(quota, model_id):
    mid = (model_id or "").lower()
    third_party = any(k in mid for k in ("claude", "gpt", "oss", "gemma"))
    group = "3p" if third_party else "gemini"
    keys = [k for k in (quota or {}) if isinstance((quota or {}).get(k), dict)]
    if not any(k.startswith(group + "-") for k in keys) and keys:
        group = keys[0].split("-")[0]
    return group


def window_label(key):
    if key.endswith("5h"):
        return "5h"
    if "weekly" in key:
        return "week"
    return key.rsplit("-", 1)[-1]


def pick_quota(quota, model_id):
    """选当前模型组的短窗口（5h）：优先精确名，否则剩得最少且 reset 最短的那个。"""
    if not isinstance(quota, dict) or not quota:
        return None, None
    group = quota_group(quota, model_id)
    exact = f"{group}-5h"
    if isinstance(quota.get(exact), dict):
        return exact, quota[exact]
    cands = {
        k: v for k, v in quota.items()
        if isinstance(v, dict) and k.startswith(group + "-") and "weekly" not in k
    } or {k: v for k, v in quota.items() if isinstance(v, dict) and "weekly" not in k}
    if not cands:
        cands = {k: v for k, v in quota.items() if isinstance(v, dict)}
    if not cands:
        return None, None

    def reset_of(item):
        try:
            return float(item[1].get("reset_in_seconds") or 1 << 60)
        except (TypeError, ValueError):
            return float(1 << 60)

    return min(cands.items(), key=reset_of)


def quota_windows(quota, qkey):
    """返回要显示的窗口：短窗口 + 周窗口，形如 [(k, bucket), ...]。"""
    out = [(qkey, quota[qkey])]
    group = qkey.split("-")[0]
    weeks = [k for k in quota if "weekly" in k]
    week_key = f"{group}-weekly"
    if week_key not in weeks:
        week_key = next((k for k in weeks if k.startswith(group + "-")), weeks[0] if weeks else None)
    if week_key and week_key != qkey and isinstance(quota.get(week_key), dict):
        out.append((week_key, quota[week_key]))
    return out


# ---------- tok/s 状态文件 ----------
def state_path(session_id):
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id or "nosession")[:64]
    return os.path.join(STATE_DIR, f"agy-statusline-{os.getuid()}-{safe}.json")


def load_state(p):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(p, st):
    try:
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f)
        os.replace(tmp, p)
    except Exception:
        pass


def update_rate(session_id, agent_state, total_out):
    """按「一轮工作」的平均输出速度估算 tok/s，lump 更新也不会爆表。"""
    now = time.time()
    p = state_path(session_id)
    st = load_state(p)
    if st.get("sid") != session_id:
        st = {"sid": session_id}
    prev_out = st.get("out")
    busy_now = (agent_state or "").lower() in BUSY_STATES
    rate = st.get("rate")

    if st.get("burst_start") is None and busy_now:
        st["burst_start"] = now
        st["burst_out"] = total_out if prev_out is None else prev_out
    if total_out is not None and prev_out is not None and total_out != prev_out:
        bs = st.get("burst_start")
        base_out = st.get("burst_out", prev_out)
        span = now - bs if bs else now - st.get("ts", now)
        dout = total_out - base_out
        if span >= 1.0 and dout > 0:
            inst = dout / span
            rate = inst if rate is None else 0.5 * inst + 0.5 * rate
            st["rate"] = rate
            st["rate_ts"] = now
    if not busy_now:
        st.pop("burst_start", None)
        st.pop("burst_out", None)

    st["ts"] = now
    if total_out is not None:
        st["out"] = total_out
    save_state(p, st)

    if rate and st.get("rate_ts") and now - st["rate_ts"] <= RATE_TTL:
        return rate
    return None


# ---------- 颜色（浅色背景，256 色 ANSI，按类型与数值区分） ----------
DIM = "38;5;244"
CWD_COLOR = "38;5;239"


def color(code, text, enabled=True):
    return f"\x1b[{code}m{text}\x1b[0m" if enabled and text else text


def color_quota(key, pct):
    """5h 短窗口走森林绿系，week 周窗口走深青系；低配额统一转暖/加粗红。"""
    if "weekly" in (key or ""):
        if pct >= 80:
            return "38;5;30"
        if pct >= 50:
            return "38;5;31"
        if pct >= 20:
            return "38;5;166"
        return "1;38;5;124"
    if pct >= 80:
        return "38;5;28"
    if pct >= 50:
        return "38;5;64"
    if pct >= 20:
        return "38;5;136"
    return "1;38;5;124"


def color_ctx(pct):
    """上下文占用：宝蓝 → 靛紫 → 深橙 → 加粗红。"""
    if pct < 30:
        return "38;5;25"
    if pct < 60:
        return "38;5;56"
    if pct < 80:
        return "38;5;166"
    return "1;38;5;160"


def color_cache(hit):
    """缓存命中率：梅紫 → 暖棕 → 砖红。"""
    if hit >= 80:
        return "38;5;126"
    if hit >= 50:
        return "38;5;130"
    return "38;5;124"


def color_rate(rate):
    """输出速度：淡灰 → 铜棕 → 琥珀橙 → 翠绿加粗。"""
    if rate >= 120:
        return "1;38;5;29"
    if rate >= 60:
        return "38;5;166"
    if rate >= 25:
        return "38;5;94"
    return "38;5;243"


def model_family_color(model):
    """按模型家族分主色：Claude 暖陶橙、GPT 墨绿、Flash 青蓝、Pro/Neon 靛紫。"""
    m = model or {}
    text = f"{m.get('id') or ''} {m.get('display_name') or ''}".lower()
    if "claude" in text:
        return "38;5;130"
    if "gpt" in text or "openai" in text or re.search(r"(?:^|[-_\s])o[134](?:$|[-_\s])", text):
        return "38;5;22"
    if "flash" in text:
        return "38;5;31"
    if any(k in text for k in ("neon", "pro", "ultra", "gemini")):
        return "38;5;55"
    return "38;5;24"


def format_model(model, use_color):
    label = shorten_model(model)
    base_code = model_family_color(model)
    m = re.match(r"^(.+?)(\s*\([^)]+\))\s*$", label)
    if not m:
        return color(base_code, label, use_color)
    base, tier_raw = m.group(1), m.group(2)
    tier = tier_raw.strip(" ()").lower()
    if any(k in tier for k in ("xhigh", "max", "ultra")):
        tier_code = "1;38;5;125"
    elif any(k in tier for k in ("high", "thinking")):
        tier_code = f"1;{base_code}"
    elif "med" in tier:
        tier_code = "38;5;61"
    elif any(k in tier for k in ("low", "min", "fast")):
        tier_code = DIM
    else:
        tier_code = base_code
    return color(base_code, base, use_color) + color(tier_code, tier_raw, use_color)


def build(payload):
    use_color = not os.environ.get("NO_COLOR")
    sep = f"\x1b[{DIM}m · \x1b[0m" if use_color else " · "

    # 顺序：cwd → quota(5h/week) → ctx → cache → t/s → 模型
    parts = []

    # 工作目录
    cwd = (payload.get("workspace") or {}).get("current_dir") or payload.get("cwd") or ""
    parts.append(color(CWD_COLOR, fmt_cwd(cwd), use_color))

    # Quota：短窗口 + 周窗口，形如 97% 5h · 100% week
    model_id = (payload.get("model") or {}).get("id", "")
    qkey, qb = pick_quota(payload.get("quota"), model_id)
    if qb is not None:
        for key, bucket in quota_windows(payload.get("quota"), qkey):
            try:
                pct = max(0.0, min(1.0, float(bucket.get("remaining_fraction")))) * 100
                code = color_quota(key, pct)
                parts.append(color(code, f"{pct:.0f}% {window_label(key)}", use_color))
            except (TypeError, ValueError):
                continue

    # 上下文
    cw = payload.get("context_window") or {}
    try:
        size = float(cw.get("context_window_size") or 0)
        pct = float(cw.get("used_percentage") or 0)
        if size > 0:
            used = pct / 100.0 * size
            label = f"ctx {fmt_tokens(used)}/{fmt_tokens(size)}"
            parts.append(color(color_ctx(pct), label, use_color))
        else:
            parts.append(color(DIM, "ctx --", use_color))
    except (TypeError, ValueError):
        parts.append(color(DIM, "ctx --", use_color))

    # 缓存命中率（最近一轮）
    cu = cw.get("current_usage") or {}
    try:
        cr = float(cu.get("cache_read_input_tokens") or 0)
        it = float(cu.get("input_tokens") or 0)
        cc = float(cu.get("cache_creation_input_tokens") or 0)
        denom = cr + it + cc
        if denom > 0:
            hit = cr / denom * 100
            parts.append(color(color_cache(hit), f"cache {hit:.0f}%", use_color))
        else:
            parts.append(color(DIM, "cache --", use_color))
    except (TypeError, ValueError):
        parts.append(color(DIM, "cache --", use_color))

    # 输出速度（估算）
    try:
        rate = update_rate(
            payload.get("session_id") or "",
            payload.get("agent_state") or "",
            int(cw.get("total_output_tokens") or 0),
        )
        if rate:
            parts.append(color(color_rate(rate), f"~{rate:.0f}t/s", use_color))
    except Exception:
        pass

    # 模型名放最后
    parts.append(format_model(payload.get("model"), use_color))

    return sep.join(parts)


def main():
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    try:
        line = build(payload)
    except Exception:
        line = ""
    print(line)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print("agy")
        sys.exit(0)
