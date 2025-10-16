# app.py
# 圖文 QA 編輯器（自建 API 版）
# - 使用 secrets.toml 管理帳密 & API 參數
# - Sidebar 登入 + 進度條（messages 完成比例）+ 溫度 slider (0.1~0.9)
# - 支援 st.logo（預設讀 APP_LOGO 或 ./logo.png）
# - 🎲 只挑 messages 為空的資料
# - 所有按鈕 width='stretch'
# - 存檔：回寫頂層 model / contributor（messages 只保留 role/content）

import os
import json
import base64
import random
import re
from typing import List, Dict, Any, Optional
from collections import Counter  # 統計 contributor

import streamlit as st
from filelock import FileLock

st.set_page_config(
    page_title="Twinkle Gallery", 
    page_icon="🌟",
    layout="wide",  # ✅ (1) main 寬版
    menu_items={
        'Get help': 'https://discord.gg/Cx737yw4ed',
        'About': '本專案是由 Twinkle AI 團隊開發的圖文問答資料集編輯器範例，歡迎加入我們的 [Discord](https://discord.gg/Cx737yw4ed) 交流！',
    }
)

# ------------------------
# Time-based theme (Asia/Taipei) → pick logo
# ------------------------
from datetime import datetime, time
try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except Exception:
    from pytz import timezone as ZoneInfo  # fallback,若環境只有 pytz

def _now_in_taipei():
    try:
        tz = ZoneInfo("Asia/Taipei") if isinstance(ZoneInfo, type) else ZoneInfo("Asia/Taipei")
        return datetime.now(tz)
    except Exception:
        # 萬一系統沒安裝時區資料，就用系統時間
        return datetime.now()

def _is_dark_by_taipei_time(now_dt: Optional[datetime] = None) -> bool:
    """
    規則：台北時間 17:00 (含) ~ 次日 06:00 (不含) 視為 dark，其餘為 light。
    """
    now_dt = now_dt or _now_in_taipei()
    t = now_dt.time()
    return (t >= time(17, 0)) or (t < time(6, 0))

def _pick_image_by_time(light_path: str, dark_path: str) -> str:
    """
    根據台北時間自動選擇圖片版本：
    - 17:00 ~ 06:00 → dark
    - 06:00 ~ 17:00 → light
    若找不到檔案則退回 light_path。
    """
    chosen = dark_path if _is_dark_by_taipei_time() else light_path
    if not os.path.exists(chosen):
        chosen = light_path
    return chosen

# ------------------------
# Secrets & App logo
# ------------------------
def _get_secret(key: str, default=None):
    try:
        if key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    return os.getenv(key, default)

DATA_JSONL = _get_secret("DATA", "data.jsonl")  # 可放檔名或絕對路徑
API_BASE = _get_secret("MY_API_BASE", None)
API_KEY  = _get_secret("OPENAI_API_KEY", None)
MODEL    = _get_secret("MY_MODEL_NAME", "gpt-4o-mini")
SUPPORTS_VISION = str(_get_secret("SUPPORTS_VISION", "true")).lower() in ("1", "true", "yes")
APP_LOGO_LIGHT = _get_secret("APP_LOGO_LIGHT", "static/logo_light.png")  # 可放檔名或 URL
APP_LOGO_DARK  = _get_secret("APP_LOGO_DARK", "static/logo_dark.png")    # 可放檔名或 URL

# 嘗試顯示 logo（若失敗就忽略）
APP_LOGO = _pick_image_by_time(APP_LOGO_LIGHT, APP_LOGO_DARK)
try:
    st.logo(APP_LOGO)
except Exception:
    print(f"無法載入 logo：{APP_LOGO}")

# ------------------------
# API client & helpers
# ------------------------

def _get_client():
    if not API_KEY or not API_BASE:
        return None
    try:
        from openai import OpenAI
        return OpenAI(api_key=API_KEY, base_url=API_BASE)
    except Exception:
        return None

