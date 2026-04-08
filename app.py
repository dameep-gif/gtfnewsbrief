import base64
import html
import re
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path
from textwrap import dedent
import streamlit as st
import pandas as pd
from crawler import crawl_naver_news, crawl_google_news
from analyzer import analyze_sentiment, extract_keywords, clean_text
import time


def normalize_title_for_grouping(title):
    normalized = clean_text(str(title or "")).lower()
    return " ".join(normalized.split())


def parse_article_datetime(row):
    for column in ("발행일", "수집일시"):
        value = row.get(column)
        if not value:
            continue
        parsed = pd.to_datetime(value, errors="coerce")
        if not pd.isna(parsed):
            return parsed
    return pd.NaT


def calculate_title_similarity(left_title, right_title):
    if not left_title or not right_title:
        return 0.0

    sequence_score = SequenceMatcher(None, left_title, right_title).ratio()
    left_tokens = {token for token in left_title.split() if len(token) > 1}
    right_tokens = {token for token in right_title.split() if len(token) > 1}

    if left_tokens or right_tokens:
        token_score = len(left_tokens & right_tokens) / max(len(left_tokens | right_tokens), 1)
    else:
        token_score = 0.0

    prefix_bonus = 0.12 if left_title[:12] == right_title[:12] and left_title[:12] else 0.0
    return max(sequence_score, (sequence_score * 0.72) + (token_score * 0.28) + prefix_bonus)


def are_articles_same_issue(left_row, right_row, similarity_threshold=0.42, max_hours_apart=48):
    left_time = left_row.get("_article_datetime")
    right_time = right_row.get("_article_datetime")

    if not pd.isna(left_time) and not pd.isna(right_time):
        if abs(left_time - right_time) > pd.Timedelta(hours=max_hours_apart):
            return False

    similarity = calculate_title_similarity(
        left_row.get("_normalized_title", ""),
        right_row.get("_normalized_title", "")
    )
    return similarity >= similarity_threshold


def summarize_group_keywords(group_df, limit=3):
    stop_words = {"기사", "뉴스", "관련", "속보", "단독", "브리핑"}
    document_counter = Counter()
    overall_counter = Counter()
    display_counter = {}

    def collect_tokens(text):
        tokens = []
        cleaned_text = re.sub(r"[^0-9A-Za-z가-힣]+", " ", str(text or ""))
        for token in cleaned_text.split():
            token = token.strip()
            if len(token) <= 1:
                continue
            if token.lower() in stop_words:
                continue
            tokens.append(token)
        return tokens

    for _, row in group_df.iterrows():
        article_tokens = []

        if "키워드" in group_df.columns:
            for keyword in str(row.get("키워드", "")).split(","):
                article_tokens.extend(collect_tokens(keyword))

        article_tokens.extend(collect_tokens(row.get("제목", "")))

        unique_normalized_tokens = set()
        for token in article_tokens:
            normalized_token = token.lower()
            overall_counter[normalized_token] += 1
            unique_normalized_tokens.add(normalized_token)
            display_counter.setdefault(normalized_token, Counter())[token] += 1

        for normalized_token in unique_normalized_tokens:
            document_counter[normalized_token] += 1

    preferred_tokens = [
        normalized_token
        for normalized_token, _ in sorted(
            document_counter.items(),
            key=lambda item: (item[1], overall_counter[item[0]]),
            reverse=True
        )
        if document_counter[normalized_token] >= 2
    ]

    if preferred_tokens:
        selected_tokens = preferred_tokens[:limit]
    else:
        selected_tokens = []
        for normalized_token, _ in overall_counter.most_common():
            if normalized_token not in selected_tokens:
                selected_tokens.append(normalized_token)
            if len(selected_tokens) >= limit:
                break

    if not selected_tokens:
        merged_titles = " ".join(group_df["제목"].fillna("").tolist())
        fallback_keywords = extract_keywords(merged_titles, num_keywords=limit)
        fallback_keywords = [keyword for keyword in fallback_keywords if keyword not in {"분석불가", "분석오류"}]
        return ", ".join(fallback_keywords[:limit]) if fallback_keywords else "요약 키워드 없음"

    display_keywords = [
        display_counter[token].most_common(1)[0][0]
        for token in selected_tokens[:limit]
    ]
    return ", ".join(display_keywords)


def summarize_group_sources(group_df):
    unique_sources = [source for source in group_df["출처"].fillna("").tolist() if source]
    unique_sources = list(dict.fromkeys(unique_sources))

    if not unique_sources:
        return "출처 확인 중"
    if len(unique_sources) == 1:
        return unique_sources[0]
    return f"{unique_sources[0]} 외 {len(unique_sources) - 1}곳"


