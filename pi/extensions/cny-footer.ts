/**
 * cny-footer — 自定义底部状态栏
 *
 * 第一行：目录 (git 分支) • 会话名
 * 第二行：context │ speed │ hit │ cost            模型 • 思考等级
 *
 * 与内置 footer 的差异：
 *  - 不展示输入 / 输出 token 总量（看 /session 或 /speed all）
 *  - 「speed」写成「平均(当前) t/s · 平均(当前)s」，括号内是最近一条，括号外是会话均值
 *  - 「cost」按 DeepSeek 官方人民币价逐条消息计费（区分高峰 / 空闲，见下）
 *  - 各项用 │ 分隔，并各自带颜色和符号，便于快速扫读
 *
 * 计费说明：
 *   pi 自带的 usage.cost 是用模型目录里的美元单价算的，目录里只有「高峰价」一档，
 *   所以空闲时段会多算一倍。这里改为按每条消息自己的时间戳套官方人民币价：
 *     deepseek-flash  高峰 输入 2 元 / 缓存命中 0.04 元 / 输出 8 元（每百万 token）
 *                     空闲 折半：1 / 0.02 / 4
 *     deepseek-v4-pro 高峰 9 / 0.30 / 27，空闲 4.5 / 0.15 / 13.5
 *   高峰 = 北京时间周一至周五 9:00-12:00、14:00-18:00；其余时间为空闲。
 *   注意：中国法定节假日未做判断，节假日期间会按高峰价多算（少数情况）。
 *   其他厂商的模型回退为 pi 的美元成本 × 汇率。
 *
 * 汇率：默认 7.2，可用环境变量 PI_CNY_RATE 覆盖（仅用于回退计算）。
 *
 * 速度统计：
 *  - 首字延迟 first = 首个流式 delta − 发起 provider 请求（含网络往返 + prefill）
 *  - 生成速度 t/s = usage.output ÷ (message_end − 首个 delta)，纯解码，不含首字
 *  - footer 第二行：context 打头，速度写成「平均(当前)」，按宽度三档降级：
 *      完整  context ▮ 12.3%/128k (auto) │ speed 113.4(77.0) t/s · 1.11(1.40)s │ hit ⚡ 99.8% │ cost ￥0.004 (off-peak)
 *      缩写  ctx 12.3%/128k │ 113(77) t/s · 1.1(1.4)s │ hit 99.8% │ cost ￥0.004
 *      极简  12%/128k │ 113(77) t/s · 1.1(1.4)s │ 99% │ ￥.004
 *    放不下时先去竖线，再不行才硬截断（...）
 *  - /bar [auto|full|short|nano] 手动定起始档（不带参数循环）；auto 根据宽度自己选
 *  - 第一行放不下先丢会话名，再丢分支；第三行放不下按顺序丢整条扩展状态，不切半条
 *  - 右侧模型名要么完整出现，要么不出现，绝不出半截
 *  - /speed 打印完整报告，`/speed all` 追加逐条明细
 *  - 速度样本会以 custom 条目（cny-footer-speed）写进会话文件，/reload、续接会话都会
 *    从里面恢复，不会因为重载而空白；真的没有任何样本时显示 speed -
 *  - 纯解码窗口小于 200ms 时视为 provider 整包缓冲，退化为端到端速度并标 ~
 *  - 数据只在内存里：重启 pi 或切换会话后从零开始；已还原的历史消息不重算
 *  - 设 PI_SPEED_LOG=1 时，每次响应结束往 stderr 打一行测量结果（headless 验收用）
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { truncateToWidth, visibleWidth } from "@earendil-works/pi-tui";
import { homedir } from "node:os";
import { isAbsolute, relative, resolve, sep } from "node:path";

/** 配色：改成任意主题色令牌即可，例如 "accent" / "success" / "mdLink" / "thinkingHigh" */
const COLORS = {
	label: "dim", // labels (input/output/hit/cost/context) + separators
	input: "mdLink", // 蓝：总输入
	output: "success", // 绿：输出
	hit: "accent", // 紫：缓存命中率
	cost: "warning", // 黄：人民币花费
	speed: "accent", // 紫：首字延迟 / 生成速度
	contextOk: "muted",
	contextWarn: "warning",
	contextBad: "error",
	model: "muted",
} as const;