def _data_url(img_path: str) -> Optional[str]:
    if not img_path or not os.path.exists(img_path):
        return None
    with open(img_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    ext = os.path.splitext(img_path)[1].lower()
    mime = {".jpg":"image/jpeg",".jpeg":"image/jpeg",".png":"image/png",".webp":"image/webp"}.get(ext,"image/jpeg")
    return f"data:{mime};base64,{b64}"

def sanitize_model_output(s: str) -> str:
    """移除/重寫可能洩漏來源或提示字眼的語句，保持自然語氣。"""
    if not s:
        return s

    # 1) 直接移除常見前綴（避免破壞句意）
    patterns_remove = [
        r"(?i)\s*作為一個?AI[^\n。]*[。]?",
        r"\s*根據(本張)?圖片[^\n。]*[。]?",
        r"\s*從(這張)?圖片(中)?(可以|能)?看(到|出)[^\n。]*[。]?",
        r"\s*根據(提供的)?文字(內容)?[^\n。]*[。]?",
        r"\s*依(據|照)提示[^\n。]*[。]?",
        r"\s*綜合(以上|上述)(資訊|內容)[^\n。]*[。]?",
        r"\s*就(我|我們)所(知|見)[^\n。]*[。]?",
        r"\s*基於(題示|提供)[^\n。]*[。]?",
    ]
    for pat in patterns_remove:
        s = re.sub(pat, "", s)

    # 2) 溫和重寫一些短語
    replacements = {
        "總結來說，": "",
        "總而言之，": "",
        "整體來看，": "",
        "整體而言，": "",
        "一般而言，": "一般來說，",
        "通常而言，": "通常來說，",
        "我推測": "看起來",
        "我認為": "看來",
        "我猜測": "或許",
        "可以看出": "看來",
        "可以推斷": "多半",
        "看起來像是": "看起來是",
    }
    for k, v in replacements.items():
        s = s.replace(k, v)

    # 3) 清理空白
    s = re.sub(r"\n{3,}", "\n\n", s)
    s = re.sub(r"[ \t]+(\n)", r"\1", s)
    s = s.strip()
    return s

# ------------------------
# Auth（從 secrets 讀使用者）
# ------------------------
def load_users_from_secrets() -> List[Dict[str, Any]]:
    try:
        users = st.secrets.get("users", [])
        if isinstance(users, dict):
            users = [users[k] for k in sorted(users.keys())]
        return list(users)
    except Exception:
        return []

def _bcrypt_available():
    try:
        import bcrypt  # type: ignore
        return True
    except Exception:
        return False

BC_AVAILABLE = _bcrypt_available()

def verify_password(user_record: Dict[str, Any], password: str) -> bool:
    if BC_AVAILABLE and user_record.get("password_hash"):
        import bcrypt  # type: ignore
        try:
            return bcrypt.checkpw(password.encode(), user_record["password_hash"].encode())
        except Exception:
            return False
    elif user_record.get("password") is not None:
        return password == user_record.get("password")
    return False

# ------------------------
# QA 產生：Q（看圖）/ A（只看 text）
# ------------------------
def gen_question_from_image(img_path: str, fallback_text: str, temperature: float) -> Optional[str]:
    # 問題生成的模式與隨機種子（供 UI 顯示）
    mode = "visual"  # 或 "intro"
    q_seed = random.randint(1, 10_000)
    # 50% 機率：改為用 API 產生「導向介紹圖片」的單句問句（更有變化性）
    # 另一半則維持原本「看圖提出可回答的具體問題」
    if random.random() < 0.5:
        mode = "intro"
        client_alt = _get_client()
        if client_alt:
            try:
                # 盲問：不傳圖片、不傳文本線索，避免模型提前帶入背景知識
                sys_alt = (
                    "你是一位提示語句產生器，目標是產生一個能引導對方『介紹這張圖片或談談由來/故事』的單句問句。"
                    "限制：使用繁體中文、自然口語、不可包含引號或前後綴字樣、不要超過30字。"
                    "重點：問題必須通用、開放式，不能包含任何具體地名、數字、人名或推測，不得引用圖片或文字中的內容。"
                )
                user_alt = (
                    "請只輸出一句自然的繁中問句，引導對方介紹或說說這張圖的背景故事。"
                    "務必避免加入任何特定名詞或細節，讓問句具有普適性。"
                )
                alt_messages = [
                    {"role": "system", "content": sys_alt},
                    {"role": "user", "content": user_alt},
                ]
                alt_resp = client_alt.chat.completions.create(
                    model=MODEL,
                    messages=alt_messages,
                    temperature=temperature,
                    seed=q_seed,
                )
                alt_q = (alt_resp.choices[0].message.content or "").strip()
                alt_q = re.sub(r"\s+", " ", alt_q)
                if alt_q:
                    # 將元資料存到 session，供 UI 顯示
                    st.session_state.qa_meta = {
                        "mode": mode, "temperature": temperature, "q_seed": q_seed
                    }
                    return alt_q
            except Exception:
                pass
        # API 失敗備援（仍屬於 intro 模式）
        st.session_state.qa_meta = {"mode": mode, "temperature": temperature, "q_seed": q_seed}
        return "可以簡單介紹一下這張圖片嗎？"
    client = _get_client()
    if client and SUPPORTS_VISION:
        try:
            url = _data_url(img_path)
            if url:
                messages = [
                    {"role": "system", "content": "你是精準的視覺助理。請根據圖片提出『一個』具體可答的問題，避免主觀揣測。以繁體中文。"},
                    {"role": "user", "content": [
                        {"type": "text", "text": "請只輸出問題一句話。"},
                        {"type": "image_url", "image_url": {"url": url}},
                    ]},
                ]
                resp = client.chat.completions.create(
                    model=MODEL,
                    messages=messages,
                    temperature=temperature,
                    seed=q_seed,
                )
                q = (resp.choices[0].message.content or "").strip()
                if q:
                    st.session_state.qa_meta = {"mode": mode, "temperature": temperature, "q_seed": q_seed}
                    return q
        except Exception:
            pass

    filename = os.path.basename(img_path or "") if img_path else ""
    first_lines = (fallback_text or "").splitlines()[0:3]
    hint = " / ".join([h.strip() for h in first_lines if h.strip()])[:200]
    if not hint:
        hint = "圖片中的地點、建物或場景"
    return f"這張圖所呈現的「{filename or '場景'}」中，最具代表性的元素是什麼？"

def gen_answer_from_text(
    only_text: str,
    question: str,
    temperature: float,
    img_path: Optional[str] = None,
    background_prob: float = 1.0,
) -> Optional[str]:
    client = _get_client()
    if not client:
        return "文字未提供相關資訊。"

    url = None
    if SUPPORTS_VISION and img_path:
        try:
            url = _data_url(img_path)
        except Exception:
            url = None

    add_bg = random.random() < max(0.0, min(1.0, background_prob))

    sys = (
        "你是一位自然親切、知識穩健的助理。"
        "請以自然語氣作答，像在與使用者對話，不使用任何標題或固定格式。"
        "回答時不得提及或暗示資訊來源（例如『從圖片可見』『根據文字內容』『依照提示』等），"
        "也不要提到系統、規則、模型或任何技術性詞彙。"
        "先清楚回答問題；若有助理解且允許補充，可自然加入背景脈絡，使用不確定語氣（如『可能、一般來說、或許』），"
        "避免對特定人事時地物做未經證實的斷言。"
        "當你要引入新的地名、人物或主題，而這些資訊並未在問題或文字中明確出現時，"
        "請務必在前面加上自然的承接句，使敘事流暢。"
        "最後可用一句自然的話詢問對方是否想更深入了解。"
    )

    control_hint = "可適度補充背景" if add_bg else "僅回答問題，不另外補充"
    user_text = (
        f"【風格】自然、清楚、口語且不生硬；避免任何透露來源的語句。\n"
        f"【背景補充】{control_hint}\n"
        f"【問題】\n{question}\n\n"
        f"【可用內容】\n{only_text}\n\n"
        "直接寫成流暢的一段或數段文字，不要提到『圖片』『文字』『提示』或『系統』。"
    )

    if url:
        messages = [
            {"role": "system", "content": sys},
            {"role": "user", "content": [
                {"type": "text", "text": user_text},
                {"type": "image_url", "image_url": {"url": url}},
            ]},
        ]
    else:
        messages = [
            {"role": "system", "content": sys},
            {"role": "user", "content": user_text},
        ]

    a_seed = random.randint(1, 10_000)
    # 將 a_seed 也寫到 session 的 qa_meta（若已存在就更新）
    try:
        meta = dict(st.session_state.get("qa_meta", {}))
        meta["a_seed"] = a_seed
        st.session_state.qa_meta = meta
    except Exception:
        st.session_state.qa_meta = {"a_seed": a_seed, "temperature": temperature}

    try:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            temperature=temperature,
            seed=a_seed,
        )
        out = (resp.choices[0].message.content or "").strip()
        return sanitize_model_output(out)
    except Exception:
        return "文字未提供相關資訊。"

