#!/usr/bin/env python3
"""
GaoooN 日次レビューレポート → LINE 公式アカウント配信（Flex Messageカード）

処理:
  1. Playwright で GaoooN にヘッドレス自動ログイン
  2. 対象店舗の「アンケート結果（満足度の低い回答フィルタ）」を取得
  3. Google クチコミ（コメントあり）を取得
  4. Claude API で構造化要約（JSON）を生成
  5. Python 側で LINE Flex Message を組み立て、broadcast API で配信

実行モード:
  - SEND_MODE=send で実送信、それ以外はドライラン（JSON標準出力のみ）

ローカル実行例:
    pip install -r requirements.txt
    playwright install chromium
    cp .env.example .env  # 値を埋める
    python daily_report.py                # ドライラン
    SEND_MODE=send python daily_report.py # 実送信
"""

from __future__ import annotations
import os
import re
import sys
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from pathlib import Path

import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, Page

# ------------------------------------------------------------------
# 設定
# ------------------------------------------------------------------
_ENV_PATH = Path(__file__).resolve().parent / ".env"
print(f"DEBUG: loading env from {_ENV_PATH} (exists={_ENV_PATH.exists()})", file=sys.stderr)
load_dotenv(dotenv_path=_ENV_PATH, override=True)

GAOOON_EMAIL = os.getenv("GAOOON_EMAIL", "").strip()
GAOOON_PASSWORD = os.getenv("GAOOON_PASSWORD", "").strip()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
LINE_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
LINE_GROUP_ID = os.getenv("LINE_GROUP_ID", "").strip()  # 設定があれば push、無ければ broadcast
SEND_MODE = os.getenv("SEND_MODE", "dry-run").strip().lower()

# 対象店舗リスト（BWC社の上位5店舗）
STORES = [
    {"location": "chukasobanito_nagoya",   "name": "中華そば二兎"},
    {"location": "toriko_shibuya",          "name": "挽き肉のトリコ 渋谷店"},
    {"location": "toriko_sakae",            "name": "挽き肉のトリコ 栄店"},
    {"location": "todoroki_meieki",         "name": "肉玉中華そば 轟"},
    {"location": "meimeimaratan_sakae",     "name": "美美マーラータン"},
]

# 環境変数で対象を絞り込みたい場合（テスト用に1店舗だけ走らせる等）
_only = os.getenv("STORE_LOCATION", "").strip()
if _only:
    STORES = [s for s in STORES if s["location"] == _only]

BASE_URL = "https://meo-integrated-application.bubbleapps.io"
LOGIN_URL = f"{BASE_URL}/login"

def survey_url(location: str) -> str:
    return f"{BASE_URL}/questionnaire_result_3?location={location}"

JST = timezone(timedelta(hours=9))
TODAY = datetime.now(JST).date()
# 週次レポート：直近の月曜〜日曜
_dow = TODAY.weekday()  # 月=0
WEEK_START = TODAY - timedelta(days=_dow + 7)  # 先週月曜
WEEK_END = WEEK_START + timedelta(days=6)       # 先週日曜
WEEK_LABEL = f"{WEEK_START.strftime('%-m/%-d')}〜{WEEK_END.strftime('%-m/%-d')}"


# ------------------------------------------------------------------
# データ構造
# ------------------------------------------------------------------
@dataclass
class SurveyAnswer:
    timestamp: str
    rating: str
    gender: str
    age: str
    source: str
    staff: str
    cleanliness: str
    speed: str
    taste: str
    comment: str


@dataclass
class GoogleReview:
    timestamp: str
    author: str
    stars: int
    text: str


@dataclass
class CollectedData:
    store_name: str
    surveys_low: List[SurveyAnswer] = field(default_factory=list)
    reviews_low: List[GoogleReview] = field(default_factory=list)


# ------------------------------------------------------------------
# Playwright スクレイピング
# ------------------------------------------------------------------
def login(page: Page) -> None:
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    page.wait_for_selector('input[type="email"]', timeout=15000)
    page.fill('input[type="email"]', GAOOON_EMAIL)
    page.fill('input[type="password"]', GAOOON_PASSWORD)
    page.locator('button:has-text("ログイン")').first.click()
    page.wait_for_function("() => !location.pathname.includes('/login')", timeout=20000)
    page.wait_for_load_state("networkidle", timeout=15000)