/** 美元 → 人民币汇率，仅用于非 DeepSeek 模型的回退计费，可用 PI_CNY_RATE 覆盖 */
const USD_TO_CNY = (() => {
	const raw = Number(process.env.PI_CNY_RATE);
	return Number.isFinite(raw) && raw > 0 ? raw : 7.2;
})();

/** 单价，单位：元 / 百万 token。cacheWrite 无独立价格，按缓存未命中的输入价计费。 */
type Rate = { input: number; cacheRead: number; output: number };

const DEEPSEEK_RATES: Record<string, { peak: Rate; offPeak: Rate }> = (() => {
	const flash = {
		peak: { input: 2, cacheRead: 0.04, output: 8 },
		offPeak: { input: 1, cacheRead: 0.02, output: 4 },
	};
	const pro = {
		peak: { input: 9, cacheRead: 0.3, output: 27 },
		offPeak: { input: 4.5, cacheRead: 0.15, output: 13.5 },
	};
	return {
		"deepseek-flash": flash,
		"deepseek-v4-flash": flash, // 旧名，仍按 Flash 计费
		"deepseek-v4-flash-vision-exp": flash,
		"deepseek-v4-pro": pro,
	};
})();

/** 北京时间（UTC+8）周一至周五 9:00-12:00、14:00-18:00 为高峰时段 */
function isPeakTime(timestampMs: number): boolean {
	const CN_OFFSET_MS = 8 * 60 * 60 * 1000;
	const d = new Date(timestampMs + CN_OFFSET_MS);
	const day = d.getUTCDay(); // 0 = 周日, 6 = 周六
	if (day === 0 || day === 6) return false;
	const minutes = d.getUTCHours() * 60 + d.getUTCMinutes();
	return (minutes >= 9 * 60 && minutes < 12 * 60) || (minutes >= 14 * 60 && minutes < 18 * 60);
}

type Totals = {
	input: number;
	output: number;
	cacheRead: number;
	cacheWrite: number;
	/** 人民币总花费（已按每条消息的峰谷价分别计算） */
	cny: number;
	/** 其中按高峰价 / 空闲价计得的部分，用于标注总额构成 */
	peakCny: number;
	offPeakCny: number;
	/** 最近一次请求的缓存命中率（%），无缓存活动时为 undefined */
	hitRate?: number;
};

function formatTokens(count: number): string {
	if (count < 1000) return count.toString();
	if (count < 10000) return `${(count / 1000).toFixed(1)}k`;
	if (count < 1000000) return `${Math.round(count / 1000)}k`;
	if (count < 10000000) return `${(count / 1000000).toFixed(1)}M`;
	return `${Math.round(count / 1000000)}M`;
}

function formatCny(cny: number): string {
	return `￥${cny >= 1 ? cny.toFixed(2) : cny.toFixed(3)}`;
}

/** 一次响应的时间线采样 */
type SpeedSample = {
	/** 首个 delta − 发起请求；provider 没暴露「发请求」时退化为 delta − message_start */
	ttftMs?: number;
	/** message_end − 首个 delta，纯解码耗时；窗口太短（provider 整包缓冲）时为 undefined */
	decodeMs?: number;
	/** 发起请求 → 结束的整段耗时，纯解码测不出来时用它兜底 */
	e2eMs?: number;
	outTokens: number;
};

/** 速度样本持久化用的 customType，/reload 或重启后靠它恢复 */
const SPEED_ENTRY = "cny-footer-speed";