def build_issue_groups(news_df, similarity_threshold=0.42, max_hours_apart=48):
    if news_df.empty:
        return []

    working_df = news_df.copy().reset_index(drop=True)
    working_df["_display_order"] = range(len(working_df))
    working_df["_normalized_title"] = working_df["제목"].fillna("").map(normalize_title_for_grouping)
    working_df["_article_datetime"] = working_df.apply(parse_article_datetime, axis=1)

    groups = []
    assigned_indexes = set()

    for seed_idx, seed_row in working_df.iterrows():
        if seed_idx in assigned_indexes:
            continue

        group_indexes = [seed_idx]
        assigned_indexes.add(seed_idx)
        group_changed = True

        while group_changed:
            group_changed = False
            current_group_rows = working_df.loc[group_indexes]

            for candidate_idx, candidate_row in working_df.iterrows():
                if candidate_idx in assigned_indexes:
                    continue

                if any(
                    are_articles_same_issue(candidate_row, group_row, similarity_threshold, max_hours_apart)
                    for _, group_row in current_group_rows.iterrows()
                ):
                    group_indexes.append(candidate_idx)
                    assigned_indexes.add(candidate_idx)
                    group_changed = True

        group_df = (
            working_df.loc[group_indexes]
            .sort_values("_display_order", ascending=True)
            .reset_index(drop=True)
        )
        representative_row = group_df.iloc[0].to_dict()
        related_articles_df = group_df.iloc[1:].reset_index(drop=True)

        group_datetime = representative_row.get("발행일") or representative_row.get("수집일시") or "발행일 확인 중"
        group_keywords = summarize_group_keywords(group_df)
        group_sources = summarize_group_sources(group_df)
        related_count = len(related_articles_df)

        groups.append(
            {
                "representative": representative_row,
                "related_articles": related_articles_df.to_dict("records"),
                "related_count": related_count,
                "article_count": len(group_df),
                "source_summary": group_sources,
                "display_time": group_datetime,
                "keyword_summary": group_keywords,
                "group_label": f"관련 기사 {related_count}건" if related_count else "단독 기사",
            }
        )

    return groups


def build_issue_groups_html(issue_groups, show_sentiment=True):
    if not issue_groups:
        return dedent("""
        <div class="news-empty-state">
            아직 표시할 뉴스가 없습니다.<br>
            왼쪽에서 조건을 선택하고 크롤링을 시작해 주세요.
        </div>
        """).strip()

    issue_blocks = []
    for issue_index, issue_group in enumerate(issue_groups, start=1):
        representative = issue_group["representative"]
        representative_title = html.escape(str(representative.get("제목", "제목 없음")))
        representative_source = html.escape(str(representative.get("출처", "출처 없음")))
        representative_time = html.escape(str(issue_group["display_time"]))
        representative_link = html.escape(str(representative.get("링크", "#")), quote=True)
        source_summary = html.escape(str(issue_group["source_summary"]))
        keyword_summary = html.escape(str(issue_group["keyword_summary"]))
        group_label = html.escape(str(issue_group["group_label"]))

        sentiment_html = ""
        sentiment_value = representative.get("감정", "")
        if show_sentiment and sentiment_value:
            sentiment_html = f'<span class="issue-badge">{html.escape(str(sentiment_value))}</span>'

        if issue_group["related_articles"]:
            related_items_html = ""
            related_items = []
            for related_article in issue_group["related_articles"]:
                related_title = html.escape(str(related_article.get("제목", "제목 없음")))
                related_source = html.escape(str(related_article.get("출처", "출처 없음")))
                related_time = html.escape(str(related_article.get("발행일") or related_article.get("수집일시") or "발행일 확인 중"))
                related_link = html.escape(str(related_article.get("링크", "#")), quote=True)

                related_items.append(
                    dedent(f"""
                    <li class="issue-related-item">
                        <a class="issue-related-title" href="{related_link}" target="_blank">{related_title}</a>
                        <div class="issue-related-meta">{related_source} · 발행 {related_time}</div>
                    </li>
                    """).strip()
                )

            related_items_html = dedent(f"""
            <div class="issue-related-heading">관련 기사 {issue_group['related_count']}건</div>
            <ul class="issue-related-list">
                {''.join(related_items)}
            </ul>
            """).strip()
            issue_blocks.append(
                dedent(f"""
                <details class="issue-group">
                    <summary class="issue-summary">
                        <div class="issue-summary-top">
                            <div class="issue-summary-index">ISSUE {issue_index:02d}</div>
                            <div class="issue-summary-count">{group_label}</div>
                        </div>
                        <div class="issue-summary-title">{representative_title}</div>
                        <div class="issue-summary-meta">{source_summary} · 발행 {representative_time}</div>
                        <div class="issue-summary-keywords">공통 키워드 요약: {keyword_summary}</div>
                        <div class="issue-summary-badges">{sentiment_html}</div>
                    </summary>
                    <div class="issue-group-body">
                        <div class="issue-lead-card">
                            <div class="issue-body-label">대표 기사</div>
                            <div class="issue-body-title">{representative_title}</div>
                            <div class="issue-body-meta">{representative_source} · 발행 {representative_time}</div>
                            <a class="news-card-link issue-body-link" href="{representative_link}" target="_blank">READ ARTICLE</a>
                        </div>
                        {related_items_html}
                    </div>
                </details>
                """).strip()
            )
            continue

        issue_blocks.append(
            dedent(f"""
            <article class="issue-group issue-group-single">
                <div class="issue-summary issue-summary-single">
                    <div class="issue-summary-top">
                        <div class="issue-summary-index">ISSUE {issue_index:02d}</div>
                        <div class="issue-summary-count">{group_label}</div>
                    </div>
                    <a class="issue-summary-title issue-summary-link" href="{representative_link}" target="_blank">{representative_title}</a>
                    <div class="issue-summary-meta">{representative_source} · 발행 {representative_time}</div>
                    <div class="issue-summary-keywords">공통 키워드 요약: {keyword_summary}</div>
                    <div class="issue-summary-badges">{sentiment_html}</div>
                    <div class="issue-summary-actions">
                        <a class="news-card-link issue-body-link" href="{representative_link}" target="_blank">READ ARTICLE</a>
                    </div>
                </div>
            </article>
            """).strip()
        )

    return "".join(issue_blocks)