def open_survey_result_page(page: Page, location: str) -> None:
    page.goto(survey_url(location), wait_until="domcontentloaded")
    page.wait_for_load_state("networkidle", timeout=30000)
    page.wait_for_timeout(4000)  # ★Bubbleの初期描画は重め

    # 「アンケート結果」タブ（ヘッダー部にあるボタン）をクリック
    tab_clicked = False
    candidates = [
        'button:has-text("アンケート結果")',
        '[role="button"]:has-text("アンケート結果")',
        'div:has-text("アンケート結果"):not(:has(*))',
    ]
    for sel in candidates:
        elems = page.locator(sel)
        cnt = elems.count()
        for i in range(cnt):
            try:
                el = elems.nth(i)
                if el.is_visible():
                    box = el.bounding_box()
                    if box and box["y"] < 350:
                        el.click()
                        tab_clicked = True
                        break
            except Exception:
                continue
        if tab_clicked:
            break
    if not tab_clicked:
        print("  WARN: アンケート結果タブが見つからない", file=sys.stderr)

    page.wait_for_timeout(7000)  # ★タブ切替→テーブル再描画待ち（クラウド遅延対策）

    # 「満足度の高い回答」→クリックで「低い回答」に切り替わるトグル
    # クラウドのヘッドレス環境ではclickが効かないケースがあるため、複数手段でフォールバック
    # 切り替え成功の判定は「データ行が再描画される」ことで行う

    def has_low_data():
        """満足度の低い回答テーブルに日付付きの行が出現したか"""
        try:
            return page.evaluate(
                "() => /\\d{4}年\\s*\\d{1,2}月\\s*\\d{1,2}日\\s*\\([日月火水木金土]\\)\\s*\\d{1,2}:\\d{2}/.test(document.body.innerText)"
            )
        except Exception:
            return False

    toggle_methods = []

    def try_standard_click():
        try:
            btn = page.locator('text=満足度の高い回答').first
            if btn.is_visible():
                btn.click()
                return "standard"
        except Exception as e:
            return f"standard_err:{e.__class__.__name__}"
        return None

    def try_force_click():
        try:
            page.locator('text=満足度の高い回答').first.click(force=True, timeout=5000)
            return "force"
        except Exception as e:
            return f"force_err:{e.__class__.__name__}"

    def try_js_click():
        try:
            ok = page.evaluate("""() => {
                const els = Array.from(document.querySelectorAll('*'));
                for (const el of els) {
                    if (el.children.length === 0 && el.textContent &&
                        el.textContent.trim() === '満足度の高い回答') {
                        // 親要素まで遡って onclick がついている要素を探して1回だけクリック
                        let p = el;
                        for (let i = 0; i < 5 && p; i++) {
                            const styles = window.getComputedStyle(p);
                            if (styles.cursor === 'pointer' || p.onclick) {
                                p.click(); return true;
                            }
                            p = p.parentElement;
                        }
                        // 最後の手段：直接テキストノード親をクリック
                        if (el.parentElement) { el.parentElement.click(); return true; }
                    }
                }
                return false;
            }""")
            return "js" if ok else "js_no_target"
        except Exception as e:
            return f"js_err:{e.__class__.__name__}"

    # 標準clickを試して、データ行が出るまで待つ。出なければ次の手段を試す
    for attempt_name, fn in [("standard", try_standard_click), ("force", try_force_click), ("js", try_js_click)]:
        res = fn()
        toggle_methods.append(f"{attempt_name}={res}")
        # クリック後にデータが出るまで最大15秒待つ
        try:
            page.wait_for_function(
                "() => /\\d{4}年\\s*\\d{1,2}月\\s*\\d{1,2}日\\s*\\([日月火水木金土]\\)\\s*\\d{1,2}:\\d{2}/.test(document.body.innerText)",
                timeout=15000
            )
            print(f"  DEBUG: filter toggle ok via [{attempt_name}] / methods={toggle_methods}", file=sys.stderr)
            break
        except Exception:
            continue
    else:
        print(f"  WARN: filter toggle all-failed / methods={toggle_methods}", file=sys.stderr)

    page.wait_for_load_state("networkidle", timeout=15000)
    page.wait_for_timeout(2000)

    page.wait_for_load_state("networkidle", timeout=20000)
    page.wait_for_timeout(3000)

    # データテーブルが描画されるまで明示待機（行が出るまで or 上限15秒）
    try:
        page.wait_for_function(
            "() => document.body.innerText.match(/\\d{4}年\\s*\\d{1,2}月\\s*\\d{1,2}日/) "
            "|| document.body.innerText.includes('回答数')",
            timeout=15000
        )
    except Exception:
        pass

    # ★クラウドでの初期化失敗対策：月セレクタを「別月→現在月」とガチャしてデータ強制再ロード
    try:
        cur_month = TODAY.month
        prev_month = 12 if cur_month == 1 else cur_month - 1
        selects = page.locator('select').all()
        for s in selects:
            try:
                opts = s.locator('option').all_inner_texts()
                if any(o.strip() in {f"{i}月" for i in range(1, 13)} for o in opts):
                    s.select_option(label=f"{prev_month}月")
                    page.wait_for_timeout(3000)
                    s.select_option(label=f"{cur_month}月")
                    page.wait_for_timeout(5000)
                    print(f"  DEBUG: month re-toggled {prev_month}月→{cur_month}月", file=sys.stderr)
                    break
            except Exception:
                continue
        page.wait_for_timeout(2000)
    except Exception as e:
        print(f"  WARN: month re-toggle skipped: {e}", file=sys.stderr)


