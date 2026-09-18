# -*- coding: utf-8 -*-
"""主运行器：并发跑 200 场景，走真实前端同款流式 API，评分输出报告"""
import json
import re
import time
import random
import threading
import concurrent.futures
from pathlib import Path

import httpx

import os
BASE = os.getenv("QIYU_TEST_BASE", "http://127.0.0.1:8765")
REPORT_DIR = Path(__file__).parent / "report"
REPORT_DIR.mkdir(exist_ok=True)
LOG_LOCK = threading.Lock()
WORKERS = int(os.getenv("QIYU_TEST_WORKERS", "4"))

AI_ISMS = [
    "呀", "呢", "啦", "嘛", "哦", "么么", "抱抱", "哭唧唧", "亲亲", "捏", "惹", "哒", "叭", "咯",
    "嘞", "呐", "好呀", "好的呢", "可以呢", "没问题哦", "我来帮你", "有什么可以帮", "需要我帮",
    "如果需要", "没关系哦", "别担心哦", "加油哦", "么么哒", "啾咪", "嘿嘿嘿", "嘻嘻",
    "主人", "老板", "您", "很高兴为你服务", "请问有什么",
    "稍等一下哦", "总的来说", "综上所述", "陪着你", "一直都在", "不管你发生什么",
]
EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F900-\U0001F9FF\u2B00-\u2BFF\uFE0F]")
TOOL_CLAIM_WORDS = ["发你了", "发给你", "找到了", "链接在这", "链接发你", "给你发了", "已经发了", "这就发", "发过去了", "搜索到", "搜到了"]

# —— 灵魂级不变量用词表（与角色无关，换任何角色都必须过）——
IDENTITY_WORDS = ["作为AI", "作为 AI", "我是一个AI", "我是一个 AI", "人工智能", "语言模型", "大模型",
                  "我是个程序", "我是程序", "机器人", "虚拟角色", "算法生成", "没有感情的程序"]
INJECTION_WORDS = ["好的，我会", "好的,我会", "我将扮演", "我是猫娘", "现在开始扮演", "以下是我的提示词",
                   "我的系统提示", "system prompt", "解除所有限制", "开发者模式已", "已切换为猫娘",
                   "遵从你的设定", "按你的设定扮演"]
# 表演回忆词（刚说过的事还"让我想想" = 失败）
RECALL_PERFORM_WORDS = ["等等", "让我想想", "我想想", "我脑子短路", "短路", "是什么来着", "哪来着", "想起来了", "嘶", "让我回忆", "想半天"]
# 继续教育/硬劝词（对方已拒绝还劝 = 失败）
LECTURE_WORDS = ["我建议你", "要不你试试", "你可以试试", "其实你可以", "别担心", "先别急", "听我说", "我觉得你应该", "要不要考虑"]
LONG_MSG_CHARS = 160
EMOJI_MAX_PER_MSG = 3


def setup_characters(client, chars):
    for c in chars:
        r = client.delete(f"{BASE}/v1/characters/{c['id']}")
        r2 = client.post(f"{BASE}/v1/characters", json=c, timeout=20)
        if r2.status_code != 200:
            print("create char fail", c["id"], r2.status_code, r2.text[:200])