/** 解码窗口短于这个值就认为 provider 把整包缓冲了，测不出真实解码速度 */
const MIN_DECODE_MS = 200;

/** footer 第二行档位：auto = 按宽度自动降级；其余为起始档（放不下时继续降） */
type BarMode = "auto" | "full" | "short" | "nano";
const BAR_MODES: BarMode[] = ["auto", "full", "short", "nano"];
const BAR_TIERS = ["full", "short", "nano"] as const;
let barMode: BarMode = "auto";

const SPEED_LOG = !!process.env.PI_SPEED_LOG;

const speedState: {
	/** 发起 provider 请求的时刻 */
	tRequest?: number;
	/** 首个流式 delta 的时刻 */
	tFirstDelta?: number;
	/** 本轮已收到的 delta 数，用于流式中的实时估算 */
	deltas: number;
	last?: SpeedSample;
	samples: SpeedSample[];
} = { deltas: 0, samples: [] };

const nowMs = () => performance.now();

const fmtTps = (tps: number) => `${tps.toFixed(1)} t/s`;

const fmtSeconds = (ms: number) =>
	ms < 1000 ? `${Math.round(ms)}ms` : `${(ms / 1000).toFixed(2)}s`;

/** 优先纯解码速度；provider 整包缓冲时退化为端到端速度（含首字） */
const windowMsOf = (sample: SpeedSample) => sample.decodeMs ?? sample.e2eMs;

const tpsOf = (sample: SpeedSample) => {
	const windowMs = windowMsOf(sample);
	return windowMs === undefined ? undefined : sample.outTokens / (Math.max(1, windowMs) / 1000);
};

/** 会话均值：多条样本才有意义，否则返回 undefined */
function averageSpeed():
	| { ttftMs?: number; tps?: number; approx: boolean; outTokens: number }
	| undefined {
	if (speedState.samples.length < 2) return undefined;
	let ttftSum = 0;
	let ttftCount = 0;
	for (const sample of speedState.samples) {
		if (sample.ttftMs !== undefined) {
			ttftSum += sample.ttftMs;
			ttftCount += 1;
		}
	}

	// 能测出纯解码速度的样本优先；一个都没有时退回端到端样本，并标记 approx
	const decoded = speedState.samples.filter((sample) => sample.decodeMs !== undefined);
	const usable = decoded.length > 0 ? decoded : speedState.samples.filter((sample) => windowMsOf(sample) !== undefined);

	let tps: number | undefined;
	let outSum = 0;
	if (usable.length > 0) {
		const windowSum = usable.reduce((sum, sample) => sum + (windowMsOf(sample) as number), 0);
		const tokenSum = usable.reduce((sum, sample) => sum + sample.outTokens, 0);
		if (windowSum > 0) {
			tps = tokenSum / (windowSum / 1000);
			outSum = tokenSum;
		}
	}
	if (ttftCount === 0 && tps === undefined) return undefined;
	return {
		ttftMs: ttftCount > 0 ? ttftSum / ttftCount : undefined,
		tps,
		approx: decoded.length === 0,
		outTokens: outSum,
	};
}

const resetSpeedTimeline = () => {
	speedState.tRequest = undefined;
	speedState.tFirstDelta = undefined;
	speedState.deltas = 0;
};

/** 把 $HOME 缩成 ~ */
function formatCwdForFooter(cwd: string, home: string | undefined): string {
	if (!home) return cwd;
	const resolvedCwd = resolve(cwd);
	const resolvedHome = resolve(home);
	const rel = relative(resolvedHome, resolvedCwd);
	const inside =
		rel === "" || (rel !== ".." && !rel.startsWith(`..${sep}`) && !isAbsolute(rel));
	if (!inside) return cwd;
	return rel === "" ? "~" : `~${sep}${rel}`;
}