def parse_survey_rows(page: Page) -> List[SurveyAnswer]:
    body_text = page.locator("body").inner_text()
    answers: List[SurveyAnswer] = []
    blocks = re.split(
        r'(\d{4}年\s*\d{1,2}月\s*\d{1,2}日\s*\([日月火水木金土]\)\s*\d{1,2}:\d{2})',
        body_text
    )
    for i in range(1, len(blocks) - 1, 2):
        ts = blocks[i].strip()
        body = blocks[i + 1]
        if "クチコミ集計" in body or "月別の集計結果" in body:
            break
        lines = [l.strip() for l in body.splitlines() if l.strip()]
        if len(lines) < 4:
            continue
        rating = lines[0] if lines else ""
        if rating not in ("どちらでもない", "不満", "非常に不満"):
            continue
        gender = lines[1] if len(lines) > 1 else ""
        age = lines[2] if len(lines) > 2 else ""
        source = lines[3] if len(lines) > 3 else ""
        staff = lines[4] if len(lines) > 4 else ""
        cleanliness = lines[5] if len(lines) > 5 else ""
        speed = lines[6] if len(lines) > 6 else ""
        taste = lines[7] if len(lines) > 7 else ""
        nokori = lines[8:]
        noko_keys = {"濃い", "ちょっと濃い", "ちょうどいい", "ちょっと薄い", "薄い"}
        if nokori and nokori[-1] in noko_keys:
            comment = " / ".join(nokori[:-1]).strip()
        else:
            comment = " / ".join(nokori).strip()
        answers.append(SurveyAnswer(
            timestamp=ts, rating=rating, gender=gender, age=age,
            source=source, staff=staff, cleanliness=cleanliness,
            speed=speed, taste=taste, comment=comment,
        ))
    return answers


def open_google_reviews_tab(page: Page) -> None:
    page.locator('text=Googleクチコミ').first.click()
    page.wait_for_timeout(6000)  # ★クチコミタブの描画待ち
    page.wait_for_load_state("networkidle", timeout=20000)
    page.wait_for_timeout(2000)


def parse_google_reviews(page: Page) -> List[GoogleReview]:
    text = page.locator("body").inner_text()
    reviews: List[GoogleReview] = []
    blocks = re.split(
        r'(\d{1,2}月\s*\d{1,2}日\s*\([日月火水木金土]\)\s*\d{1,2}:\d{2})',
        text
    )
    for i in range(1, len(blocks) - 1, 2):
        ts = blocks[i].strip()
        body = blocks[i + 1]
        if "前ページ" in body or "クチコミ数推移" in body or "アンケート結果" in body:
            break
        lines = [l for l in body.splitlines() if l.strip()]
        if not lines:
            continue
        author = lines[0].strip()
        body_text = "\n".join(lines[1:]).strip()
        body_text = re.sub(
            r'(前ページ|次ページ|first_page|last_page|chevron_\w+|\d+/\d+).*',
            '', body_text, flags=re.DOTALL
        ).strip()
        reviews.append(GoogleReview(timestamp=ts, author=author, stars=0, text=body_text))
        if len(reviews) >= 50:
            break
    return reviews


def collect_for_store(page: Page, store: dict,
                       debug_dir: Optional[Path] = None) -> CollectedData:
    """1セッション内で複数店舗のデータを順次取得するための単店舗用ヘルパー"""
    data = CollectedData(store_name=store["name"])
    location = store["location"]
    try:
        open_survey_result_page(page, location)
        if debug_dir:
            page.screenshot(
                path=str(debug_dir / f"02_survey_{location}.png"), full_page=True
            )
        data.surveys_low = parse_survey_rows(page)
        # 0件のときは描画失敗の可能性 → 待機してリトライ（クラウド環境向け）
        for retry_i in range(2):
            if data.surveys_low:
                break
            print(f"  [{store['name']}] 0件→リトライ {retry_i+1}/2", file=sys.stderr)
            page.wait_for_timeout(6000)
            data.surveys_low = parse_survey_rows(page)
        print(f"  [{store['name']}] 低評価アンケート: {len(data.surveys_low)} 件",
              file=sys.stderr)

        open_google_reviews_tab(page)
        if debug_dir:
            page.screenshot(
                path=str(debug_dir / f"03_reviews_{location}.png"), full_page=True
            )
        all_reviews = parse_google_reviews(page)
        print(f"  [{store['name']}] Google クチコミ: {len(all_reviews)} 件",
              file=sys.stderr)
        data.reviews_low = [r for r in all_reviews if r.text and len(r.text) > 20]
    except Exception as e:
        print(f"  ERROR [{store['name']}]: {e}", file=sys.stderr)
        if debug_dir:
            try:
                page.screenshot(
                    path=str(debug_dir / f"99_err_{location}.png"), full_page=True
                )
            except Exception:
                pass
        raise
    return data