APP_DIR = Path(__file__).resolve().parent


def load_global_tax_free_logo():
    assets_dir = APP_DIR / "assets"
    for file_name in (
        "global-tax-free-logo.png",
        "global-tax-free-logo.jpg",
        "global-tax-free-logo.jpeg",
        "global-tax-free-logo.webp",
        "global-tax-free-logo.svg",
    ):
        logo_path = assets_dir / file_name
        if not logo_path.exists():
            continue

        if logo_path.suffix.lower() == ".svg":
            return logo_path.read_text(encoding="utf-8")

        mime_type = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
        }.get(logo_path.suffix.lower(), "application/octet-stream")
        encoded = base64.b64encode(logo_path.read_bytes()).decode("ascii")
        return f'<img alt="GLOBAL TAX FREE logo" src="data:{mime_type};base64,{encoded}">'

    return '<div class="brand-wordmark-fallback">GLOBAL TAX FREE</div>'


GLOBAL_TAX_FREE_LOGO = load_global_tax_free_logo()

# 페이지 설정
st.set_page_config(
    page_title="Global Tax Free Morning News Report",
    page_icon="📰",
    layout="wide",
    initial_sidebar_state="expanded"
)

# 스타일 추가
st.markdown("""
    <style>
    :root {
        --font-ko: "Malgun Gothic", "맑은 고딕", sans-serif;
        --font-en: "Times New Roman", Times, serif;
        --font-mixed: "Times New Roman", Times, "Malgun Gothic", "맑은 고딕", serif;
    }
    html, body, [data-testid="stAppViewContainer"], [data-testid="stSidebar"] {
        font-family: var(--font-mixed);
    }
    [data-testid="stAppViewContainer"] * {
        font-family: var(--font-mixed);
    }
    .material-symbols-rounded,
    .material-icons,
    [data-testid="stIconMaterial"] {
        font-family: "Material Symbols Rounded" !important;
        font-weight: normal;
        font-style: normal;
        letter-spacing: normal;
        text-transform: none;
        white-space: nowrap;
        direction: ltr;
        -webkit-font-feature-settings: "liga";
        -webkit-font-smoothing: antialiased;
        font-variation-settings: "FILL" 0, "wght" 400, "GRAD" 0, "opsz" 24;
    }
    [data-testid="stAppViewContainer"] {
        background: linear-gradient(180deg, #ffffff 0%, #fbf8f1 100%);
    }
    .main {
        padding-top: 1.2rem;
    }
    .block-container {
        width: 100%;
        max-width: min(1480px, calc(100vw - 2rem));
        padding-top: 2rem;
        padding-bottom: 4rem;
        padding-left: clamp(1rem, 2.6vw, 2.4rem);
        padding-right: clamp(1rem, 2.6vw, 2.4rem);
    }
    [data-testid="stSidebar"] {
        background: linear-gradient(180deg, #fffdf8 0%, #f5f0e5 100%);
        border-right: 1px solid #e6dcc9;
    }
    [data-testid="stSidebar"] * {
        font-family: var(--font-mixed);
    }
    [data-testid="stSidebar"] label,
    [data-testid="stSidebar"] legend,
    [data-testid="stSidebar"] p,
    [data-testid="stSidebar"] span,
    [data-testid="stSidebar"] .stMarkdown,
    [data-testid="stSidebar"] .stMarkdown * {
        color: #171717 !important;
    }
    [data-testid="stSidebar"] input,
    [data-testid="stSidebar"] textarea,
    [data-testid="stSidebar"] [data-baseweb="select"] input,
    [data-testid="stSidebar"] [data-baseweb="select"] div,
    [data-testid="stSidebar"] [data-baseweb="tag"] span {
        color: #171717 !important;
    }
    [data-testid="stSidebar"] input::placeholder,
    [data-testid="stSidebar"] textarea::placeholder {
        color: #7b8088 !important;
    }
    .stTabs [data-baseweb="tab-list"] {
        gap: 0.75rem;
        border-bottom: 1px solid #dfd6c6;
        margin-bottom: 1.25rem;
    }
    .stTabs [data-baseweb="tab"] {
        font-family: var(--font-mixed);
        font-size: 0.95rem;
        color: #6d727a;
        padding: 0.55rem 0.9rem 0.7rem;
    }
    .stTabs [aria-selected="true"] {
        color: #d96f1d !important;
        border-bottom: 2px solid #d96f1d !important;
    }
    .stButton > button,
    .stDownloadButton > button {
        border: 1.3px solid #d96f1d;
        background: #fff8ef;
        color: #8d4f16;
        border-radius: 999px;
        font-family: var(--font-mixed);
        letter-spacing: 0.04em;
        font-weight: 700;
    }
    .stButton > button:hover,
    .stDownloadButton > button:hover {
        border-color: #9d5e23;
        color: #9d5e23;
        background: #fff2dd;
    }
    .report-masthead {
        text-align: center;
        margin: 0 auto 2rem;
        padding-top: 0.25rem;
    }
    .brand-lockup {
        display: flex;
        justify-content: center;
        align-items: center;
        margin-bottom: 1.15rem;
    }
    .brand-logo {
        width: min(430px, 86vw);
        line-height: 0;
    }
    .brand-logo svg,
    .brand-logo img {
        display: block;
        width: 100%;
        height: auto;
    }
    .brand-wordmark-fallback {
        font-family: var(--font-en);
        font-size: 1.85rem;
        font-weight: 700;
        letter-spacing: 0.03em;
        color: #f18121;
    }
    .report-title {
        font-family: var(--font-en);
        font-size: 1.95rem;
        line-height: 1.45;
        color: #7c7c7c;
        font-weight: 700;
        letter-spacing: 0.03em;
    }
    .report-submeta {
        margin: 0.35rem 0 1rem;
        color: #707888;
        text-align: right;
        font-family: var(--font-en);
        font-size: 0.84rem;
    }
    .report-section-title {
        display: flex;
        align-items: center;
        gap: 0.65rem;
        margin: 0.35rem 0 0.95rem 0.55rem;
        font-family: var(--font-ko);
        font-size: 1.45rem;
        font-weight: 700;
        color: #111111;
    }
    .report-section-title .report-dot {
        width: 11px;
        height: 11px;
        border-radius: 50%;
        background: #f2cb37;
        box-shadow: 0 0 0 2px #ffffff, -12px 0 0 -2px #7b8088;
        flex: 0 0 11px;
    }
    .report-board {
        border: 1.5px solid #546987;
        border-radius: 24px;
        padding: 1.8rem 1.45rem 1.4rem;
        background: rgba(255, 255, 255, 0.88);
        width: 100%;
        box-sizing: border-box;
    }
    .report-board .news-cards {
        display: grid;
        gap: 1.25rem;
        width: 100%;
    }
    .issue-group {
        border: 1.35px solid #4c6282;
        background: linear-gradient(180deg, #ffffff 0%, #fbfbf7 100%);
        transition: border-color 0.2s ease, box-shadow 0.2s ease;
    }
    .issue-group:hover {
        background: linear-gradient(180deg, #fffdf7 0%, #fdf4e7 100%);
        border-color: #d96f1d;
        box-shadow: 0 14px 28px rgba(217, 111, 29, 0.12);
    }
    .issue-group[open] {
        border-color: #365172;
        box-shadow: 0 14px 30px rgba(39, 59, 84, 0.08);
    }
    .issue-summary {
        list-style: none;
        cursor: pointer;
        padding: 1.3rem 1.2rem;
    }
    .issue-summary-single {
        cursor: default;
    }
    .issue-summary::-webkit-details-marker {
        display: none;
    }
    .issue-summary::marker {
        content: "";
    }
    .issue-summary-top {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 0.8rem;
        margin-bottom: 0.75rem;
    }
    .issue-summary-index {
        font-family: var(--font-en);
        font-size: 0.76rem;
        letter-spacing: 0.22em;
        color: #8a8f97;
    }
    .issue-summary-count {
        border: 1px solid #d7b07f;
        background: #fff8ee;
        color: #865325;
        padding: 0.26rem 0.7rem;
        font-size: 0.76rem;
        font-family: var(--font-mixed);
        line-height: 1.35;
        white-space: nowrap;
    }
    .issue-summary-title {
        font-family: var(--font-mixed);
        font-size: 1.14rem;
        line-height: 1.65;
        color: #121212;
        margin-bottom: 0.75rem;
        transition: color 0.2s ease;
    }
    .issue-summary-link {
        display: block;
        text-decoration: none;
    }
    .issue-summary-link:hover {
        color: #d96f1d;
    }
    .issue-group:hover .issue-summary-title {
        color: #b55d19;
    }
    .issue-summary-meta {
        font-family: var(--font-mixed);
        color: #6e7682;
        font-size: 0.84rem;
        margin-bottom: 0.55rem;
    }
    .issue-summary-keywords {
        font-family: var(--font-ko);
        color: #6e6353;
        font-size: 0.88rem;
        line-height: 1.6;
        margin-bottom: 0.65rem;
    }
    .issue-summary-badges {
        display: flex;
        flex-wrap: wrap;
        gap: 0.45rem;
    }
    .issue-summary-actions {
        margin-top: 0.95rem;
        display: flex;
    }
    .issue-badge {
        border: 1px solid #d7b07f;
        background: #fff8ee;
        color: #865325;
        padding: 0.28rem 0.6rem;
        font-size: 0.72rem;
        font-family: var(--font-mixed);
        line-height: 1.4;
    }
    .issue-group-body {
        border-top: 1px solid #d7dee9;
        padding: 0 1.2rem 1.2rem;
    }
    .issue-lead-card {
        padding: 1rem 0 1.15rem;
        border-bottom: 1px dashed #d7dee9;
        margin-bottom: 1rem;
    }
    .issue-body-label {
        font-family: var(--font-ko);
        color: #6b7381;
        font-size: 0.82rem;
        margin-bottom: 0.5rem;
    }
    .issue-body-title {
        font-family: var(--font-mixed);
        font-size: 1.06rem;
        line-height: 1.65;
        color: #121212;
        margin-bottom: 0.6rem;
    }
    .issue-body-meta {
        font-family: var(--font-mixed);
        color: #6e7682;
        font-size: 0.82rem;
        margin-bottom: 0.85rem;
    }
    .issue-body-link {
        align-self: flex-start;
    }
    .issue-related-heading {
        font-family: var(--font-ko);
        color: #4a5b73;
        font-size: 0.95rem;
        margin-bottom: 0.8rem;
    }
    .single-issue-heading {
        margin-top: 0.1rem;
        margin-bottom: 0.1rem;
        color: #7b8088;
    }
    .issue-related-list {
        list-style: none;
        padding: 0;
        margin: 0;
        display: grid;
        gap: 0.8rem;
    }
    .issue-related-item {
        padding: 0.8rem 0.9rem;
        border: 1px solid #dbe2ec;
        background: rgba(250, 251, 252, 0.95);
        transition: background 0.2s ease, border-color 0.2s ease, transform 0.2s ease;
    }
    .issue-related-item:hover {
        background: #fff7ed;
        border-color: #e3b17e;
        transform: translateY(-1px);
    }
    .issue-related-title {
        display: inline-block;
        text-decoration: none;
        color: #26354a;
        font-family: var(--font-mixed);
        font-size: 0.97rem;
        line-height: 1.55;
        margin-bottom: 0.35rem;
    }
    .issue-related-title:hover {
        color: #d96f1d;
    }
    .issue-related-meta {
        color: #7a8595;
        font-family: var(--font-mixed);
        font-size: 0.8rem;
    }
    .news-card-index {
        text-align: center;
        font-family: var(--font-en);
        font-size: 0.76rem;
        letter-spacing: 0.22em;
        color: #8a8f97;
        margin-bottom: 0.85rem;
    }
    .news-card-title {
        text-align: center;
        font-family: var(--font-mixed);
        font-size: 1.2rem;
        line-height: 1.6;
        color: #121212;
        margin-bottom: 0.8rem;
    }
    .news-card-meta {
        text-align: center;
        font-family: var(--font-mixed);
        color: #6e7682;
        font-size: 0.82rem;
        margin-bottom: 0.8rem;
    }
    .news-card-tags {
        display: flex;
        justify-content: center;
        gap: 0.45rem;
        flex-wrap: wrap;
        margin-bottom: 0.9rem;
    }
    .news-tag {
        border: 1px solid #d7b07f;
        background: #fff8ee;
        color: #865325;
        padding: 0.28rem 0.6rem;
        font-size: 0.72rem;
        font-family: var(--font-mixed);
        line-height: 1.4;
    }
    .keyword-tag {
        max-width: 100%;
        word-break: keep-all;
    }
    .news-card-link {
        align-self: center;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        padding: 0.55rem 1rem;
        border: 1.15px solid #556987;
        color: #556987;
        text-decoration: none;
        font-family: var(--font-en);
        font-size: 0.8rem;
        letter-spacing: 0.09em;
        transition: all 0.2s ease;
    }
    .news-card-link:hover {
        color: #fffdf9;
        background: #556987;
    }
    .news-empty-state {
        min-height: 220px;
        display: flex;
        align-items: center;
        justify-content: center;
        text-align: center;
        color: #7a7f86;
        font-family: var(--font-ko);
        line-height: 1.8;
    }
    .footer-note {
        text-align: center;
        color: #8b8b8b;
        font-size: 0.8rem;
        font-family: var(--font-en);
    }
    @media (max-width: 780px) {
        [data-testid="stSidebar"] label,
        [data-testid="stSidebar"] legend,
        [data-testid="stSidebar"] p,
        [data-testid="stSidebar"] span,
        [data-testid="stSidebar"] .stMarkdown,
        [data-testid="stSidebar"] .stMarkdown *,
        [data-testid="stSidebar"] input,
        [data-testid="stSidebar"] textarea,
        [data-testid="stSidebar"] [data-baseweb="select"] input,
        [data-testid="stSidebar"] [data-baseweb="select"] div,
        [data-testid="stSidebar"] [data-baseweb="tag"] span {
            color: #171717 !important;
        }
        .block-container {
            padding-top: 1.25rem;
            max-width: 100%;
            padding-left: 0.9rem;
            padding-right: 0.9rem;
        }
        .brand-logo {
            width: min(320px, 88vw);
        }
        .report-title {
            font-size: 1.35rem;
        }
        .report-board {
            padding: 1.45rem 1rem 1.1rem;
        }
        .news-card {
            min-height: 132px;
            padding: 1.1rem 0.9rem;
        }
        .news-card-title {
            font-size: 1.04rem;
        }
        .issue-summary {
            padding: 1.05rem 0.95rem;
        }
        .issue-group-body {
            padding: 0 0.95rem 0.95rem;
        }
        .issue-summary-title,
        .issue-body-title {
            font-size: 1rem;
        }
        .issue-summary-top {
            align-items: flex-start;
            flex-direction: column;
        }
    }
    </style>
    """, unsafe_allow_html=True)