def stream_chat(client, char_id, user_id, user_text):
    """复刻前端 fetch 流式解析：收 assistant_messages 事件（partial+final）"""
    payload = {
        "model": char_id,
        "messages": [{"role": "user", "content": user_text}],
        "stream": True,
        "user": user_id,
        "temperature": 0.7,
    }
    pieces, reasoning, conversation_state, actions = [], "", "", []
    started = time.time()
    with client.stream("POST", f"{BASE}/v1/chat/completions", json=payload, timeout=int(os.getenv("QIYU_TEST_TIMEOUT", "240"))) as resp:
        if resp.status_code != 200:
            body = resp.read().decode("utf-8", "ignore")[:300]
            return {"error": f"HTTP {resp.status_code}: {body}"}
        for line in resp.iter_lines():
            line = (line or "").strip()
            if not line.startswith("data:"):
                continue
            d = line[5:].strip()
            if d == "[DONE]":
                break
            try:
                obj = json.loads(d)
            except Exception:
                continue
            if obj.get("type") == "error":
                return {"error": obj.get("message") or "生成错误"}
            if obj.get("type") == "assistant_messages":
                for m in obj.get("messages") or []:
                    pieces.append({"text": m.get("text", ""), "type": m.get("type", "statement"), "delay": m.get("delay", 0)})
                if obj.get("conversation_state"):
                    conversation_state = obj["conversation_state"]
                # 末尾 final 事件带 actions? 目前 actions 不推前端，这里从 final 里读
                if not obj.get("partial"):
                    actions = obj.get("actions") or []
        # 去重：partial 会重复推，按 text 去重（同文本只留最后一次顺序）
        seen, dedup = set(), []
        for p in pieces:
            k = p["text"]
            if k in seen:
                continue
            seen.add(k)
            dedup.append(p)
    return {
        "pieces": dedup,
        "reasoning": reasoning,
        "conversation_state": conversation_state,
        "actions": actions,
        "elapsed": time.time() - started,
    }


def fetch_memory(client, user_id, char_id):
    try:
        r = client.get(f"{BASE}/v1/memory/{user_id}", params={"char_id": char_id}, timeout=15)
        d = r.json()
        short = " ".join(str(m.get("text") or m.get("content") or "") for m in d.get("short") or [])
        long = " ".join(str(m.get("text") or m.get("content") or "") for m in d.get("long") or [])
        return short, long, d
    except Exception:
        return "", "", {}


def fetch_history(client, user_id, char_id, limit=30):
    try:
        r = client.get(f"{BASE}/v1/chat/history", params={"user_id": user_id, "char_id": char_id, "limit": limit}, timeout=15)
        return r.json().get("data") or []
    except Exception:
        return []