def collect_all(stores: list, headless: bool = True,
                debug_dir: Optional[Path] = None) -> list:
    """1セッションでログイン→全店舗ループ"""
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx = browser.new_context(viewport={"width": 1400, "height": 900}, locale="ja-JP")
        page = ctx.new_page()
        try:
            login(page)
            if debug_dir:
                page.screenshot(path=str(debug_dir / "01_after_login.png"))
            for store in stores:
                try:
                    data = collect_for_store(page, store, debug_dir=debug_dir)
                    results.append((store, data))
                except Exception:
                    # 1店舗失敗しても他の店舗は続行
                    results.append((store, None))
        finally:
            browser.close()
    return results


# ------------------------------------------------------------------
# Claude API 要約 → Flex 用構造化JSON
# ------------------------------------------------------------------
def summarize_to_struct(data: CollectedData) -> dict:
    surveys_text = "\n".join([
        f"- {a.timestamp} / {a.rating} / {a.gender}{a.age} / {a.source}\n"
        f"  [接客:{a.staff} / 清潔:{a.cleanliness} / 提供:{a.speed} / 味:{a.taste}]\n"
        f"  コメント: {a.comment if a.comment else '(なし)'}"
        for a in data.surveys_low
    ]) or "(該当なし)"

    reviews_text = "\n\n".join([
        f"▼{r.timestamp} {r.author}\n{r.text}" for r in data.reviews_low[:30]
    ]) or "(該当なし)"

    prompt = f"""あなたはラーメン店「{data.store_name}」の店舗運営者向け週次レポートを作るアシスタントです。
対象期間: {WEEK_LABEL}（先週月曜〜日曜）

以下のデータから、LINE Flex Message Carousel に埋め込む情報を **JSON のみ** で返してください。
マークダウン記号やコードブロックは不要、純粋な JSON だけを返してください。

【データ：先週分の "満足度の低い回答" アンケート】
{surveys_text}

【データ：先週前後のGoogleクチコミ抜粋（コメントあり）】
{reviews_text}

【返す JSON 構造（厳守）】
{{
  "kpi": {{
    "survey_low_count": 11,
    "review_low_count": 2,
    "satisfaction_rate_pct": 96,
    "comment_one_line": "直近1週間で目立つのは接客・提供スピードへの不満。改善余地あり"
  }},
  "alerts_survey": [
    {{
      "ts": "5/24(日) 14:13",
      "who": "女性40代・通りすがり",
      "scores": {{
        "staff": "良くない",
        "clean": "良くない",
        "speed": "悪い",
        "taste": "良くない"
      }},
      "quote": "麺が伸びていた",
      "action": "提供までの時間チェック（特に土曜14時帯）"
    }}
  ],
  "alerts_review": [
    {{
      "ts": "5/22(金) 21:17",
      "who": "犬ぐー さん",
      "stars": 3,
      "quote": "21時過ぎ・半分の入りでも席を詰めて座らせる。ホスピタリティが残念。",
      "action": "ピーク後の席配置ルールを再確認"
    }}
  ],
  "improvements": [
    {{
      "priority": "high",
      "icon": "🍜",
      "title": "麺の品質管理",
      "body": "茹で時間徹底、ピーク後の提供時間短縮。提供から喫食までを30秒以内に。"
    }}
  ],
  "good_voices": [
    {{
      "ts": "5/23(土)",
      "who": "O. shoto さん（★5）",
      "quote": "店長(？)男性の方の非常に丁寧な仕事に感動しました"
    }}
  ]
}}

【ルール】
- kpi.comment_one_line は1〜2行のサマリーコメント（80字以内）
- alerts_survey は最大4件
  - scores の値は元データそのまま（「かなり良い/良い/普通/良くない/悪い」のいずれか）
  - action は **データから推定できる具体的な対応案** を短く（40字以内）
  - quote がない場合は空文字列""
- alerts_review は最大3件
  - **★3以下 or 明らかな不満コメントのみ**
  - stars は推定の整数（不明なら0）
  - quote は 80字以内に要約／引用、内容は捏造しない
  - action も短く
- improvements は最大3件
  - priority: "high"/"medium"/"low"
  - icon は内容にあった絵文字1つ（🍜 / 👥 / 🧹 / ⏱ / 💬 など）
- good_voices は最大3件
  - **接客・店員・スタッフ・店長・対応・ホスピタリティに言及している投稿のみ**
  - 味・雰囲気・コスパの褒めは含めない
- 該当なしは空配列 []"""

    headers = {
        "Content-Type": "application/json",
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
    }
    payload = {
        "model": "claude-sonnet-4-5-20250929",
        "max_tokens": 2500,
        "messages": [{"role": "user", "content": prompt}],
    }
    res = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers=headers, json=payload, timeout=180
    )
    res.raise_for_status()
    text = res.json()["content"][0]["text"].strip()
    if text.startswith("```"):
        text = re.sub(r'^```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```\s*$', '', text)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        print(f"WARN: JSON parse failed: {e}\n{text}", file=sys.stderr)
        return {
            "alert_count_text": "（要約パース失敗）",
            "alerts_survey": [], "alerts_review": [],
            "good_voices": [], "improvements": []
        }