# ------------------------
# JSONL I/O
# ------------------------

def read_jsonl(path: str) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except Exception:
                pass
    return out

def write_jsonl(path: str, items: List[Dict[str, Any]]):
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    lock = FileLock(path + ".lock")
    with lock:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for obj in items:
                f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        os.replace(tmp, path)

# ------------------------
# Session state
# ------------------------

if "auth_user" not in st.session_state:
    st.session_state.auth_user = None  # {"username":..., "role":...}
if "data_items" not in st.session_state:
    st.session_state.data_items = read_jsonl(DATA_JSONL)
if "idx" not in st.session_state:
    st.session_state.idx = 0
if "qa_draft" not in st.session_state:
    st.session_state.qa_draft = None  # {"q": "...", "a": "..."}

# 預設溫度
if "temperature" not in st.session_state:
    st.session_state.temperature = round(random.uniform(0.1, 0.8), 2)

# ------------------------
# Sidebar：登入 + 進度條 + 統計
# ------------------------

st.sidebar.header("🔐 登入 Twinkle Gallery")
users = load_users_from_secrets()

if st.session_state.auth_user:
    st.sidebar.success(f"已登入：{st.session_state.auth_user['username']}")
    if st.sidebar.button("登出", width='stretch'):
        st.session_state.auth_user = None
        st.rerun()