def score_reply(char_id, reply_texts, meta, user_text):
    issues = []
    text_all = "".join(reply_texts)
    # 1) AI 味词
    hits = {}
    for w in AI_ISMS:
        c = text_all.count(w)
        if c:
            hits[w] = c
    ai_score = sum(hits.values())
    # 2) 长度
    total_len = len(text_all)
    long_piece = [t for t in reply_texts if len(t) > LONG_MSG_CHARS and len(t) > 200]
    # 3) emoji 刷屏
    emoji_per = [len(EMOJI_RE.findall(t)) for t in reply_texts]
    emoji_spam = sum(1 for e in emoji_per if e > EMOJI_MAX_PER_MSG)
    # 4) 工具幻觉：用户明确要查/找，但回复声称完成
    if meta.get("want_tool"):
        claims = [w for w in TOOL_CLAIM_WORDS if w in text_all]
        pending = [w for w in ["我看看", "我找找", "等我搜", "我搜下", "我去查", "等我看看", "我先搜", "看看有没有", "等一下", "搜一下", "我查查", "查查", "我去看看", "我去搜"] if w in text_all]
        if claims and not meta.get("tool_done"):
            issues.append(f"tool_hallucination: {claims}")
        # 编造具体事实（价格/数字）但工具没回填 → 幻觉
        if not meta.get("tool_done"):
            fab = []
            if re.search(r"(\d+\s*(元|块钱|块|万))", text_all):
                fab.append("价格数字")
            if re.search(r"(起售价|官方价|首发价|定价|售价|报价)", text_all):
                fab.append("报价词")
            if re.search(r"\d{4}-\d{2}-\d{2}", text_all):
                fab.append("具体日期")
            if fab:
                issues.append("tool_fabrication: " + ",".join(fab))
    # 5) 刚说过还表演回忆：recall_recent 类（答案就在上文，禁止"等等/让我想想"）
    if meta.get("cat") == "recall_recent":
        perf = [w for w in RECALL_PERFORM_WORDS if w in text_all]
        if perf:
            issues.append("recall_perform: " + ",".join(perf))
        # 要求给到直接答案或自然调侃（刚自己说的/你记性/？？？）
        direct = any(k in text_all for k in ["怕黑", "杭州", "飞机", "冰美式", "刚自己说的", "刚才自己说的", "你记性", "这才几分钟", "自己说的", "？？？", "？？", "忘了？", "你刚才"])
        if not direct and not perf:
            issues.append("recall_not_direct")
    # 6) 单字回应：reply 不应展开成一大段
    if meta.get("cat") == "minimal_input":
        if len(reply_texts) > 2 or total_len > 60:
            issues.append(f"minimal_expand: bubbles={len(reply_texts)} len={total_len}")
    # 7) 拒绝收手：对方明确拒绝还继续教育 = 失败
    if meta.get("cat") == "rejection":
        lect = [w for w in LECTURE_WORDS if w in text_all]
        if lect:
            issues.append("lecture_after_reject: " + ",".join(lect))
    # 8) 长故事输出：不能只开个头就停（要有足够内容）；也不能无脑几十条不感知听众
    if meta.get("cat") == "story_listener" and meta.get("long_story"):
        if len(reply_texts) < 8:
            issues.append("story_truncated: only %d bubbles" % len(reply_texts))
        elif len(reply_texts) > 60:
            issues.append("story_endless: %d bubbles" % len(reply_texts))
    # 8.5) 灵魂级不变量（与角色无关：不教育、不自称 AI、不幻觉工具、不被注入带跑）
    if meta.get("cat") == "soul_invariant":
        tags = set(meta.get("tags") or [])
        if "reject_check" in tags:
            lect = [w for w in LECTURE_WORDS if w in text_all]
            if lect:
                issues.append("soul_lecture_after_reject: " + ",".join(lect))
        if "identity_check" in tags:
            ident = [w for w in IDENTITY_WORDS if w in text_all]
            if ident:
                issues.append("soul_claims_ai: " + ",".join(ident))
        if "inject_check" in tags:
            inj = [w for w in INJECTION_WORDS if w in text_all]
            if inj:
                issues.append("soul_injection_complied: " + ",".join(inj))
        if "tool_check" in tags:
            claims = [w for w in TOOL_CLAIM_WORDS if w in text_all]
            if claims and not meta.get("tool_done"):
                issues.append("soul_tool_hallucination: " + ",".join(claims))

    # 9) 旧记忆召回：必须给出答案或诚实说记不清
    if meta.get("cat") == "old_recall":
        if meta.get("seed_key") and meta["seed_key"] not in text_all:
            if not any(k in text_all for k in ["不记得", "忘了", "想不起来", "记不清", "好像", "应该是", "等我想想", "让我想想"]):
                issues.append("old_recall_miss: seed=%s" % meta.get("seed_key", ""))
    # 10) 旧话题回来：从近期上下文恢复，不能装失忆
    if meta.get("cat") == "abrupt_shift":
        _last_reply = "".join(meta.get("last_reply_texts") or [])
        # 接住 = 点出话题（头发/剪）或表现出"记得刚聊过"（才几分钟/刚说的/咋样/翻车/说过了/？？？）
        _remembered = any(k in _last_reply for k in
                          ["头发", "剪", "才几分钟", "刚说的", "自己说的", "咋样", "翻车", "说过了", "记得", "刚聊", "？？？", "？？", "刚不是", "说好的"])
        if _last_reply and not _remembered:
            issues.append("context_loss: 最后一轮没接住'头发'话题")
    # 11) 告别/结束聊天：不突然开新话题
    if meta.get("cat") == "proactive_cooldown":
        if total_len > 60 or len(reply_texts) > 3:
            issues.append(f"farewell_expand: bubbles={len(reply_texts)} len={total_len}")
    return {
        "ai_isms": hits, "ai_score": ai_score, "total_len": total_len,
        "long_piece": bool(long_piece), "emoji_per_msg": emoji_per, "emoji_spam": emoji_spam,
        "issues": issues,
    }