# ------------------------------------------------------------------
# LINE Flex Message 組み立て
# ------------------------------------------------------------------
def _txt(text: str, **kwargs) -> dict:
    base = {"type": "text", "text": text, "wrap": True}
    base.update(kwargs)
    return base


def _sep(margin: str = "md") -> dict:
    return {"type": "separator", "color": "#e5e7eb", "margin": margin}


# ----- score → dots / 色 -----
SCORE_ORDER = ["かなり良い", "良い", "普通", "良くない", "悪い"]
SCORE_DOTS = {  # 5段階を●●●●● で表示。低いほど赤・少ない
    "かなり良い": ("●●●●●", "#1f7a4f"),
    "良い":       ("●●●●○", "#46a39d"),
    "普通":       ("●●●○○", "#e0a93b"),
    "良くない":   ("●●○○○", "#e07a3b"),
    "悪い":       ("●○○○○", "#d65d6e"),
}


def _build_kpi_card(struct: dict, store_name: str, today_label: str) -> dict:
    """カード①: KPIサマリー"""
    kpi = struct.get("kpi", {}) or {}
    survey_n = kpi.get("survey_low_count", 0)
    review_n = kpi.get("review_low_count", 0)
    sat_pct = kpi.get("satisfaction_rate_pct", 0)
    comment = kpi.get("comment_one_line", "")

    alert_total = survey_n + review_n
    badge_color = "#d65d6e" if alert_total >= 3 else ("#e0a93b" if alert_total >= 1 else "#1f7a4f")
    badge_text = f"要注意 {alert_total}件" if alert_total > 0 else "✓ 異常なし"

    return {
        "type": "bubble",
        "size": "kilo",
        "header": {
            "type": "box", "layout": "vertical",
            "backgroundColor": "#1f2330",
            "paddingAll": "20px",
            "contents": [
                {
                    "type": "box", "layout": "horizontal",
                    "contents": [
                        _txt("🍜 " + store_name, color="#ffffff", size="md", weight="bold", flex=1),
                        {
                            "type": "box", "layout": "vertical",
                            "backgroundColor": badge_color,
                            "cornerRadius": "12px",
                            "paddingStart": "10px", "paddingEnd": "10px",
                            "paddingTop": "3px", "paddingBottom": "3px",
                            "contents": [_txt(badge_text, color="#ffffff", size="xxs", weight="bold")],
                            "flex": 0,
                        }
                    ]
                },
                _txt(f"ウィークリーレポート　{WEEK_LABEL}", color="#cfd6db", size="xxs", margin="xs"),
            ]
        },
        "body": {
            "type": "box", "layout": "vertical",
            "paddingAll": "20px", "spacing": "none",
            "contents": [
                _txt(f"先週（{WEEK_LABEL}）サマリー", color="#6c7280", size="xxs", weight="bold"),
                {
                    "type": "box", "layout": "horizontal", "margin": "md", "spacing": "md",
                    "contents": [
                        {
                            "type": "box", "layout": "vertical", "flex": 1,
                            "backgroundColor": "#fff5f5", "cornerRadius": "8px",
                            "paddingAll": "10px",
                            "contents": [
                                _txt("低評価", size="xxs", color="#a3293a"),
                                _txt(str(survey_n), size="xxl", color="#a3293a", weight="bold"),
                                _txt("アンケート", size="xxs", color="#6c7280"),
                            ]
                        },
                        {
                            "type": "box", "layout": "vertical", "flex": 1,
                            "backgroundColor": "#fff5f5", "cornerRadius": "8px",
                            "paddingAll": "10px",
                            "contents": [
                                _txt("★3↓", size="xxs", color="#a3293a"),
                                _txt(str(review_n), size="xxl", color="#a3293a", weight="bold"),
                                _txt("クチコミ", size="xxs", color="#6c7280"),
                            ]
                        },
                        {
                            "type": "box", "layout": "vertical", "flex": 1,
                            "backgroundColor": "#e3f3ec", "cornerRadius": "8px",
                            "paddingAll": "10px",
                            "contents": [
                                _txt("満足率", size="xxs", color="#1f7a4f"),
                                _txt(f"{sat_pct}", size="xxl", color="#1f7a4f", weight="bold"),
                                _txt("％", size="xxs", color="#6c7280"),
                            ]
                        },
                    ]
                },
                _sep("xl"),
                _txt("💬 今日のひと言", color="#6c7280", size="xxs", weight="bold", margin="md"),
                _txt(comment or "本日は特筆事項なし。", size="sm", color="#1f2330", margin="sm"),
                _sep("xl"),
                _txt("👉 横スワイプで詳細を見る", color="#46a39d", size="xxs", margin="md", align="center"),
            ]
        },
    }


