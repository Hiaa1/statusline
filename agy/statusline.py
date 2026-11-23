#!/usr/bin/env python3
# agy statusLine 渲染脚本（单行，浅色主题友好）
# 输入：agy 通过 stdin 传入的 JSON payload
# 输出：一行带 ANSI 颜色的状态栏；任何异常都降级为纯文本，绝不崩
import json
import os
import re
import sys
import time
import unicodedata

STATE_DIR = "/tmp"
GEN_STATES = {"working", "thinking", "streaming", "generating"}
RATE_TTL = 300.0  # 空闲后 ~N 秒仍显示上一轮速度，超过就隐藏
MAX_SAMPLES = 6   # 每个底层模型保留最近 N 条采样做 token 加权均值
MAX_SPAN_SEC = 600.0
MIN_SPAN_SEC = 0.2
MAX_READ_BYTES = 262144
WIDTH_MARGIN = 4  # 为 agy TUI 左右内边距预留列数，避免窄屏折行
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


# ---------- 可见宽度与截断 ----------
def char_width(ch):
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def visible_width(text):
    return sum(char_width(ch) for ch in ANSI_RE.sub("", text))


def truncate_ansi(text, max_width):
    """按可见列宽安全截断带 ANSI 颜色的字符串，避免残留未闭合转义符。"""
    if max_width <= 0:
        return ""
    if visible_width(text) <= max_width:
        return text
    if max_width == 1:
        return "…"
    budget = max_width - 1
    out = []
    used = 0
    has_ansi = False
    pos = 0
    for m in ANSI_RE.finditer(text):
        seg = text[pos:m.start()]
        for ch in seg:
            w = char_width(ch)
            if used + w > budget:
                out.append("…")
                if has_ansi:
                    out.append("\x1b[0m")
                return "".join(out)
            out.append(ch)
            used += w
        out.append(m.group(0))
        has_ansi = m.group(0) != "\x1b[0m"
        pos = m.end()
    for ch in text[pos:]:
        w = char_width(ch)
        if used + w > budget:
            out.append("…")
            if has_ansi:
                out.append("\x1b[0m")
            return "".join(out)
        out.append(ch)
        used += w
    return "".join(out)


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


def fmt_tokens_tight(n):
    s = fmt_tokens(n)
    return s[:-3] + "M" if s.endswith(".0M") else s


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


def fmt_cwd_short(path, max_len=16):
    home = os.path.expanduser("~")
    if not path:
        return "?"
    if path == home:
        return "~"
    base = os.path.basename(path.rstrip(os.sep)) or path
    if visible_width(base) > max_len:
        return truncate_ansi(base, max_len)
    return base


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


# ---------- 底层模型识别 & tok/s 状态文件 ----------
def is_orchestrator_model(model):
    m = model or {}
    text = f"{m.get('id') or ''} {m.get('display_name') or ''}".lower()
    return "orchestrator" in text


def model_short_tag(model_or_id):
    """把模型名或 model dict 映射为紧凑单字母标签，用于按底层模型分桶测速。"""
    if isinstance(model_or_id, dict):
        text = f"{model_or_id.get('id') or ''} {model_or_id.get('display_name') or ''}".lower()
    else:
        text = str(model_or_id or "").lower()
    if "hydrogen" in text:
        return "H"
    if "neon" in text:
        return "N"
    if "carbon" in text:
        return "C"
    if "argon" in text or "g1-pro" in text:
        return "A"
    if "flash" in text:
        return "F"
    if "opus" in text:
        return "O"
    if "sonnet" in text:
        return "S"
    if "haiku" in text:
        return "K"
    if "gpt" in text or "openai" in text:
        return "G"
    if "pro" in text:
        return "P"
    m = re.search(r"[a-z]", text)
    return m.group(0).upper() if m else "M"