else:
    with st.sidebar.form("login_form"):
        username = st.text_input("帳號")
        password = st.text_input("密碼", type="password")
        ok = st.form_submit_button("登入", width='stretch')
    if ok:
        rec = next((u for u in users if u.get("username") == username), None)
        if rec and verify_password(rec, password):
            st.session_state.auth_user = {"username": rec["username"], "role": rec.get("role", "editor")}
            st.sidebar.success("登入成功！")
            st.rerun()
        else:
            st.sidebar.error("登入失敗，請檢查帳密。")

st.sidebar.markdown("---")
# 進度條（messages 完成比例）
total = len(st.session_state.data_items)
completed = sum(1 for it in st.session_state.data_items if it.get("messages"))
percent = int(round((completed / total) * 100)) if total else 0
st.sidebar.caption("完成度（有對話的筆數 / 全部）")
st.sidebar.progress(percent)  # 0~100
st.sidebar.write(f"{completed} / {total}（{percent}％）")

# ✅ (3) 貢獻者統計：登入後才顯示，卡片化 + 小圖表
if st.session_state.auth_user:
    st.sidebar.markdown("---")
    st.sidebar.markdown("#### 👥 貢獻者統計")

    contrib = Counter(
        (it.get("contributor") or "").strip()
        for it in st.session_state.data_items
        if (it.get("contributor") or "").strip()
    )
    if contrib:
        total_contrib = sum(contrib.values())

        # 以兩欄 metric 呈現 Top N（更像小卡片）
        top_items = contrib.most_common(6)
        cols = st.sidebar.columns(2)
        for idx, (name, cnt) in enumerate(top_items):
            with cols[idx % 2]:
                st.metric(label=name or "(未署名)", value=cnt)

        # 百分比分佈的小型長條圖（美觀且一眼懂）
        st.sidebar.caption("分佈概覽")
        # bar_chart 接受 dict/list；這裡用 dict 更簡潔
        st.sidebar.bar_chart(
            data={k or "(未署名)": v for k, v in contrib.most_common()},
            height=140
        )
    else:
        st.sidebar.caption("尚無貢獻者資料")

st.sidebar.markdown("---")
st.sidebar.caption("指導單位")
moda_img = _pick_image_by_time("static/moda_light.svg", "static/moda_dark.svg")
st.sidebar.image(moda_img, width='stretch')
iii_img = _pick_image_by_time("static/iii_light.svg", "static/iii_dark.svg")
st.sidebar.image(iii_img, width='stretch')
st.sidebar.image("static/ocf.svg")