def _build_survey_card(alerts: list, today_label: str) -> Optional[dict]:
    if not alerts:
        return None
    items = []
    for i, a in enumerate(alerts[:4]):
        if i > 0:
            items.append(_sep("md"))
        head = f"{a.get('ts','')}  {a.get('who','')}"
        items.append(_txt(head, size="xxs", color="#6c7280"))
        quote = (a.get('quote') or '').strip()
        if quote:
            items.append(_txt(f"「{quote}」", size="sm", color="#1f2330", weight="bold", margin="xs"))
        scores = a.get('scores') or {}
        label_map = [("staff", "接客"), ("clean", "清潔"), ("speed", "提供"), ("taste", "味")]
        score_rows = []
        for key, label in label_map:
            val = scores.get(key, "")
            if val and val in SCORE_DOTS:
                dots, color = SCORE_DOTS[val]
                score_rows.append({
                    "type": "box", "layout": "horizontal", "margin": "xs",
                    "contents": [
                        _txt(label, size="xxs", color="#6c7280", flex=1),
                        _txt(dots, size="xs", color=color, flex=0),
                        _txt(f" {val}", size="xxs", color="#6c7280", flex=2, margin="xs"),
                    ]
                })
        if score_rows:
            items.extend(score_rows)
        action = (a.get('action') or '').strip()
        if action:
            items.append({
                "type": "box", "layout": "vertical",
                "backgroundColor": "#ffe9d9", "cornerRadius": "4px",
                "paddingAll": "6px", "margin": "sm",
                "contents": [_txt(f"💡 {action}", size="xxs", color="#8a4914")]
            })

    return {
        "type": "bubble", "size": "kilo",
        "header": {
            "type": "box", "layout": "vertical",
            "backgroundColor": "#a3293a", "paddingAll": "16px",
            "contents": [
                _txt("🚨 アンケート低評価", color="#ffffff", size="md", weight="bold"),
                _txt(f"{len(alerts)}件 ／ 先週 {WEEK_LABEL}", color="#ffe1e6", size="xxs", margin="xs"),
            ]
        },
        "body": {
            "type": "box", "layout": "vertical",
            "paddingAll": "16px", "contents": items
        }
    }


def _build_review_card(reviews: list, today_label: str) -> Optional[dict]:
    if not reviews:
        return None
    items = []
    for i, r in enumerate(reviews[:3]):
        if i > 0:
            items.append(_sep("md"))
        stars = int(r.get('stars') or 0)
        star_str = "★" * stars + "☆" * (5 - stars) if stars else "★ — —"
        head_row = {
            "type": "box", "layout": "horizontal",
            "contents": [
                _txt(r.get('who', ''), size="xs", color="#1f2330", weight="bold", flex=1),
                _txt(star_str, size="xs", color="#e0a93b", flex=0),
            ]
        }
        items.append(head_row)
        items.append(_txt(r.get('ts', ''), size="xxs", color="#6c7280", margin="xs"))
        items.append(_txt(f"「{(r.get('quote') or '').strip()}」", size="sm", color="#1f2330", margin="sm"))
        action = (r.get('action') or '').strip()
        if action:
            items.append({
                "type": "box", "layout": "vertical",
                "backgroundColor": "#ffe9d9", "cornerRadius": "4px",
                "paddingAll": "6px", "margin": "sm",
                "contents": [_txt(f"💡 {action}", size="xxs", color="#8a4914")]
            })

    return {
        "type": "bubble", "size": "kilo",
        "header": {
            "type": "box", "layout": "vertical",
            "backgroundColor": "#a3293a", "paddingAll": "16px",
            "contents": [
                _txt("⭐ 低評価クチコミ", color="#ffffff", size="md", weight="bold"),
                _txt(f"{len(reviews)}件 ／ 先週 {WEEK_LABEL}", color="#ffe1e6", size="xxs", margin="xs"),
            ]
        },
        "body": {"type": "box", "layout": "vertical", "paddingAll": "16px", "contents": items}
    }