def orchestrator_planner_tag(model):
    """从 Orchestrator (Neon/Argon/Carbon) 提取 Planner 标签用于排序置前。"""
    m = model or {}
    text = f"{m.get('display_name') or ''} {m.get('id') or ''}".lower()
    for key, tag in (("neon", "N"), ("carbon", "C"), ("argon", "A"), ("g1-pro", "A"), ("opus", "O")):
        if key in text:
            return tag
    return None


def state_path(session_id):
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id or "nosession")[:64]
    return os.path.join(STATE_DIR, f"agy-statusline-{os.getuid()}-{safe}.json")


def load_state(p):
    try:
        with open(p, encoding="utf-8") as f:
            st = json.load(f)
            return st if isinstance(st, dict) else {}
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


def read_transcript_deltas(path, st, can_read_from_start):
    """增量 tail transcript.jsonl，返回本轮新增的 [(tag, dout), ...] 并更新 offset/step_outs。"""
    if not path or not os.path.isfile(path):
        return [], False
    try:
        size = os.path.getsize(path)
    except OSError:
        return [], False

    prev_path = st.get("tr_path")
    offset = int(st.get("tr_offset") or 0)
    step_outs = st.get("step_outs")
    if not isinstance(step_outs, dict):
        step_outs = {}

    if prev_path != path or size < offset:
        step_outs = {}
        st["tr_path"] = path
        if size > 0 and not can_read_from_start:
            # 首次接管已有会话（含旧版状态文件升级）：跳到末尾，避免把历史记录算成瞬时速度
            st["tr_offset"] = size
            st["step_outs"] = step_outs
            return [], True
        offset = 0

    if size <= offset:
        st["tr_path"] = path
        st["tr_offset"] = offset
        st["step_outs"] = step_outs
        return [], True

    deltas = []
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            chunk = f.read(MAX_READ_BYTES)
        last_nl = chunk.rfind(b"\n")
        if last_nl == -1:
            return [], True
        complete = chunk[:last_nl + 1]
        new_offset = offset + len(complete)
        for raw in complete.splitlines():
            if not raw.strip():
                continue
            try:
                rec = json.loads(raw.decode("utf-8", errors="replace"))
            except Exception:
                continue
            if not isinstance(rec, dict):
                continue
            model_name = rec.get("model")
            out_tok = rec.get("output_tokens")
            if not model_name or out_tok is None:
                continue
            try:
                out_val = int(out_tok)
            except (TypeError, ValueError):
                continue
            if out_val <= 0:
                continue
            skey = str(rec.get("step_index", f"off:{new_offset}:{len(deltas)}"))
            prev_val = int(step_outs.get(skey, 0))
            if out_val > prev_val:
                deltas.append((model_short_tag(model_name), out_val - prev_val))
                step_outs[skey] = out_val
                st["tr_seen_tokens"] = True
        if len(step_outs) > 32:
            for k in list(step_outs.keys())[:-32]:
                step_outs.pop(k, None)
        st["tr_path"] = path
        st["tr_offset"] = new_offset
        st["step_outs"] = step_outs
    except Exception:
        return [], False
    return deltas, True


def add_bucket_sample(buckets, tag, now, dout, span):
    if dout <= 0 or span < MIN_SPAN_SEC or span > MAX_SPAN_SEC:
        return
    arr = buckets.get(tag)
    if not isinstance(arr, list):
        arr = []
    arr.append([now, float(dout), float(span)])
    cutoff = now - RATE_TTL
    arr = [x for x in arr if isinstance(x, list) and len(x) == 3 and x[0] >= cutoff][-MAX_SAMPLES:]
    buckets[tag] = arr


def bucket_rate(samples, now, min_ts=0.0):
    if not isinstance(samples, list):
        return None
    cutoff = max(now - RATE_TTL, min_ts)
    valid = [x for x in samples if isinstance(x, list) and len(x) == 3 and x[0] >= cutoff and x[2] > 0]
    if not valid:
        return None
    tot_tok = sum(x[1] for x in valid)
    tot_sec = sum(x[2] for x in valid)
    return tot_tok / tot_sec if tot_sec > 0 and tot_tok > 0 else None