st.sidebar.markdown("---")
st.sidebar.caption("專案與程式碼")
st.sidebar.markdown("🤗 [Formosa-Vision](https://huggingface.co/datasets/lianghsun/Formosa-Vision)")
st.sidebar.markdown("💖 [GitHub 專案頁](https://github.com/ai-twinkle/twinkle-gallery)")

# ------------------------
# Main（左右並排：左=圖+文+🎲；右=新增對話→既有對話）
# ------------------------

if total == 0:
    st.warning("資料為空，請準備 data.jsonl。")
    st.stop()

item = st.session_state.data_items[st.session_state.idx]
img_path = item.get("image_path", "")
text = item.get("text", "")
messages = item.get("messages", [])

# 兩欄
left_col, right_col = st.columns(2)

# ---- 左側：圖片 + 文字 + 🎲 ----
with left_col:
    if img_path and os.path.exists(img_path):
        st.image(img_path, width='stretch')
        # 圖片 caption：使用 JSONL 的 source（原圖網址）
        src_caption = item.get("source") or ""
        if src_caption:
            st.caption(src_caption)
    else:
        st.warning("找不到圖片檔案。")

    st.text_area(
        "本筆文字",
        value=text,
        height=250,
        key=f"text_display_{st.session_state.idx}",
        disabled=True,
        label_visibility="collapsed"
    )

    if st.button("🎲 隨機挑沒有對話的資料", width='stretch'):
        empty_indices = [i for i, it in enumerate(st.session_state.data_items) if not it.get("messages")]
        if not empty_indices:
            st.info("沒有 messages 為空的資料。")
        else:
            st.session_state.idx = random.choice(empty_indices)
            st.session_state.qa_draft = None
            st.rerun()

    # 回報圖文不合：將此筆 contributor 標記為目前登入者
    if st.session_state.auth_user:
        if st.button("🚩 回報圖文不合（將此筆 contributor 設為我）", width='stretch'):
            item["contributor"] = st.session_state.auth_user["username"]
            st.session_state.data_items[st.session_state.idx] = item
            try:
                write_jsonl(DATA_JSONL, st.session_state.data_items)
                st.success("已回報並標記貢獻者為你。")
            except Exception as e:
                st.error(f"回報失敗：{e}")
    else:
        st.button("🚩 回報圖文不合（需登入）", width='stretch', disabled=True)