# 제목
st.markdown(
    f"""
    <section class="report-masthead">
        <div class="brand-lockup">
            <div class="brand-logo">{GLOBAL_TAX_FREE_LOGO}</div>
        </div>
        <div class="report-title">Corporate Strategy Department<br>News Report</div>
    </section>
    """,
    unsafe_allow_html=True
)

# 사이드바 설정
with st.sidebar:
    st.title("🔍 검색 및 설정")
    st.markdown("---")

    # 사전 정의 키워드 목록
    predefined_keywords = ["택스리펀드", "택스리펀", "의료관광", "외국인관광객", "인바운드 관광"]

    # 검색 옵션
    search_mode = st.radio(
        "검색 방식 선택:",
        ["직접 입력", "사전 정의 키워드"],
        index=0
    )

    show_sentiment = True
    show_keywords = True
    news_sources = ["네이버 뉴스", "Google 뉴스"]
    search_submitted = False

    if search_mode == "직접 입력":
        with st.form("direct_search_form", clear_on_submit=False, enter_to_submit=True):
            keyword_input = st.text_input(
                "검색 키워드를 쉼표(,)로 구분하여 입력하세요:",
                placeholder="예: 인공지능, 경제뉴스, 기술"
            )
            keywords_list = [k.strip() for k in keyword_input.split(",") if k.strip()] if keyword_input else []
            category = "all"

            # 뉴스 개수 설정
            max_items = st.slider(
                "크롤링할 뉴스 개수 (키워드당):",
                min_value=5,
                max_value=100,
                value=20,
                step=5
            )

            # 정렬 옵션
            st.markdown("---")
            st.title("🗂 정렬 옵션")
            sort_options = ["최신순"]
            if show_sentiment:
                sort_options.append("정확도순")
            sort_option = st.selectbox(
                "뉴스 정렬 기준:",
                sort_options,
                index=sort_options.index("정확도순") if "정확도순" in sort_options else 0
            )

            # 크롤링 버튼
            st.markdown("---")
            search_submitted = st.form_submit_button("Search", use_container_width=True)
    elif search_mode == "사전 정의 키워드":
        selected_keywords = st.multiselect(
            "검색할 키워드를 선택하세요 (OR 조건):",
            predefined_keywords,
            default=["택스리펀드"]
        )
        keywords_list = selected_keywords if selected_keywords else []
        category = "all"

        # 정렬 옵션
        max_items = st.slider(
            "크롤링할 뉴스 개수 (키워드당):",
            min_value=5,
            max_value=100,
            value=20,
            step=5
        )

        st.markdown("---")
        st.title("🗂 정렬 옵션")
        sort_options = ["최신순"]
        if show_sentiment:
            sort_options.append("정확도순")
        sort_option = st.selectbox(
            "뉴스 정렬 기준:",
            sort_options,
            index=sort_options.index("정확도순") if "정확도순" in sort_options else 0
        )

        # 크롤링 버튼
        st.markdown("---")
        search_submitted = st.button("Search", use_container_width=True)