def update_rates(payload, now=None):
    """按底层真实模型分桶估算 tok/s；tool_use 期间暂停计时，排除工具执行耗时。

    返回 (is_orch, [(tag, rate), ...])。
    """
    now = time.time() if now is None else float(now)
    session_id = payload.get("session_id") or payload.get("conversation_id") or ""
    agent_state = (payload.get("agent_state") or "").lower()
    model = payload.get("model") or {}
    cw = payload.get("context_window") or {}
    total_out = cw.get("total_output_tokens")
    try:
        total_out = int(total_out) if total_out is not None else None
    except (TypeError, ValueError):
        total_out = None

    outer_id = f"{model.get('id') or ''}|{model.get('display_name') or ''}"
    is_orch = is_orchestrator_model(model)

    p = state_path(session_id)
    st = load_state(p)
    is_fresh = st.get("sid") != session_id
    if is_fresh:
        st = {"sid": session_id}

    buckets = st.get("buckets")
    if not isinstance(buckets, dict):
        buckets = {}
        st["buckets"] = buckets

    # 外层模型切换时记录分界时间戳，避免旧模型样本串进新模式
    if outer_id and st.get("outer_model") != outer_id:
        st["outer_model"] = outer_id
        st["mode_since"] = now
        st["work_sec"] = 0.0
        st["work_since"] = None

    gen_now = agent_state in GEN_STATES
    work_sec = float(st.get("work_sec") or 0.0)
    work_since = st.get("work_since")

    if gen_now:
        if work_since is None:
            work_since = now
        active_span = work_sec + max(0.0, now - float(work_since))
    else:
        if work_since is not None:
            work_sec += max(0.0, now - float(work_since))
            work_since = None
        active_span = work_sec

    prev_ts = st.get("ts")
    prev_out = st.get("out")
    # 仅当我们已从零输出观察到本轮开始、且此前 transcript 尚不存在时，才从头读新建的 transcript
    can_read_from_start = (
        not is_fresh
        and not st.get("tr_path")
        and int(prev_out or 0) == 0
        and active_span > 0
    )
    tr_deltas, has_transcript = read_transcript_deltas(payload.get("transcript_path") or "", st, can_read_from_start)

    # 兼容状态先翻到 idle、transcript 晚几百毫秒落盘的异步竞态
    pending_span = 0.0
    if st.get("pending_work_sec") and st.get("pending_ts"):
        if now - float(st["pending_ts"]) <= 3.0:
            pending_span = float(st["pending_work_sec"])
        else:
            st.pop("pending_work_sec", None)
            st.pop("pending_ts", None)

    recorded = False
    if tr_deltas:
        span = active_span if active_span >= MIN_SPAN_SEC else pending_span
        if span < MIN_SPAN_SEC and prev_ts is not None:
            span = max(0.0, now - float(prev_ts))
        tot_d = sum(d for _, d in tr_deltas)
        if tot_d > 0 and MIN_SPAN_SEC <= span <= MAX_SPAN_SEC:
            # 合并同一 tick 内相同 tag 的增量，不同 tag 按 token 占比分摊本段活跃耗时
            merged = {}
            for tag, d in tr_deltas:
                merged[tag] = merged.get(tag, 0) + d
            for tag, d in merged.items():
                add_bucket_sample(buckets, tag, now, d, span * (d / tot_d))
            recorded = True
    elif (
        total_out is not None
        and prev_out is not None
        and total_out > prev_out
        and (not has_transcript or (not st.get("tr_seen_tokens") and not gen_now))
    ):
        # 降级路径：无 transcript_path 时用 total_output_tokens + 纯活跃计时
        span = active_span if active_span >= MIN_SPAN_SEC else pending_span
        if span < MIN_SPAN_SEC and prev_ts is not None:
            span = max(0.0, now - float(prev_ts))
        dout = total_out - prev_out
        tag = orchestrator_planner_tag(model) if is_orch else model_short_tag(model)
        if MIN_SPAN_SEC <= span <= MAX_SPAN_SEC:
            add_bucket_sample(buckets, tag or "M", now, dout, span)
            recorded = True

    if recorded:
        work_sec = 0.0
        work_since = now if gen_now else None
        st.pop("pending_work_sec", None)
        st.pop("pending_ts", None)
    elif agent_state == "idle":
        if work_sec >= MIN_SPAN_SEC:
            st["pending_work_sec"] = work_sec
            st["pending_ts"] = now
        work_sec = 0.0
        work_since = None
    elif gen_now:
        st.pop("pending_work_sec", None)
        st.pop("pending_ts", None)

    # 清理过期桶
    for tag in list(buckets.keys()):
        if bucket_rate(buckets[tag], now) is None:
            buckets.pop(tag, None)

    st["work_sec"] = work_sec
    st["work_since"] = work_since
    st["ts"] = now
    if total_out is not None:
        st["out"] = total_out
    save_state(p, st)

    mode_since = float(st.get("mode_since") or 0.0)
    if is_orch:
        planner = orchestrator_planner_tag(model)
        items = []
        for tag, samples in buckets.items():
            r = bucket_rate(samples, now, min_ts=mode_since)
            if r is not None:
                items.append((tag, r))
        items.sort(key=lambda kv: (0 if kv[0] == planner else (1 if kv[0] in ("H", "S", "F") else 2), kv[0]))
        return True, items

    cur_tag = model_short_tag(model)
    r = bucket_rate(buckets.get(cur_tag), now, min_ts=mode_since)
    if r is None and not has_transcript and len(buckets) == 1:
        only_tag, only_samples = next(iter(buckets.items()))
        r = bucket_rate(only_samples, now, min_ts=mode_since)
        if r is not None:
            cur_tag = only_tag
    return False, [(cur_tag, r)] if r is not None else []


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