# ---- 右側：新增單筆對話（在上方） → 既有對話 ----
with right_col:
    need_login = st.session_state.auth_user is None

    # ✅ (2) 把「新增單筆對話」移到最上面
    st.markdown("### 新增單筆對話")
    btn_disabled = (st.session_state.qa_draft is not None) or need_login

    if need_login:
        st.warning("請先在左側登入，才能新增/存檔。")

    if st.button("➕ 新增單筆對話", width='stretch', disabled=btn_disabled):
        st.session_state.temperature = round(random.uniform(0.1, 0.8), 2)
        st.sidebar.info(f"🎲 本次隨機溫度：{st.session_state.temperature}")

        if not API_KEY or not API_BASE:
            st.error("尚未設定 API（請在 .streamlit/secrets.toml 放入 MY_API_BASE / OPENAI_API_KEY）。")
        else:
            with st.spinner("VLM 正在看圖提出問題…"):
                q = gen_question_from_image(img_path, fallback_text=text, temperature=st.session_state.temperature)
            if not q:
                st.error("產生問題失敗。")
            else:
                with st.spinner("根據文字與（如有）圖片產生答案…"):
                    a = gen_answer_from_text(
                        text,
                        q,
                        temperature=st.session_state.temperature,
                        img_path=img_path
                    ) or "文字未提供相關資訊。"
                st.session_state.qa_draft = {"q": q, "a": a}
                st.rerun()

    if st.session_state.qa_draft:
        meta = st.session_state.get("qa_meta", {})
        mode_label = "導向介紹（盲問）" if meta.get("mode") == "intro" else "看圖提問"
        temp_val = meta.get("temperature", st.session_state.temperature)
        q_seed = meta.get("q_seed", "-")
        a_seed = meta.get("a_seed", "-")
        # st.info(f"草稿已產生，可編輯後存檔或取消。｜模式：{mode_label}｜temp：{temp_val}｜q_seed：{q_seed}｜a_seed：{a_seed}")
        st.info(f"草稿已產生，可編輯後存檔或取消。｜模式：{mode_label}")
        
        st.text_input("問題（可改）", value=st.session_state.qa_draft["q"], key="draft_q")
        st.text_area("答案（可改）", value=st.session_state.qa_draft["a"], key="draft_a", height=180)

        c1, c2 = st.columns(2)
        with c1:
            if st.button("💾 存檔（追加到 messages）", width='stretch', disabled=need_login):
                q = st.session_state.get("draft_q", "").strip()
                a = st.session_state.get("draft_a", "").strip()
                if not q or not a:
                    st.warning("問題與答案不可為空。")
                else:
                    messages = item.get("messages", [])
                    messages.extend([
                        {"role": "user", "content": q},
                        {"role": "assistant", "content": a},
                    ])
                    item["messages"] = messages

                    if not item.get("model"):
                        item["model"] = MODEL
                    if st.session_state.auth_user:
                        item["contributor"] = st.session_state.auth_user["username"]

                    st.session_state.data_items[st.session_state.idx] = item
                    try:
                        write_jsonl(DATA_JSONL, st.session_state.data_items)
                        st.success("已存檔！")
                        st.session_state.qa_draft = None
                        st.rerun()
                    except Exception as e:
                        st.error(f"存檔失敗：{e}")

        with c2:
            if st.button("🗑️ 取消本輪新增", type="secondary", width='stretch'):
                st.session_state.qa_draft = None
                st.success("已丟棄草稿。")
                st.rerun()

    st.subheader("既有對話（messages）")
    if not messages:
        st.caption("目前沒有對話。")
    else:
        for i in range(0, len(messages), 2):
            pair = messages[i:i+2]
            user_msg = pair[0] if len(pair) > 0 else {}
            asst_msg = pair[1] if len(pair) > 1 else {}

            q_key = f"msg_q_{st.session_state.idx}_{i}"
            a_key = f"msg_a_{st.session_state.idx}_{i}"

            q_val = user_msg.get("content", "")
            a_val = asst_msg.get("content", "")

            with st.container(border=True):
                st.caption(f"第 {i//2 + 1} 筆")
                st.text_input("Q（可編輯）", value=q_val, key=q_key)
                st.text_area("A（可編輯）", value=a_val, key=a_key, height=140)

                c_del, c_save = st.columns(2)
                with c_del:
                    if st.button("🗑️ 刪除", key=f"btn_del_{st.session_state.idx}_{i}", width='stretch', disabled=need_login):
                        new_msgs = item.get("messages", []).copy()
                        del new_msgs[i: i+2]
                        item["messages"] = new_msgs
                        st.session_state.data_items[st.session_state.idx] = item
                        try:
                            write_jsonl(DATA_JSONL, st.session_state.data_items)
                            st.success("已刪除該筆對話。")
                            st.rerun()
                        except Exception as e:
                            st.error(f"刪除失敗：{e}")

                with c_save:
                    if st.button("💾 儲存", key=f"btn_save_{st.session_state.idx}_{i}", width='stretch', disabled=need_login):
                        new_q = st.session_state.get(q_key, "").strip()
                        new_a = st.session_state.get(a_key, "").strip()
                        try:
                            if i < len(item.get("messages", [])):
                                item["messages"][i]["content"] = new_q
                            if i+1 < len(item.get("messages", [])):
                                item["messages"][i+1]["content"] = new_a

                            st.session_state.data_items[st.session_state.idx] = item
                            write_jsonl(DATA_JSONL, st.session_state.data_items)
                            st.success("已更新並寫回檔案。")
                            st.rerun()
                        except Exception as e:
                            st.error(f"儲存失敗：{e}")

            st.divider()

    st.markdown("---")
    c3, c4 = st.columns(2)
    with c3:
        if st.button("🔄 重新讀取檔案", width='stretch'):
            st.session_state.data_items = read_jsonl(DATA_JSONL)
            st.success("已重新載入。")
            st.rerun()
    with c4:
        if st.button("🧷 重新寫回（無變更也覆寫）", width='stretch', disabled=need_login):
            try:
                write_jsonl(DATA_JSONL, st.session_state.data_items)
                st.success("已寫回檔案。")
            except Exception as e:
                st.error(f"寫回失敗：{e}")