def _build_improvement_card(improvements: list, today_label: str) -> Optional[dict]:
    if not improvements:
        return None
    items = []
    priority_color = {"high": "#d65d6e", "medium": "#e0a93b", "low": "#6c7280"}
    priority_label = {"high": "最優先", "medium": "推奨", "low": "余裕があれば"}
    for i, imp in enumerate(improvements[:3]):
        if i > 0:
            items.append(_sep("md"))
        p = imp.get('priority', 'medium')
        color = priority_color.get(p, '#e0a93b')
        label = priority_label.get(p, '推奨')
        icon = imp.get('icon', '💡')
        items.append({
            "type": "box", "layout": "horizontal", "alignItems": "center", "spacing": "sm",
            "contents": [
                {
                    "type": "box", "layout": "vertical",
                    "backgroundColor": color, "cornerRadius": "12px",
                    "paddingStart": "8px", "paddingEnd": "8px",
                    "paddingTop": "3px", "paddingBottom": "3px",
                    "width": "70px",
                    "contents": [_txt(label, color="#ffffff", size="xxs", weight="bold", align="center")]
                },
                _txt(f"{icon} {imp.get('title','')}", size="md", weight="bold", color="#1f2330", flex=1),
            ]
        })
        items.append(_txt(imp.get('body', ''), size="xs", color="#3f4451", margin="sm"))

    return {
        "type": "bubble", "size": "kilo",
        "header": {
            "type": "box", "layout": "vertical",
            "backgroundColor": "#2d736e", "paddingAll": "16px",
            "contents": [
                _txt("💡 今日の改善アクション", color="#ffffff", size="md", weight="bold"),
                _txt(f"AI提案　今週のアクション", color="#cfecea", size="xxs", margin="xs"),
            ]
        },
        "body": {"type": "box", "layout": "vertical", "paddingAll": "16px", "contents": items}
    }


def _build_good_card(voices: list, today_label: str, store_location: str) -> Optional[dict]:
    items = []
    if not voices:
        items.append(_txt("接客に関する高評価コメントは、本日該当なし。", size="sm", color="#1f7a4f"))
        items.append(_txt("引き続き丁寧な接客を心がけましょう 🙌", size="xs", color="#6c7280", margin="sm"))
    else:
        for i, v in enumerate(voices[:3]):
            if i > 0:
                items.append(_sep("md"))
            items.append(_txt(v.get('who', ''), size="xs", color="#1f7a4f", weight="bold"))
            items.append(_txt(v.get('ts', ''), size="xxs", color="#6c7280", margin="xs"))
            items.append(_txt(f"「{(v.get('quote') or '').strip()}」", size="sm", color="#143b27", margin="sm"))

    return {
        "type": "bubble", "size": "kilo",
        "header": {
            "type": "box", "layout": "vertical",
            "backgroundColor": "#1f7a4f", "paddingAll": "16px",
            "contents": [
                _txt("👍 接客への良い声", color="#ffffff", size="md", weight="bold"),
                _txt(f"先週のハイライト　{WEEK_LABEL}", color="#d4eee0", size="xxs", margin="xs"),
            ]
        },
        "body": {"type": "box", "layout": "vertical", "paddingAll": "16px",
                 "backgroundColor": "#e3f3ec", "contents": items},
        "footer": {
            "type": "box", "layout": "vertical",
            "contents": [{
                "type": "button",
                "style": "primary", "color": "#46a39d", "height": "sm",
                "action": {
                    "type": "uri",
                    "label": "📊 GaoooNで詳細を確認",
                    "uri": survey_url(store_location),
                }
            }]
        }
    }