def format_rates(is_orch, rates, use_color, compact=False):
    if not rates:
        return None
    if not is_orch:
        _, r = rates[0]
        txt = f"{r:.0f}t/s" if compact else f"~{r:.0f}t/s"
        return color(color_rate(r), txt, use_color)
    chunks = []
    last_idx = len(rates) - 1
    for i, (tag, r) in enumerate(rates):
        if compact:
            txt = f"{tag}{r:.0f}"
        else:
            suffix = "t/s" if i == last_idx else ""
            txt = f"{tag}~{r:.0f}{suffix}"
        chunks.append(color(color_rate(r), txt, use_color))
    slash = color(DIM, "/", use_color)
    return slash.join(chunks)


def model_family_color(model):
    """按模型家族分主色：Orchestrator 蓝紫、Claude 暖陶橙、GPT 墨绿、Flash/Hydrogen 青蓝、Pro/Neon 靛紫。"""
    m = model or {}
    text = f"{m.get('id') or ''} {m.get('display_name') or ''}".lower()
    if "orchestrator" in text:
        return "38;5;61"
    if "claude" in text:
        return "38;5;130"
    if "gpt" in text or "openai" in text or re.search(r"(?:^|[-_\s])o[134](?:$|[-_\s])", text):
        return "38;5;22"
    if "flash" in text or "hydrogen" in text:
        return "38;5;31"
    if any(k in text for k in ("neon", "pro", "ultra", "carbon", "argon", "gemini")):
        return "38;5;55"
    return "38;5;24"


def _model_tier_code(model, base_code, tier):
    if is_orchestrator_model(model):
        if "neon" in tier:
            return "1;38;5;55"
        if "carbon" in tier:
            return "1;38;5;125"
        if "argon" in tier:
            return "1;38;5;25"
        if "opus" in tier:
            return "1;38;5;130"
        return f"1;{base_code}"
    if any(k in tier for k in ("xhigh", "max", "ultra")):
        return "1;38;5;125"
    if any(k in tier for k in ("high", "thinking")):
        return f"1;{base_code}"
    if "med" in tier:
        return "38;5;61"
    if any(k in tier for k in ("low", "min", "fast")):
        return DIM
    return base_code