export default function cnyFooter(pi: ExtensionAPI) {
	/** 流式中刷新 footer（节流），由 setFooter 工厂填充 */
	let requestRender: (() => void) | undefined;
	let lastRenderAt = 0;

	// ---------- 速度打点：请求 → 首个 delta → 结束 ----------
	pi.on("before_provider_request", () => {
		speedState.tRequest = nowMs();
		speedState.tFirstDelta = undefined;
		speedState.deltas = 0;
	});

	pi.on("message_start", (event, _ctx) => {
		if (event.message.role !== "assistant") return;
		speedState.tRequest ??= nowMs();
	});

	pi.on("message_update", (event, _ctx) => {
		const type = (event.assistantMessageEvent as { type?: string } | undefined)?.type;
		if (type !== "text_delta" && type !== "thinking_delta" && type !== "toolcall_delta") return;
		speedState.tFirstDelta ??= nowMs();
		speedState.deltas += 1;

		const stamp = nowMs();
		if (stamp - lastRenderAt >= 150) {
			lastRenderAt = stamp;
			requestRender?.();
		}
	});

	pi.on("message_end", (event, _ctx) => {
		const message = event.message;
		if (message.role !== "assistant") return;

		const outTokens = message.usage?.output ?? 0;
		const tFirst = speedState.tFirstDelta;
		const t0 = speedState.tRequest;
		const tEnd = nowMs();
		if (outTokens <= 0 || (tFirst === undefined && t0 === undefined)) {
			resetSpeedTimeline();
			return;
		}

		const decodeMs = tFirst === undefined ? undefined : tEnd - tFirst;
		const sample: SpeedSample = {
			ttftMs: t0 === undefined || tFirst === undefined ? undefined : Math.max(0, tFirst - t0),
			decodeMs: decodeMs !== undefined && decodeMs >= MIN_DECODE_MS ? decodeMs : undefined,
			e2eMs: t0 === undefined ? undefined : Math.max(1, tEnd - t0),
			outTokens,
		};
		if (sample.decodeMs === undefined && sample.e2eMs === undefined) {
			resetSpeedTimeline();
			return;
		}

		speedState.last = sample;
		speedState.samples.push(sample);
		// 持久化：/reload 或重启后 session_start 靠它把速度栏恢复出来
		// （老版本 pi 或 --no-session 时可能没有 appendEntry，不能让它影响统计）
		try {
			pi.appendEntry?.(SPEED_ENTRY, sample);
		} catch {
			/* 写不进去就算了，本轮数值照样显示 */
		}

		if (SPEED_LOG) {
			console.error(
				`[speed] model=${message.model ?? "?"} out=${outTokens} deltas=${speedState.deltas} ttft=${sample.ttftMs === undefined ? -1 : Math.round(sample.ttftMs)}ms decode=${decodeMs === undefined ? -1 : Math.round(decodeMs)}ms e2e=${sample.e2eMs === undefined ? -1 : Math.round(sample.e2eMs)}ms tps=${tpsOf(sample)?.toFixed(2) ?? "-"}`,
			);
		}

		resetSpeedTimeline();
		requestRender?.();
	});

	// ---------- /speed：完整报告 ----------
	pi.registerCommand("speed", {
		description: "报告首字延迟与生成速度：最近一条 + 会话均值（加 all 看逐条明细）",
		handler: async (args, ctx) => {
			const last = speedState.last;
			if (!last) {
				ctx.ui.notify("还没有测量数据：先发一条消息。", "warning");
				return;
			}
			const row = (label: string, ttft: number | undefined, tps: number | undefined, out: number) =>
				`${label}  first ${ttft === undefined ? "-" : fmtSeconds(ttft)}  speed ${tps === undefined ? "-" : fmtTps(tps)}  (${formatTokens(out)} out)`;

			const lines = [row("最近", last.ttftMs, tpsOf(last), last.outTokens)];
			const avg = averageSpeed();
			if (avg) {
				lines.push(`${row("均值", avg.ttftMs, avg.tps, avg.outTokens)}  ${speedState.samples.length} 条`);
			}
			if (/\ball\b/.test(args ?? "")) {
				speedState.samples.forEach((sample, index) => {
					lines.push(
						`#${index + 1}  first ${sample.ttftMs === undefined ? "-" : fmtSeconds(sample.ttftMs)}  ${tpsOf(sample) === undefined ? "-" : fmtTps(tpsOf(sample) as number)}  ${formatTokens(sample.outTokens)} out`,
					);
				});
			}
			ctx.ui.notify(lines.join("\n"), "info");
		},
	});

	// ---------- /bar：切换底部栏档位 ----------
	pi.registerCommand("bar", {
		description: "footer 档位：auto | full | short | nano（不带参数则循环）",
		handler: async (args, ctx) => {
			const want = (args ?? "").trim().toLowerCase();
			barMode = BAR_MODES.includes(want as BarMode)
				? (want as BarMode)
				: BAR_MODES[(BAR_MODES.indexOf(barMode) + 1) % BAR_MODES.length];
			requestRender?.();
			ctx.ui.notify(`footer 档位：${barMode}`, "info");
		},
	});

	pi.on("session_start", async (_event, ctx) => {
		// 从会话文件里恢复速度样本（/reload、续接会话都走这条），没有就保持空
		resetSpeedTimeline();
		speedState.samples = [];
		for (const entry of ctx.sessionManager.getEntries()) {
			if (entry.type !== "custom" || entry.customType !== SPEED_ENTRY) continue;
			const data = entry.data as SpeedSample | undefined;
			if (!data || typeof data.outTokens !== "number") continue;
			speedState.samples.push({
				ttftMs: typeof data.ttftMs === "number" ? data.ttftMs : undefined,
				decodeMs: typeof data.decodeMs === "number" ? data.decodeMs : undefined,
				e2eMs: typeof data.e2eMs === "number" ? data.e2eMs : undefined,
				outTokens: data.outTokens,
			});
		}
		speedState.last = speedState.samples[speedState.samples.length - 1];
		if (ctx.mode !== "tui") return;

		ctx.ui.setFooter((tui, theme, footerData) => {
			const unsubscribe = footerData.onBranchChange(() => tui.requestRender());

			// 自动压缩开关（内置 footer 的 "(auto)" 后缀）
			let autoCompact = true;
			try {
				autoCompact = pi.getSettings()?.compaction?.enabled !== false;
			} catch {
				/* 拿不到设置时按默认开启处理 */
			}

			// 每次渲染都要遍历整个会话，按 (会话/叶子/条目数) 缓存
			let cache:
				| { key: string; totals: Totals }
				| undefined;

			/** 单条 usage 的人民币成本：有官方价就按消息时间判峰谷，否则回退美元 × 汇率 */
			const applyCost = (totals: Totals, usage: any, modelId: string | undefined, timestampMs: number) => {
				const rates = modelId ? DEEPSEEK_RATES[modelId] : undefined;
				if (!rates) {
					totals.cny += (usage.cost?.total ?? 0) * USD_TO_CNY;
					return;
				}
				const peak = isPeakTime(timestampMs);
				const rate = peak ? rates.peak : rates.offPeak;
				const missInput = (usage.input ?? 0) + (usage.cacheWrite ?? 0);
				const cny =
					(missInput * rate.input +
						(usage.cacheRead ?? 0) * rate.cacheRead +
						(usage.output ?? 0) * rate.output) /
					1_000_000;
				totals.cny += cny;
				if (peak) totals.peakCny += cny;
				else totals.offPeakCny += cny;
			};

			const getTotals = (): Totals => {
				const sm = ctx.sessionManager;
				const key = `${sm.getSessionId()}|${sm.getLeafId() ?? ""}|${sm.getEntryCount()}`;
				if (cache && cache.key === key) return cache.totals;

				const totals: Totals = {
					input: 0,
					output: 0,
					cacheRead: 0,
					cacheWrite: 0,
					cny: 0,
					peakCny: 0,
					offPeakCny: 0,
				};
				let hitRate: number | undefined;

				const add = (usage: any, modelId: string | undefined, timestampMs: number) => {
					if (!usage) return;
					totals.input += usage.input ?? 0;
					totals.output += usage.output ?? 0;
					totals.cacheRead += usage.cacheRead ?? 0;
					totals.cacheWrite += usage.cacheWrite ?? 0;
					applyCost(totals, usage, modelId, timestampMs);
				};

				const tsOf = (entry: any): number => {
					const ms = Date.parse(entry.timestamp ?? "");
					return Number.isFinite(ms) ? ms : Date.now();
				};

				for (const entry of sm.getEntries()) {
					if (entry.type === "usage") {
						add(entry.usage, entry.model, tsOf(entry));
					} else if (entry.type === "message" && entry.message.role === "assistant") {
						const usage = entry.message.usage;
						add(usage, entry.message.model, tsOf(entry));
						const promptTokens = usage.input + usage.cacheRead + usage.cacheWrite;
						if (promptTokens > 0) {
							hitRate = (usage.cacheRead / promptTokens) * 100;
						}
					} else if (
						entry.type === "message" &&
						entry.message.role === "toolResult" &&
						entry.message.usage
					) {
						add(entry.message.usage, ctx.model?.id, tsOf(entry));
					} else if (
						(entry.type === "branch_summary" || entry.type === "compaction") &&
						entry.usage
					) {
						add(entry.usage, ctx.model?.id, tsOf(entry));
					}
				}

				totals.hitRate = hitRate;
				cache = { key, totals };
				return totals;
			};

			requestRender = () => tui.requestRender();

			return {
				dispose() {
					unsubscribe();
					requestRender = undefined;
				},
				invalidate() {
					cache = undefined;
				},
				render(width: number): string[] {
					const totals = getTotals();

					// ---------- 第一行：目录 + 分支 + 会话名（放不下先丢会话名，再丢分支） ----------
					const homeDir = process.env.HOME || process.env.USERPROFILE || homedir();
					const cwdText = formatCwdForFooter(ctx.sessionManager.getCwd(), homeDir);
					const branch = footerData.getGitBranch();
					const sessionName = ctx.sessionManager.getSessionName();
					const buildPwdLine = (withBranch: boolean, withSession: boolean) => {
						let text = cwdText;
						if (withBranch && branch) text += ` (${branch})`;
						if (withSession && sessionName) text += ` • ${sessionName}`;
						return theme.fg(COLORS.label, text);
					};
					let pwdLine = buildPwdLine(true, true);
					if (visibleWidth(pwdLine) > width) pwdLine = buildPwdLine(true, false);
					if (visibleWidth(pwdLine) > width) pwdLine = buildPwdLine(false, false);
					pwdLine = truncateToWidth(pwdLine, width, theme.fg(COLORS.label, "..."));

					// ---------- 第二行左侧：ctx 打头，速度写成 平均(当前)（三档） ----------
					type FooterPart = { full: string; short: string; nano: string };
					const parts: FooterPart[] = [];
					const dim = (text: string) => theme.fg(COLORS.label, text);
					const separator = dim(" │ ");

					const usage = ctx.getContextUsage();
					const contextWindow = usage?.contextWindow ?? ctx.model?.contextWindow ?? 0;
					const percent = usage?.percent ?? null;
					const autoIndicator = autoCompact ? " (auto)" : "";
					const windowText = formatTokens(contextWindow);
					const contextText =
						percent === null
							? `?/${windowText}${autoIndicator}`
							: `${percent.toFixed(1)}%/${windowText}${autoIndicator}`;
					const contextColor =
						percent !== null && percent > 90
							? COLORS.contextBad
							: percent !== null && percent > 70
								? COLORS.contextWarn
								: COLORS.contextOk;
					parts.push({
						full: dim("context ") + theme.fg(contextColor, `▮ ${contextText}`),
						short: dim("ctx ") + theme.fg(contextColor, `${percent === null ? "?" : percent.toFixed(1)}%/${windowText}`),
						nano: theme.fg(contextColor, `${percent === null ? "?" : Math.round(percent)}%/${windowText}`),
					});

					// 速度：平均(当前) t/s · 平均(当前)s —— 括号内是最近一条，括号外是会话均值，均值不足两条时只给当前
					const streaming = speedState.tFirstDelta !== undefined && speedState.deltas > 0;
					let currentTps: number | undefined;
					let currentFirstMs: number | undefined;
					let currentApprox = false;
					if (streaming) {
						currentApprox = true;
						const elapsedMs = nowMs() - (speedState.tFirstDelta as number);
						if (elapsedMs >= 400) currentTps = (speedState.deltas / elapsedMs) * 1000;
						if (speedState.tRequest !== undefined) {
							currentFirstMs = (speedState.tFirstDelta as number) - speedState.tRequest;
						}
					} else if (speedState.last) {
						currentTps = tpsOf(speedState.last);
						currentApprox = speedState.last.decodeMs === undefined;
						currentFirstMs = speedState.last.ttftMs;
					}

					const avgSpeed = averageSpeed();
					const pairText = (outer: string | undefined, inner: string | undefined) => {
						if (inner === undefined) return outer;
						return outer === undefined ? inner : `${outer}(${inner})`;
					};
					const tpsDigits = (tps: number | undefined, digits: number) =>
						tps === undefined ? undefined : tps.toFixed(digits);
					const secDigits = (ms: number | undefined, digits: number) =>
						ms === undefined ? undefined : (ms / 1000).toFixed(digits);

					const marked = (text: string | undefined, approx: boolean) =>
						text === undefined || !approx ? text : `~${text}`;

					const fullTps = pairText(marked(tpsDigits(avgSpeed?.tps, 1), avgSpeed?.approx ?? false), marked(tpsDigits(currentTps, 1), currentApprox));
					const fullFirst = pairText(secDigits(avgSpeed?.ttftMs, 2), secDigits(currentFirstMs, 2));
					const shortTps = pairText(marked(tpsDigits(avgSpeed?.tps, 0), avgSpeed?.approx ?? false), marked(tpsDigits(currentTps, 0), currentApprox));
					const shortFirst = pairText(secDigits(avgSpeed?.ttftMs, 1), secDigits(currentFirstMs, 1));

					if (fullTps !== undefined) {
						const full = `${fullTps} t/s` + (fullFirst === undefined ? "" : ` · ${fullFirst}s`);
						const short = `${shortTps} t/s` + (shortFirst === undefined ? "" : ` · ${shortFirst}s`);
						parts.push({
							full: dim("speed ") + theme.fg(COLORS.speed, full),
							short: theme.fg(COLORS.speed, short),
							nano: theme.fg(COLORS.speed, short),
						});
					} else {
						// 还没有任何样本时也占位，别让这一项凭空消失
						parts.push({
							full: dim("speed ") + theme.fg(COLORS.label, "-"),
							short: theme.fg(COLORS.label, "speed -"),
							nano: theme.fg(COLORS.label, "-"),
						});
					}

					if (totals.hitRate !== undefined) {
						const rate = totals.hitRate.toFixed(1);
						parts.push({
							full: dim("hit ") + theme.fg(COLORS.hit, `⚡ ${rate}%`),
							short: dim("hit ") + theme.fg(COLORS.hit, `${rate}%`),
							nano: theme.fg(COLORS.hit, `${Math.floor(totals.hitRate)}%`),
						});
					}
					if (totals.cny > 0) {
						// 标注总额构成：全部高峰 / 全部空闲 / 两者混合
						const hasPeak = totals.peakCny > 0;
						const hasOff = totals.offPeakCny > 0;
						const period =
							hasPeak && hasOff
								? dim(" (mixed)")
								: hasPeak
									? dim(" (peak)")
									: hasOff
										? dim(" (off-peak)")
										: "";
						const cny = formatCny(totals.cny);
						parts.push({
							full: dim("cost ") + theme.fg(COLORS.cost, cny) + period,
							short: dim("cost ") + theme.fg(COLORS.cost, cny),
							nano: theme.fg(COLORS.cost, cny.replace("￥0.", "￥.")),
						});
					}

					const withAvg = (key: "full" | "short" | "nano") => parts.map((part) => part[key]).join(separator);
					// 由富到穷：完整 → 缩写 → 极简 → 去竖线 → 硬截断
					const startTier = barMode === "auto" ? 0 : BAR_TIERS.indexOf(barMode as (typeof BAR_TIERS)[number]);
					const tiers = BAR_TIERS.slice(Math.max(0, startTier));
					const candidates = tiers.map((key) => withAvg(key));
					// 实在窄：去掉分隔竖线，只留空格
					candidates.push(parts.map((part) => part.nano).join(" "));

					// ---------- 第二行右侧：模型 + 思考等级 ----------
					const modelName = ctx.model?.id || "no-model";
					let right = modelName;
					if (ctx.model?.reasoning) {
						const level = ctx.thinkingLevel || "off";
						const token = `thinking${level[0].toUpperCase()}${level.slice(1)}`;
						right =
							level === "off"
								? `${modelName} ${dim("•")} ${theme.fg("thinkingOff", "thinking off")}`
								: `${modelName} ${dim("•")} ${theme.fg(token, level)}`;
					}
					const rightBase = theme.fg(COLORS.model, right);
					const rightWithProvider =
						footerData.getAvailableProviderCount() > 1 && ctx.model
							? theme.fg(COLORS.model, `(${ctx.model.provider}) ${right}`)
							: undefined;

					// ---------- 排布：左侧一段段降级，右侧模型名只整只出现（绝不显半截） ----------
					const minPadding = 2;
					const compose = (left: string, rightStyled: string | undefined): string | undefined => {
						const leftWidth = visibleWidth(left);
						if (leftWidth > width) return undefined;
						if (!rightStyled) return left;
						const rightWidth = visibleWidth(rightStyled);
						if (leftWidth + minPadding + rightWidth > width) return left;
						return left + " ".repeat(width - leftWidth - rightWidth) + rightStyled;
					};

					let statsLine: string | undefined;
					for (const left of candidates) {
						const providerFits =
							rightWithProvider !== undefined &&
							visibleWidth(left) + minPadding + visibleWidth(rightWithProvider) <= width;
						statsLine = compose(left, providerFits ? rightWithProvider : rightBase);
						if (statsLine) break;
					}
					// 连极简都放不下时才硬截断
					statsLine ??= truncateToWidth(candidates[candidates.length - 1], width, "...");

					const lines = [pwdLine, truncateToWidth(statsLine, width, "...")];

					// ---------- 扩展状态行（放不下就丢整条，不切半条） ----------
					const statuses = footerData.getExtensionStatuses();
					if (statuses.size > 0) {
						const texts = Array.from(statuses.entries())
							.sort(([a], [b]) => a.localeCompare(b))
							.map(([, text]) => text.replace(/[\r\n\t]/g, " ").replace(/ +/g, " ").trim())
							.filter(Boolean);
						const kept: string[] = [];
						for (const text of texts) {
							if (visibleWidth([...kept, text].join(" ")) <= width) kept.push(text);
						}
						const statusLine = kept.join(" ");
						if (statusLine) {
							lines.push(truncateToWidth(statusLine, width, theme.fg(COLORS.label, "...")));
						}
					}

					return lines;
				},
			};
		});
	});
}