def build_flex_message(struct: dict, store_name: str, store_location: str) -> dict:
    today_label = TODAY.strftime("%-m/%-d") + " (" + "月火水木金土日"[TODAY.weekday()] + ")"

    alerts_survey = struct.get("alerts_survey", []) or []
    alerts_review = struct.get("alerts_review", []) or []
    good_voices = struct.get("good_voices", []) or []
    improvements = struct.get("improvements", []) or []

    bubbles = [_build_kpi_card(struct, store_name, today_label)]
    if alerts_survey:
        bubbles.append(_build_survey_card(alerts_survey, today_label))
    if alerts_review:
        bubbles.append(_build_review_card(alerts_review, today_label))
    if improvements:
        bubbles.append(_build_improvement_card(improvements, today_label))
    bubbles.append(_build_good_card(good_voices, today_label, store_location))

    bubbles = [b for b in bubbles if b is not None][:10]  # LINE上限10枚

    alert_total = len(alerts_survey) + len(alerts_review)
    alt = f"🍜 {store_name} ウィークリーレポート {WEEK_LABEL}"
    if alert_total > 0:
        alt += f" — 要注意 {alert_total}件"
    return {
        "type": "flex",
        "altText": alt[:380],
        "contents": {"type": "carousel", "contents": bubbles}
    }


# ------------------------------------------------------------------
# LINE 配信
# ------------------------------------------------------------------
def send_line_broadcast(flex_message: dict) -> tuple[bool, str]:
    """LINE_GROUP_ID が設定されていれば push（グループ宛 / 通数効率◎）、
    無ければ broadcast（友達全員宛）にフォールバック"""
    if not LINE_TOKEN:
        return False, "LINE_CHANNEL_ACCESS_TOKEN が未設定"

    headers = {
        "Authorization": f"Bearer {LINE_TOKEN}",
        "Content-Type": "application/json",
    }

    if LINE_GROUP_ID:
        url = "https://api.line.me/v2/bot/message/push"
        payload = {"to": LINE_GROUP_ID, "messages": [flex_message]}
        mode = "push to group"
    else:
        url = "https://api.line.me/v2/bot/message/broadcast"
        payload = {"messages": [flex_message]}
        mode = "broadcast"

    res = requests.post(url, headers=headers, json=payload, timeout=30)
    if res.status_code >= 300:
        return False, f"{mode} HTTP {res.status_code}: {res.text}"
    return True, f"OK ({mode})"


# ------------------------------------------------------------------
# メイン
# ------------------------------------------------------------------
def main() -> int:
    print(f"[{datetime.now(JST).isoformat()}] 日次レポート開始", file=sys.stderr)
    print(f"  対象店舗: {len(STORES)}店", file=sys.stderr)
    for s in STORES:
        print(f"    - {s['name']} ({s['location']})", file=sys.stderr)
    print(f"  モード: {SEND_MODE}", file=sys.stderr)

    if not (GAOOON_EMAIL and GAOOON_PASSWORD):
        print("ERROR: GAOOON_EMAIL / GAOOON_PASSWORD が未設定", file=sys.stderr)
        return 2
    if not ANTHROPIC_API_KEY:
        print("ERROR: ANTHROPIC_API_KEY が未設定", file=sys.stderr)
        return 3

    # 1. データ取得（1セッションで全店舗）
    print("\n[1/3] GaoooN からデータ取得中...", file=sys.stderr)
    headless = SEND_MODE != "headed"
    debug_dir = Path(__file__).resolve().parent / "debug"
    debug_dir.mkdir(exist_ok=True)
    results = collect_all(STORES, headless=headless, debug_dir=debug_dir)

    # 2. 店舗ごとに要約 → 送信
    exit_code = 0
    for idx, (store, data) in enumerate(results, 1):
        name = store["name"]
        print(f"\n[{idx}/{len(STORES)}] {name} 処理開始", file=sys.stderr)
        if data is None:
            print(f"  SKIP: データ取得失敗", file=sys.stderr)
            exit_code = 4
            continue

        # 2-1 Claude 要約
        print(f"  Claude で要約中...", file=sys.stderr)
        try:
            struct = summarize_to_struct(data)
        except Exception as e:
            print(f"  ERROR: 要約失敗 {e}", file=sys.stderr)
            exit_code = 5
            continue

        # 2-2 Flex 組み立て
        flex_msg = build_flex_message(struct, name, store["location"])

        # 2-3 送信 or ドライラン
        if SEND_MODE == "send":
            ok, info = send_line_broadcast(flex_msg)
            print(f"  LINE送信: {info}", file=sys.stderr)
            if not ok:
                exit_code = 6
            # LINE 連続送信防止のため店舗間に短いウェイト
            if idx < len(STORES):
                import time
                time.sleep(2)
        else:
            print(f"  （ドライラン）", file=sys.stderr)
            print(f"--- {name} Flex JSON ---")
            print(json.dumps(flex_msg, ensure_ascii=False, indent=2))

    print(f"\n[完了] exit code={exit_code}", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