if search_submitted:
    with st.spinner("⏳ 뉴스 수집 중..."):
        # 진행 상황 표시
        progress_bar = st.progress(0)
        
        all_dfs = []
        crawl_steps = len(news_sources) if not keywords_list else len(news_sources) * len(keywords_list)
        step = 0
        
        if keywords_list:
            for keyword in keywords_list:
                if "네이버 뉴스" in news_sources:
                    df_single = crawl_naver_news(keyword=keyword, category="all", max_items=max_items)
                    if not df_single.empty:
                        all_dfs.append(df_single)
                    step += 1
                    progress_bar.progress(step / crawl_steps * 0.5)
                if "Google 뉴스" in news_sources:
                    df_single = crawl_google_news(keyword=keyword, max_items=max_items)
                    if not df_single.empty:
                        all_dfs.append(df_single)
                    step += 1
                    progress_bar.progress(step / crawl_steps * 0.5)
            
        # 모든 결과 합치기 (중복 제거)
        if all_dfs:
            df = pd.concat(all_dfs, ignore_index=True)
            df = df.drop_duplicates(subset=['링크'], keep='first')
        else:
            df = pd.DataFrame()
    
    progress_bar.progress(50)
    if df.empty:
        st.error("❌ 뉴스를 찾을 수 없습니다. 다른 키워드나 카테고리를 시도해주세요.")
    else:
        # 분석 추가
        if show_sentiment or show_keywords:
            with st.spinner("🔬 분석 중입니다..."):
                if show_sentiment:
                    df['감정'] = df['제목'].apply(lambda x: analyze_sentiment(x)['sentiment'])
                    df['신뢰도'] = df['제목'].apply(lambda x: f"{analyze_sentiment(x)['confidence']*100:.0f}%")
                    
                if show_keywords:
                    df['키워드'] = df['제목'].apply(lambda x: ', '.join(extract_keywords(x, num_keywords=3)))
        
        progress_bar.progress(100)
        progress_bar.empty()
            
        # 탭 생성
        tab1, tab2, tab3 = st.tabs(["📰 뉴스 목록", "📊 통계", "💾 다운로드"])
        
        # 탭 1: 뉴스 목록
        with tab1:
                filtered_df = df.copy()
                
                # 정렬 옵션 적용
                latest_time_column = '발행일' if '발행일' in filtered_df.columns else '수집일시'
                if latest_time_column in filtered_df.columns:
                    filtered_df = filtered_df.copy()
                    filtered_df['_정렬일시'] = pd.to_datetime(filtered_df[latest_time_column], errors='coerce')

                if sort_option == "최신순":
                    if '_정렬일시' in filtered_df.columns:
                        filtered_df = (
                            filtered_df.sort_values('_정렬일시', ascending=False, na_position='last')
                            .drop('_정렬일시', axis=1)
                            .reset_index(drop=True)
                        )
                    else:
                        filtered_df = filtered_df.reset_index(drop=True)
                elif sort_option == "정확도순":
                    if '신뢰도' in filtered_df.columns:
                        filtered_df = filtered_df.copy()
                        filtered_df['신뢰도_숫자'] = pd.to_numeric(
                            filtered_df['신뢰도'].astype(str).str.rstrip('%'),
                            errors='coerce'
                        )
                        if filtered_df['신뢰도_숫자'].notna().any():
                            drop_columns = ['신뢰도_숫자']
                            if '_정렬일시' in filtered_df.columns:
                                drop_columns.append('_정렬일시')
                            filtered_df = filtered_df.sort_values('신뢰도_숫자', ascending=False).drop(drop_columns, axis=1).reset_index(drop=True)
                        else:
                            st.warning("⚠️ 신뢰도 값이 없어서 최신순으로 정렬합니다.")
                            if '_정렬일시' in filtered_df.columns:
                                filtered_df = (
                                    filtered_df.sort_values('_정렬일시', ascending=False, na_position='last')
                                    .drop('_정렬일시', axis=1)
                                    .reset_index(drop=True)
                                )
                            else:
                                filtered_df = filtered_df.reset_index(drop=True)
                    else:
                        st.warning("⚠️ 정확도 정렬에는 감정분석이 필요합니다. 최신순으로 정렬합니다.")
                        if '_정렬일시' in filtered_df.columns:
                            filtered_df = (
                                filtered_df.sort_values('_정렬일시', ascending=False, na_position='last')
                                .drop('_정렬일시', axis=1)
                                .reset_index(drop=True)
                            )
                        else:
                            filtered_df = filtered_df.reset_index(drop=True)
                
                issue_groups = build_issue_groups(filtered_df)
                issue_groups_html = build_issue_groups_html(
                    issue_groups,
                    show_sentiment=show_sentiment
                )
                st.markdown(
                    f"""
                    <div class="report-section-title">
                        <span class="report-dot"></span>
                        <span>주요 뉴스</span>
                    </div>
                    <div class="report-board">
                        <div class="report-submeta">TOTAL {len(issue_groups):02d} GROUPS · {len(filtered_df):02d} ARTICLES · SORT {html.escape(sort_option)}</div>
                        <div class="news-cards">
                            {issue_groups_html}
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True
                )
            
            # 탭 2: 통계
        with tab2:
                st.subheader("분석 통계")
                stats_issue_groups = build_issue_groups(df)
                unique_sources_count = df['출처'].nunique() if '출처' in df.columns else 0
                
                col1, col2, col3 = st.columns(3)
                
                with col1:
                    st.metric("총 뉴스 수", len(df))
                
                with col2:
                    st.metric("이슈 묶음", len(stats_issue_groups))
                
                with col3:
                    st.metric("출처 수", unique_sources_count)
                
                st.markdown("---")
                
                if show_sentiment:
                    st.subheader("😊 감정 분석 결과")
                    sentiment_counts = df['감정'].value_counts()
                    
                    # 차트
                    col1, col2 = st.columns([1, 1])
                    
                    with col1:
                        st.bar_chart(sentiment_counts)
                    
                    with col2:
                        sentiment_pct = (sentiment_counts / len(df) * 100).round(1)
                        for sentiment, pct in sentiment_pct.items():
                            st.write(f"{sentiment}: {pct}%")
                
                if show_keywords:
                    st.subheader("🏷️ 자주 나오는 키워드 (Top 10)")
                    
                    # 모든 키워드 추출
                    all_keywords = []
                    for keywords_str in df['키워드']:
                        all_keywords.extend([k.strip() for k in keywords_str.split(',')])
                    
                    from collections import Counter
                    keyword_counts = Counter(all_keywords)
                    top_keywords = dict(keyword_counts.most_common(10))
                    
                    st.bar_chart(pd.Series(top_keywords))
            
            # 탭 3: 다운로드
        with tab3:
                st.subheader("데이터 다운로드")
                
                # CSV 다운로드
                csv = df.to_csv(index=False, encoding='utf-8-sig')
                st.download_button(
                    label="⬇️ CSV 다운로드",
                    data=csv,
                    file_name=f"news_{int(time.time())}.csv",
                    mime="text/csv"
                )
                
                # Excel 다운로드 (옵션)
                try:
                    import openpyxl
                    from io import BytesIO
                    
                    buffer = BytesIO()
                    with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
                        df.to_excel(writer, index=False)
                    buffer.seek(0)
                    
                    st.download_button(
                        label="� Excel 다운로드",
                        data=buffer.getvalue(),
                        file_name=f"news_{int(time.time())}.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    )
                except:
                    st.info("Excel 다운로드를 위해 openpyxl 라이브러리를 설치하세요.")

# 푸터
st.markdown("---")
st.markdown("""
    <div class='footer-note'>
        GLOBAL TAX FREE NEWS REPORT | 2026
    </div>
    """, unsafe_allow_html=True)