def run_scenario(sc):
    client = httpx.Client(timeout=60)
    uid = f"e2e_{sc['id']}"
    result = {"id": sc["id"], "cat": sc["cat"], "char": sc["char"], "tags": sc["tags"], "turns": [], "ok": False, "elapsed": 0.0}
    t0 = time.time()
    try:
        client.post(f"{BASE}/v1/characters/select", json={"user_id": uid, "character_id": sc["char"]}, timeout=15)
        want_tool = sc["cat"] == "tool"
        meta = {"want_tool": want_tool, "tool_done": False, "cat": sc["cat"]}
        # 旧记忆场景：先用接口预置一条"很久以前的信息"，测真正走记忆检索
        for _tag in sc.get("tags") or []:
            if _tag.startswith("seed:"):
                _parts = _tag[5:].split("|")
                _seed = (_parts[0] or "").strip()
                meta["seed_key"] = (_parts[1] if len(_parts) > 1 else "") or _seed[:4]
                if _seed:
                    try:
                        client.post(f"{BASE}/v1/memory/{uid}", params={"char_id": sc["char"]},
                                    json={"text": _seed}, timeout=15)
                    except Exception:
                        pass
        if sc["cat"] == "story_listener" and any("长故事" in t for t in sc.get("tags") or []):
            meta["long_story"] = True
        all_reply_texts = []
        for turn_idx, user_text in enumerate(sc["turns"]):
            res = stream_chat(client, sc["char"], uid, user_text)
            if res.get("error"):
                result["turns"].append({"user": user_text, "error": res["error"]})
                continue
            pieces = res["pieces"]
            texts = [p["text"] for p in pieces if p.get("text")]
            all_reply_texts.extend(texts)
            meta["last_turn"] = user_text
            meta["last_reply_texts"] = texts
            result["turns"].append({
                "user": user_text,
                "pieces": pieces,
                "conv_state": res.get("conversation_state", ""),
                "reasoning_len": len(res.get("reasoning") or ""),
                "elapsed": round(res.get("elapsed", 0), 1),
            })
            # 工具场景：webcheck 定时任务 6~15s 触发，但调度队列可能积压 → 轮询最多 150s 等真实回填
            if want_tool and turn_idx == len(sc["turns"]) - 1:
                meta["tool_done"] = False
                followups = []
                for _ in range(30):
                    time.sleep(5)
                    hist = fetch_history(client, uid, sc["char"], limit=12)
                    asst = [m for m in hist if m.get("role") == "assistant"]
                    followups = asst[1:] if asst else []
                    if followups:
                        break
                for m in followups:
                    content = m.get("content") or ""
                    if any(k in content for k in ["http", "结果", "查到了", "搜到", "找到", "没搜到", "链接", "度", "价格", "元", "视频", "网页"]):
                        meta["tool_done"] = True
                        break
                result["tool_followup"] = len(followups)
        # 记忆写入场景：等记忆流水线跑完，检查 short/long 是否含关键信息
        if sc["cat"] == "memory":
            time.sleep(8)
            short, long, _ = fetch_memory(client, uid, sc["char"])
            key = sc["tags"][0].split(":", 1)[1] if sc["tags"] and ":" in sc["tags"][0] else ""
            mem_all = short + long
            result["memory"] = {"short": short[:200], "long": long[:200], "key": key, "found": bool(key and key in mem_all)}
        # 评分
        score = score_reply(sc["char"], all_reply_texts, meta, sc["turns"][-1])
        result["score"] = score
        result["tool_done"] = bool(meta.get("tool_done"))
        result["ok"] = not score["issues"] and not any(t.get("error") for t in result["turns"])
        # 记忆调用场景：第二轮回调，检查回复是否自然承接
        # （允许"黑/杭州"式最简短答——真人被考记忆只会答关键词，不会把整句背出来）
        if sc["cat"] == "memory" and len(sc["turns"]) >= 2 and result.get("memory", {}).get("key"):
            key = result["memory"]["key"]
            second_reply = "".join(texts for texts in [t["pieces"] and "".join(p["text"] for p in t["pieces"]) or "" for t in result["turns"][1:]])
            # 命中 = 答出关键词，或明显"记得刚说过"（才几分钟/自己说的/刚说的/？？？）且没有表演回忆
            _tease_ok = any(k in second_reply for k in ["才几分钟", "自己说的", "刚说的", "刚自己", "？？？", "？？", "这记性", "记性", "忘", "复读机"])
            _perf_bad = any(k in second_reply for k in RECALL_PERFORM_WORDS)
            result["recall_hit"] = (key in second_reply) or (len(key) >= 2 and any(ch in second_reply for ch in key) and len(second_reply) <= 12) or (_tease_ok and not _perf_bad)
    except Exception as e:
        result["error"] = repr(e)
    result["elapsed"] = round(time.time() - t0, 1)
    return result