def _shorten_base_for_narrow(base):
    low = base.lower().strip()
    if "orchestrator" in low:
        return "Orch"
    if "sonnet" in low:
        return "Sonnet"
    if "opus" in low:
        return "Opus"
    if "haiku" in low:
        return "Haiku"
    if "gpt" in low:
        return "GPT"
    if "hydrogen" in low:
        return "Hyd"
    if "flash" in low and "lite" in low:
        return "FlashL"
    if "flash" in low:
        return "Flash"
    return base.strip()


def _shorten_tier_for_narrow(model, tier):
    if is_orchestrator_model(model):
        for k, v in (("neon", "N"), ("carbon", "C"), ("argon", "A"), ("opus", "O")):
            if k in tier:
                return f"({v})"
        return f"({tier[:1].upper()})" if tier else ""
    if any(k in tier for k in ("xhigh", "extra")):
        return "(XH)"
    if "max" in tier:
        return "(Max)"
    if "high" in tier:
        return "(H)"
    if "thinking" in tier:
        return "(T)"
    if "med" in tier:
        return "(M)"
    if "min" in tier:
        return "(Min)"
    if "low" in tier:
        return "(L)"
    return f"({tier[:2].upper()})" if tier else ""


def format_model(model, use_color, compact=False):
    label = shorten_model(model)
    base_code = model_family_color(model)
    m = re.match(r"^(.+?)(\s*\([^)]+\))\s*$", label)
    if not m:
        txt = _shorten_base_for_narrow(label) if compact else label
        return color(base_code, txt, use_color)
    base, tier_raw = m.group(1), m.group(2)
    tier = tier_raw.strip(" ()").lower()
    tier_code = _model_tier_code(model, base_code, tier)
    if compact:
        base = _shorten_base_for_narrow(base)
        tier_raw = _shorten_tier_for_narrow(model, tier)
    return color(base_code, base, use_color) + color(tier_code, tier_raw, use_color)


def _window_label_compact(key):
    lbl = window_label(key)
    return "wk" if lbl == "week" else lbl


def _format_quota_merged(qitems, use_color):
    if not qitems:
        return None
    if len(qitems) == 1:
        k, p = qitems[0]
        return color(color_quota(k, p), f"{p:.0f}%", use_color)
    (k1, p1), (k2, p2) = qitems[0], qitems[1]
    return (
        color(color_quota(k1, p1), f"{p1:.0f}", use_color)
        + color(DIM, "/", use_color)
        + color(color_quota(k2, p2), f"{p2:.0f}%", use_color)
    )


