# -*- coding: utf-8 -*-
"""标注 AI 客户端（OpenAI 兼容 chat 接口，供 annotate_pack.py --自动回填 使用）。

三要素配置：base_url / api_key / model，火山方舟、DeepSeek 等 OpenAI 兼容服务通用。
读取顺序：环境变量 ANNOTATE_API_KEY 优先于 路径配置.json api.annotate_api 的 api_key；
base_url 与 model 只从 路径配置.json 读取（控制台设置页维护）。

退出码：0=成功；1=失败/未配置。
红线：路径经 common.load_config()；UTF-8；日志/报告脱敏，不打印完整 api_key。
"""

import json
import sys
import time

import httpx

import common

ENV_KEY = "ANNOTATE_API_KEY"
CONFIG_SECTION = "annotate_api"
DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
DEFAULT_MODEL = "doubao-pro-32k"


def mask_key(key: str) -> str:
    if len(key) > 8:
        return f"{key[:4]}…{key[-2:]}"
    return "已设置" if key else "未设置"


def get_annotate_config() -> dict:
    """返回 {base_url, api_key, model, key来源, 附加参数, 并发}。未配置时 api_key 为空串。

    附加参数（#35 M4）：路径配置.json api.annotate_api.附加参数（dict），原样合并进请求体——
    供 DeepSeek 关思考（{"thinking": {"type": "disabled"}}）等服务端专有参数透传用，缺省 {}。
    并发（#35 M2）：api.annotate_api.并发（int），auto_fill 取结果并发路数，缺省 8。"""
    cfg = {}
    try:
        cfg = common.load_config().get("api", {}).get(CONFIG_SECTION, {}) or {}
    except Exception:
        cfg = {}
    import os
    env_key = os.environ.get(ENV_KEY, "").strip()
    api_key = env_key or str(cfg.get("api_key", "") or "").strip()
    extra = cfg.get("附加参数")
    try:
        conc = int(cfg.get("并发", 8))
    except (TypeError, ValueError):
        conc = 8
    return {
        "base_url": str(cfg.get("base_url", "") or "").strip() or DEFAULT_BASE_URL,
        "api_key": api_key,
        "model": str(cfg.get("model", "") or "").strip() or DEFAULT_MODEL,
        "key来源": "环境变量" if env_key else ("路径配置.json" if api_key else ""),
        "附加参数": extra if isinstance(extra, dict) else {},
        "并发": max(1, conc),
    }


def is_configured() -> bool:
    return bool(get_annotate_config()["api_key"])


def chat(system: str, user: str, *, max_tokens: int = 16000, temperature: float = 0.2,
         allow_empty: bool = False) -> str:
    """单轮对话，返回助手文本。失败抛 RuntimeError（人话提示）。

    allow_empty=False（默认）：**正文为空即报错**。推理模型（返回 reasoning_content）在 max_tokens
    不足时会把预算全花在思考链上，导致 content 为空、finish_reason=length；此时必须显式失败，
    否则调用方会把空串当有效结果（静默错误，如空标注/空配置）。allow_empty=True 仅供连通性自检。
    """
    cfg = get_annotate_config()
    if not cfg["api_key"]:
        raise RuntimeError(
            "未配置标注 AI：请到控制台设置页「标注 AI 配置」填写 API Key 并保存"
            f"（或设置环境变量 {ENV_KEY}）")
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"}
    body = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    body.update(cfg["附加参数"])
    resp = None
    wait = 2
    for attempt in range(4):
        try:
            with httpx.Client(timeout=600) as client:
                resp = client.post(url, headers=headers, json=body)
        except httpx.TimeoutException:
            raise RuntimeError("标注 AI 请求超时，请检查网络/代理后重试")
        except httpx.HTTPError as e:
            raise RuntimeError(f"标注 AI 网络请求失败：{e}")
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt < 3:
                time.sleep(wait)
                wait *= 2
                continue
        break
    if resp.status_code == 401:
        raise RuntimeError("标注 AI API Key 无效或已过期，请到设置页重新填写保存")
    if resp.status_code == 404:
        raise RuntimeError(f"标注 AI 接口 404：请检查 Base URL（当前 {cfg['base_url']}）与模型名（{cfg['model']}）")
    if resp.status_code >= 400:
        detail = ""
        try:
            detail = json.dumps(resp.json(), ensure_ascii=False)[:200]
        except Exception:
            detail = resp.text[:200]
        raise RuntimeError(f"标注 AI 返回 HTTP {resp.status_code}：{detail}")
    try:
        data = resp.json()
    except Exception:
        raise RuntimeError("标注 AI 返回结构异常（非 OpenAI 兼容格式）")
    try:
        choice = data["choices"][0]
        content = choice["message"]["content"]
    except Exception:
        raise RuntimeError("标注 AI 返回结构异常（非 OpenAI 兼容格式）")
    content = content or ""
    if not content.strip():
        if allow_empty:
            return ""
        if choice.get("finish_reason") == "length":
            raise RuntimeError(
                "标注 AI 输出被长度截断（finish_reason=length）且正文为空——常见于推理模型："
                "思考链已占满 max_tokens。请调大 max_tokens 或改用非推理模型")
        raise RuntimeError("标注 AI 返回空正文（content 为空）")
    return content


def _selfcheck():
    """自检：①配置存在性 ②最小对话连通（max_tokens=512，几乎不烧额度）。"""
    print("llm_client 自检")
    cfg = get_annotate_config()
    print(f"  Base URL：{cfg['base_url']}")
    print(f"  模型：{cfg['model']}")
    if not cfg["api_key"]:
        print(f"  [FAIL] API Key 未配置（环境变量 {ENV_KEY} 或 设置页保存）")
        print("  自检结论：[FAIL] 请先配置标注 AI 再重试")
        sys.exit(1)
    print(f"  [OK] API Key 已配置：{mask_key(cfg['api_key'])}（{cfg['key来源']}）")
    try:
        out = chat("只回复 ok", "ping", max_tokens=512, allow_empty=True)
        if out.strip():
            print(f"  [OK] 最小对话连通（返回 {len(out)} 字符）")
        else:
            print("  [OK] 最小对话连通（正文为空：模型为推理模型、思考链未在 512 token 内完成，属正常）")
    except RuntimeError as e:
        print(f"  [FAIL] {e}")
        print("  自检结论：[FAIL]")
        sys.exit(1)
    print("  自检结论：[OK]")
    sys.exit(0)


if __name__ == "__main__":
    if common.is_selfcheck():
        _selfcheck()
    else:
        print("用法：python llm_client.py --自检")
        sys.exit(1)