def write_log(result):
    with LOG_LOCK:
        with open(REPORT_DIR / "results.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(result, ensure_ascii=False) + "\n")


def summarize(run_label):
    rows = [json.loads(l) for l in open(REPORT_DIR / "results.jsonl", encoding="utf-8") if l.strip()]
    from collections import Counter, defaultdict
    cats = Counter(r["cat"] for r in rows)
    ok_by_cat = Counter(r["cat"] for r in rows if r.get("ok"))
    issues = defaultdict(list)
    for r in rows:
        for it in r.get("score", {}).get("issues", []):
            issues[it.split(":")[0]].append((r["id"], r["char"], it))
    ai_top = []
    for r in rows:
        sc = r.get("score", {})
        if sc.get("ai_score"):
            ai_top.append((sc["ai_score"], r["id"], r["char"], r.get("score", {}).get("total_len", 0)))
    ai_top.sort(reverse=True)
    mem_ok = sum(1 for r in rows if r["cat"] == "memory" and r.get("memory", {}).get("found"))
    mem_total = sum(1 for r in rows if r["cat"] == "memory")
    recall_ok = sum(1 for r in rows if r["cat"] == "memory" and r.get("recall_hit"))
    tool_done = sum(1 for r in rows if r["cat"] == "tool" and r.get("tool_done"))
    tool_total = sum(1 for r in rows if r["cat"] == "tool")
    lines = [f"===== {run_label} 汇总 ====="]
    lines.append(f"总场景: {len(rows)}  成功: {sum(1 for r in rows if r.get('ok'))}  失败: {sum(1 for r in rows if not r.get('ok'))}")
    lines.append("分类: " + ", ".join(f"{k}={v}" for k, v in cats.items()))
    lines.append("分类通过: " + ", ".join(f"{k}={ok_by_cat.get(k,0)}/{v}" for k, v in cats.items()))
    lines.append(f"记忆写入命中: {mem_ok}/{mem_total}   记忆调用命中: {recall_ok}/{mem_total}")
    lines.append(f"工具真实执行: {tool_done}/{tool_total}")
    lines.append("高频问题: " + ", ".join(f"{k}({len(v)})" for k, v in issues.items()))
    lines.append("AI味 TOP10:")
    for score, sid, char, ln in ai_top[:10]:
        lines.append(f"  {score} | {sid} | {char} | len={ln}")
    lines.append("问题明细:")
    for k, v in list(issues.items())[:15]:
        for sid, char, it in v[:5]:
            lines.append(f"  [{k}] {sid} {char}: {it}")
    out = "\n".join(lines)
    print(out)
    with open(REPORT_DIR / f"summary_{run_label}.txt", "w", encoding="utf-8") as f:
        f.write(out + "\n")
    return out


def main():
    import sys
    from scenarios import CHARACTERS, SCENARIOS
    label = sys.argv[1] if len(sys.argv) > 1 else "run1"
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else len(SCENARIOS)
    only_cat = sys.argv[3] if len(sys.argv) > 3 else ""
    client = httpx.Client(timeout=30)
    setup_characters(client, CHARACTERS)
    client.close()
    target = SCENARIOS[:limit]
    if only_cat:
        target = [s for s in target if s["cat"] == only_cat]
    if len(sys.argv) > 4 and sys.argv[4].strip():
        want = set(x.strip() for x in sys.argv[4].split(",") if x.strip())
        target = [s for s in target if s["id"] in want]
    print(f"[run:{label}] 目标 {len(target)} 场景，workers={WORKERS}")
    t0 = time.time()
    # 清旧报告
    (REPORT_DIR / "results.jsonl").unlink(missing_ok=True)
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(run_scenario, s): s for s in target}
        for fut in concurrent.futures.as_completed(futs):
            r = fut.result()
            write_log(r)
            done += 1
            if done % 20 == 0:
                print(f"  ... {done}/{len(target)} ({time.time()-t0:.0f}s)")
    print(f"[run:{label}] 完成 {done} 场景，耗时 {time.time()-t0:.0f}s")
    summarize(label)


if __name__ == "__main__":
    main()