def build(payload):
    use_color = not os.environ.get("NO_COLOR")
    sep_full = f"\x1b[{DIM}m · \x1b[0m" if use_color else " · "
    sep_tight = " "

    # ---- 收集原始指标数据 ----
    cwd_raw = (payload.get("workspace") or {}).get("current_dir") or payload.get("cwd") or ""
    model = payload.get("model")
    model_id = (model or {}).get("id", "")

    # Quota：短窗口 + 周窗口
    qitems = []
    qkey, qb = pick_quota(payload.get("quota"), model_id)
    if qb is not None:
        for key, bucket in quota_windows(payload.get("quota"), qkey):
            try:
                pct = max(0.0, min(1.0, float(bucket.get("remaining_fraction")))) * 100
                qitems.append((key, pct))
            except (TypeError, ValueError):
                continue

    # 上下文
    cw = payload.get("context_window") or {}
    ctx_info = None
    try:
        size = float(cw.get("context_window_size") or 0)
        pct = float(cw.get("used_percentage") or 0)
        if size > 0:
            ctx_info = (pct / 100.0 * size, size, pct)
    except (TypeError, ValueError):
        ctx_info = None

    # 缓存命中率（最近一轮）
    cu = cw.get("current_usage") or {}
    cache_hit = None
    try:
        cr = float(cu.get("cache_read_input_tokens") or 0)
        it = float(cu.get("input_tokens") or 0)
        cc = float(cu.get("cache_creation_input_tokens") or 0)
        denom = cr + it + cc
        if denom > 0:
            cache_hit = cr / denom * 100
    except (TypeError, ValueError):
        cache_hit = None

    # 输出速度（按底层真实模型分桶估算，排除 tool_use 耗时）
    is_orch, rates = False, []
    try:
        is_orch, rates = update_rates(payload)
    except Exception:
        pass

    # ---- 按档位构造片段 ----
    cwd_full = color(CWD_COLOR, fmt_cwd(cwd_raw), use_color)
    cwd_short = color(CWD_COLOR, fmt_cwd_short(cwd_raw), use_color)

    q_full = [color(color_quota(k, p), f"{p:.0f}% {window_label(k)}", use_color) for k, p in qitems]
    q_compact = [color(color_quota(k, p), f"{p:.0f}% {_window_label_compact(k)}", use_color) for k, p in qitems]
    q_merged = _format_quota_merged(qitems, use_color)
    q_short_only = color(color_quota(qitems[0][0], qitems[0][1]), f"{qitems[0][1]:.0f}%", use_color) if qitems else None

    if ctx_info is not None:
        used, size, pct = ctx_info
        c_code = color_ctx(pct)
        ctx_full = color(c_code, f"ctx {fmt_tokens(used)}/{fmt_tokens(size)}", use_color)
        ctx_compact = color(c_code, f"ctx {fmt_tokens_tight(used)}/{fmt_tokens_tight(size)}", use_color)
        ctx_short = color(c_code, f"{fmt_tokens_tight(used)}/{fmt_tokens_tight(size)}", use_color)
        ctx_pct = color(c_code, f"{pct:.0f}%", use_color)
    else:
        ctx_full = color(DIM, "ctx --", use_color)
        ctx_compact = ctx_full
        ctx_short = color(DIM, "ctx--", use_color)
        ctx_pct = color(DIM, "--", use_color)

    if cache_hit is not None:
        h_code = color_cache(cache_hit)
        cache_full = color(h_code, f"cache {cache_hit:.0f}%", use_color)
        cache_short = color(h_code, f"c{cache_hit:.0f}%", use_color)
    else:
        cache_full = color(DIM, "cache --", use_color)
        cache_short = color(DIM, "c--", use_color)

    rate_full = format_rates(is_orch, rates, use_color, compact=False)
    rate_short = format_rates(is_orch, rates, use_color, compact=True)
    model_full = format_model(model, use_color, compact=False)
    model_short = format_model(model, use_color, compact=True)

    def join_nonempty(sep, seq):
        return sep.join(x for x in seq if x)

    # 档位 1：完整版（宽屏或未提供 terminal_width 时默认）
    line_full = join_nonempty(sep_full, [cwd_full, *q_full, ctx_full, cache_full, rate_full, model_full])

    try:
        term_w = int(payload.get("terminal_width") or 0)
    except (TypeError, ValueError):
        term_w = 0
    if term_w <= 0:
        return line_full

    avail = max(8, term_w - WIDTH_MARGIN)
    if visible_width(line_full) <= avail:
        return line_full

    # 逐级降级：compact → short → nano（依次丢 cache → cwd → t/s → week）→ 极窄兜底
    candidates = [
        join_nonempty(sep_tight, [cwd_short, *q_compact, ctx_compact, cache_short, rate_full, model_full]),
        join_nonempty(sep_tight, [cwd_short, q_merged, ctx_short, cache_short, rate_short, model_short]),
        join_nonempty(sep_tight, [cwd_short, q_merged, ctx_short, rate_short, model_short]),
        join_nonempty(sep_tight, [q_merged, ctx_short, rate_short, model_short]),
        join_nonempty(sep_tight, [q_merged, ctx_short, model_short]),
        join_nonempty(sep_tight, [q_short_only, ctx_pct, model_short]),
        join_nonempty(sep_tight, [q_short_only, model_short]),
        model_short,
    ]
    for cand in candidates:
        if cand and visible_width(cand) <= avail:
            return cand
    return truncate_ansi(candidates[-1], avail)


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
